"""Separate transport overhead from content and retain explicit operational state."""
import copy
import json
import re
from .common import digest, dumps, persistent_message, without_reasoning
from .reconciliation import resolution_key, commit_action, evidence_index, unsupported_claim


def content_budget(engine, head):
    """Reserve transport costs separately; the relay remains the exact guard."""
    margin = 1024
    from .schema_budget import schema_budget
    tool_tokens, schema_meta, measured = schema_budget(engine)
    system_tokens = engine.counter.messages(head) if head else 0
    if measured.get('system_head_hash') == digest(head) and 'system_tokens' in measured:
        system_tokens = int(measured['system_tokens'])
    output_tokens = engine.settings.get('generation_reserve_tokens', 8192)
    allowance = max(0, engine.context_length - output_tokens - margin)
    cap = max(0, allowance - tool_tokens - system_tokens)
    return {**schema_meta, 'tool_schema_tokens': tool_tokens, 'system_tokens': system_tokens,
            'safety_margin_tokens': margin, 'generation_reserve_tokens': output_tokens,
            'physical_prompt_allowance': allowance, 'content_capacity_tokens': cap}


def recent_allowance(engine, frame, hashes, weights, reusable):
    preferred = engine.settings['tail_tokens']
    floor = engine.settings['minimum_tail_tokens']
    retained = 0
    if reusable:
        seen = len(frame['seen'])
        previous = sum(weights[frame['cut']:seen])
        introduced = sum(weights[seen:])
        retained = int(previous * engine.settings.get('continuity_retained_fraction', .85)) + introduced
    return max(floor, preferred, retained)


_STATE_FIELDS = ('objective', 'phase', 'hard_requirements', 'completed', 'findings', 'decisions', 'files', 'unresolved', 'next_action')
_STATE_MARKERS = re.compile(r'^(?:Current )?(Phase|Confirmed finding|Confirmed|Finding|Found|Decision|Verified|Completed|Hard requirement|Invariant|Important files|Unresolved issue|Unresolved|Blocked|Next action|Next intended action|Resolved|Superseded)\s*:\s*(.+)', re.I)


class TaskState:
    """Literal, source-linked state. Never reads reasoning_content or infers results."""
    def __init__(self, engine):
        self.engine = engine
        self.seen = []
        with engine.store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS active_task_state(session TEXT PRIMARY KEY, state TEXT NOT NULL)')
            row = db.execute('SELECT state FROM active_task_state WHERE session=?', (engine.session_id,)).fetchone()
        loaded = without_reasoning(json.loads(row[0]), strip_spans=True) if row else {}
        self.state = {field: [r for r in loaded.get(field, []) if isinstance(r, dict)
                             and isinstance(r.get('text'), str) and r['text'].strip()
                             and isinstance(r.get('source_id'), str)] for field in _STATE_FIELDS}
        self.summary_seen = set()
        self.resolutions = [r for r in loaded.get("resolved", []) if isinstance(r, dict)
                            and r.get("field") in _STATE_FIELDS and isinstance(r.get("key"), str)
                            and isinstance(r.get("source_id"), str) and isinstance(r.get("target_source_id"), str)]
        self.reconciled = False

    def observe(self, body, hashes):
        self.reconciled = False
        self.positions = {h: i for i, h in enumerate(hashes)}
        self.records, self.evidence = evidence_index(body, hashes)
        start = len(self.seen) if hashes[:len(self.seen)] == self.seen else 0
        for message, source in zip(body[start:], hashes[start:]):
            message = persistent_message(message)
            text = message.get('content') or ''
            role = message.get('role')
            if role == 'user' and isinstance(text, str):
                objective = re.match(r'^(?:Objective|Current objective)\s*:\s*(.+)', text, re.I)
                if objective or re.match(r'^(?:implement|repair|fix|debug|investigate|build|update)\b', text, re.I) or (not self.state['objective'] and len(text) >= 20):
                    self.state['objective'] = [{'text': (objective[1] if objective else text.splitlines()[0])[:400], 'source_id': source}]
                    from .admission import evidence_lines
                    self.state['hard_requirements'] = [{'text': line[:800], 'source_id': source, 'line': n}
                        for score, n, line in evidence_lines(text) if score in (80,70)]
            # Only explicit operational markers in visible assistant/user text.
            # Unmarked prose and tool dumps are never copied as purported state.
            if role in ('assistant', 'user') and isinstance(text, str):
                visible = text
                lines = re.split(r'\n|(?<=\.)\s+(?=(?:Next action|Unresolved|Phase|Finding|Decision):)', visible)
                fenced = False
                for line in lines:
                    if line.lstrip().startswith('```'):
                        fenced = not fenced
                        continue
                    if fenced:
                        continue
                    line = re.sub(r'^\s*(?:[-*+]\s+|\d+[.)]\s+)', '', line).replace('**', '').strip()
                    match = _STATE_MARKERS.match(line)
                    if not match:
                        continue
                    marker, value = match[1].lower(), match[2][:400]
                    field = ('phase' if marker == 'phase' else 'next_action' if marker.startswith('next') else
                             'unresolved' if marker in ('unresolved', 'unresolved issue', 'blocked') else 'completed' if marker in ('completed','verified') else
                             'decisions' if marker == 'decision' else 'hard_requirements' if marker in ('hard requirement','invariant') else 'files' if marker == 'important files' else 'findings')
                    if marker in ('resolved', 'superseded'):
                        self._resolve(value, source, ('unresolved', 'next_action') if marker == 'resolved' else ('findings', 'decisions', 'unresolved', 'next_action'),
                                      status='RESOLVED' if marker == 'resolved' else 'SUPERSEDED')
                        continue
                    if marker == 'completed':
                        self._resolve(value, source, ('next_action',))
                    if marker == 'verified' and not any(self.positions.get(h, -1) < self.positions[source] for h, kind in self.evidence.items() if kind == 'verification'):
                        field = 'findings'; value = 'Reported, unverified: '+value
                    entry = {'text': value, 'source_id': source}
                    if field in ('phase', 'next_action'):
                        self.state[field] = [entry]
                    else:
                        self.state[field] = [r for r in self.state[field] if r['text'] != value][-5:] + [entry]
            if self.evidence.get(source) == 'commit':
                for field in ('unresolved', 'next_action'):
                    for row in list(self.state[field]):
                        if commit_action(row['text']):
                            self._resolve(row['text'], source, (field,), reason='successful_commit')
            for call in message.get('tool_calls') or []:
                function = call.get('function') or {}
                try:
                    args = json.loads(function.get('arguments') or '{}')
                except (ValueError, TypeError):
                    continue
                path = args.get('path') if isinstance(args, dict) else None
                if function.get('name') in ('read_file', 'write_file', 'patch') and isinstance(path, str):
                    entry = {'text': path[:500], 'source_id': source}
                    self.state['files'] = [r for r in self.state['files'] if r['text'] != entry['text']][-7:] + [entry]
        self.seen = list(hashes)
        self._summaries(hashes)
        self._apply_resolutions()
        with self.engine.store.connect() as db:
            db.execute('INSERT OR REPLACE INTO active_task_state VALUES (?,?)', (self.engine.session_id, dumps(without_reasoning({**self.state, "resolved": self.resolutions}, strip_spans=True))))

    def _resolve(self, text, source, fields, *, status='RESOLVED', reason='explicit_resolution'):
        key = resolution_key(text)
        for field in fields:
            for row in self.state[field]:
                if resolution_key(row['text']) != key or self.positions.get(row['source_id'], -1) > self.positions.get(source, -1):
                    continue
                if row.get('status') in ('RESOLVED', 'SUPERSEDED'):
                    continue
                receipt = {'key': key, 'field': field, 'target_source_id': row['source_id'],
                           'source_id': source, 'status': status, 'reason': reason}
                self.resolutions = [r for r in self.resolutions if (r['field'],r['key'],r['target_source_id']) != (field,key,row['source_id'])][-31:] + [receipt]
                self._mark(row, receipt)

    def _mark(self, row, receipt):
        if row.get('status') == receipt['status'] and row.get('resolved_by') == receipt['source_id']:
            return
        row.update(status=receipt['status'], resolved_by=receipt['source_id'], resolution_reason=receipt['reason'])
        self.reconciled = True

    def _apply_resolutions(self):
        for receipt in self.resolutions:
            for row in self.state[receipt['field']]:
                if resolution_key(row['text']) != receipt['key']:
                    continue
                # Rephrasing or a later user reopening never loses its evidence.
                target = receipt['target_source_id']; source = row['source_id']
                if source != target and self.positions.get(source, 0) > self.positions.get(receipt['source_id'], -1):
                    continue
                self._mark(row, receipt)

    def _summaries(self, hashes):
        """Retain typed conclusions, never plans or a second source of evidence."""
        from .summary import validate_memory_summary
        blocks = self.engine.store.compatible_page_blocks(self.engine.session_id, hashes)
        latest, _ = self.engine.store.compatible_summary(self.engine.session_id, hashes)
        blocks += self.engine.store.summary_chain(latest) if latest else []
        positions = {h: i for i, h in enumerate(hashes)}
        for row in sorted({r['id']: r for r in blocks}.values(), key=lambda r: r['id']):
            if row['id'] in self.summary_seen:
                continue
            try:
                covered = set(json.loads(row['covered']))
                if not covered.issubset(positions):
                    continue
                value = validate_memory_summary(row['text'], covered)
            except (ValueError, TypeError, KeyError):
                continue
            self.summary_seen.add(row['id'])
            self.summary_seen = set(sorted(self.summary_seen)[-64:])
            for section, field in (('results', 'findings'), ('code_state', 'findings'),
                                   ('decisions', 'decisions'), ('unresolved', 'unresolved'),
                                   ('next_actions', 'next_action')):
                for item in value.get(section, []):
                    status = item.get('status', 'DERIVED')
                    if unsupported_claim(item, self.evidence):
                        continue
                    if status == 'SUPERSEDED':
                        for reference in item['sources']:
                            record = self.records.get(reference, {})
                            if self.evidence.get(reference) == 'commit' and commit_action(item['text']):
                                self._resolve(item['text'], reference, (field,), status='SUPERSEDED', reason='successful_commit')
                            elif record.get('role') in ('user', 'assistant') and re.search(r'^(?:Resolved|Completed|Superseded):\s*'+re.escape(item['text'])+r'\s*$', record.get('content') or '', re.M | re.I):
                                self._resolve(item['text'], reference, (field,), status='SUPERSEDED')
                        continue
                    if status == 'INFERENCE':
                        continue
                    destination = 'unresolved' if status == 'CONFLICTING' else field
                    source = max(item['sources'], key=lambda h: positions[h])
                    entry = {'text': item['text'][:400], 'source_id': source,
                             'source_ids': list(dict.fromkeys(item['sources'])), 'derived': True}
                    if destination == 'next_action':
                        prior = self.state[destination]
                        if prior:
                            older = max(positions.get(r['source_id'], -1) for r in prior)
                            if positions[source] < older or positions[source] == older and not prior[0].get('derived'):
                                continue
                        self.state[destination] = [entry]
                    else:
                        self.state[destination] = [r for r in self.state[destination] if r['text'] != entry['text']][-5:] + [entry]

    def render(self):
        # Render one current conclusion per important field before optional
        # requirements/path lists. Full state remains durable and source linked.
        value = {field: [{'text': r['text'], 'source_id': r['source_id'],
                          **({'source_ids': r['source_ids']} if len(r.get('source_ids', [])) > 1 else {})}
                         for r in self.state[field] if r.get('status') not in ('RESOLVED','SUPERSEDED')]
                 for field in _STATE_FIELDS if any(r.get('status') not in ('RESOLVED','SUPERSEDED') for r in self.state[field] if r.get('status') not in ('RESOLVED','SUPERSEDED'))}
        if not value:
            return None, []
        limit = self.engine.settings.get('task_state_budget_tokens', 1024)
        def message():
            return {'role': 'assistant', 'content': '[ACTIVE TASK STATE: source-linked conclusions; newer RAW/user evidence takes precedence]\n' + dumps(value) + '\n[/ACTIVE TASK STATE]'}
        for field in ('files', 'hard_requirements', 'completed', 'findings', 'decisions', 'unresolved'):
            while len(value.get(field, [])) > 1 and self.engine.counter.text(message()['content']) > limit:
                value[field].pop(0)
        for field in ('files', 'hard_requirements', 'completed', 'phase'):
            if self.engine.counter.text(message()['content']) <= limit:
                break
            value.pop(field, None)
        for chars in (160, 96, 48):
            if self.engine.counter.text(message()['content']) <= limit:
                break
            for rows in value.values():
                for row in rows:
                    row['text'] = row['text'][:chars]
        result = message()
        if self.engine.counter.text(result['content']) > limit:
            return None, []
        sources = list(dict.fromkeys(h for rows in value.values() for r in rows
                                     for h in r.get('source_ids', [r['source_id']])))
        return result, sources

    def relevance(self):
        return ' '.join(r['text'] for field in ('objective', 'phase', 'findings', 'decisions', 'files', 'unresolved', 'next_action') for r in self.state[field] if r.get('status') not in ('RESOLVED','SUPERSEDED'))


def choose_warm(engine, blocks, cold_hashes, represented, query, *, priority_ids=(), protected_sources=()):
    """Select relevant nonoverlapping blocks, never reserve their budget over RAW."""
    from .engine import validate_memory_summary
    from .pages import terms
    words = {w.lower() for w in terms(query) if len(w) >= 4 and w.lower() not in {
        'continue', 'current', 'task', 'phase', 'please', 'fixture', 'inspect', 'verify'}}
    candidates = []
    protected_sources = set(protected_sources)
    for row in {r['id']: r for r in blocks}.values():
        try:
            coverage = set(json.loads(row['covered']))
            if not coverage.issubset(cold_hashes):
                continue
            value = validate_memory_summary(row['text'], coverage)
            sources = {s for items in value.values() for item in items for s in item['sources']}
            if (sources - protected_sources) & represented:
                continue
            text = ' '.join(item['text'] for items in value.values() for item in items)
            score = len(words & {w.lower() for w in terms(text)})
            size = engine.counter.text(row['text'])
            if size <= engine.settings.get('summary_memory_max_tokens', engine.settings['summary_max_tokens']):
                candidates.append((score, row['id'], size, row, sources))
        except (ValueError, TypeError, KeyError):
            continue
    chosen = []
    budget = engine.settings['warm_budget_tokens']
    for _, _, size, row, sources in sorted(candidates, key=lambda c: (c[3]['id'] in priority_ids, c[0], c[1]), reverse=True):
        if size <= budget and not (sources - protected_sources) & represented:
            chosen.append(row); represented |= sources; budget -= size
        if len(chosen) >= engine.settings.get('warm_max_blocks', 2):
            break
    return sorted(chosen, key=lambda r: r['id'])
