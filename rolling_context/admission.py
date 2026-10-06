"""Bounded prompt projections; every original record remains exact in RAW.

Protection preserves operational state and access to evidence, not unlimited
verbatim residency. All limits here use the request's measured token counter.
"""
import copy
import json
import re
from .common import digest, dumps

REQUIREMENT = re.compile(r'\b(?:must|shall|never|do not|don.t|invariant|hard requirement|required|preserve|only|safety)\b', re.I)
STATE = re.compile(r'^(?:current )?(?:objective|phase|completed|verified|unresolved|blocked|next action|important files)\s*:', re.I)


def evidence_lines(text, query=''):
    """Rank literal lines, including the list following a constraint's colon."""
    words = {w.lower() for w in re.findall(r'[\w./-]{4,}', query)}
    rows = []; continuation = False
    for n, line in enumerate(text.splitlines(), 1):
        value = line.strip()
        if not value:
            continue
        constraint = bool(REQUIREMENT.search(value))
        heading = bool(re.match(r'^(?:#{1,6}\s|\d+[.)]\s|PHASE\s+[A-Z])', value))
        if continuation and heading:
            continuation = False
        score = (100 if STATE.match(value) else 80 if constraint else 70 if continuation else
                 30 if words.intersection(w.lower() for w in re.findall(r'[\w./-]{4,}', value)) else 10 if heading else 0)
        rows.append((score, n, line))
        if constraint:
            continuation = value.endswith(':')
        elif continuation and not (value.startswith(('-', '*', '•')) or len(value) < 140):
            continuation = False
    return rows


def excerpt(engine, message, source, limit, query='', objective=False):
    """Return explicit exact excerpts plus a paginated RAW pointer, never a fake read."""
    original = message.get('content') or ''
    if not isinstance(original, str):
        original = dumps(original)
    text = original
    file_source = None
    if message.get('role') == 'tool':
        try:
            value = json.loads(original)
            if isinstance(value, dict) and isinstance(value.get('content'), str):
                text = value['content']
            if (isinstance(value, dict) and value.get('kind') in
                    ('CURRENT FILE SNAPSHOT', 'FILE SNAPSHOT READ', 'RAW FILE READ')
                    and isinstance(value.get('snapshot_id'), str)):
                # Identity/handles survive further admission reduction. This
                # grants retrieval access, never unbounded prompt protection.
                file_source = {k: value[k] for k in ('path', 'snapshot_id', 'raw_id',
                    'sha256', 'size', 'mtime_ns', 'lines', 'authority') if k in value}
                visible = value.get('excerpt') or {}
                if isinstance(visible.get('content'), str):
                    text = visible['content']
                if isinstance(value.get('content'), str) or isinstance(visible.get('content'), str):
                    file_source['excerpt_range'] = {k: visible[k] for k in
                        ('start_line', 'end_line', 'offset') if k in visible}
                    file_source['excerpt_line_numbers_are_relative'] = True
        except (ValueError, TypeError):
            pass
    rows = evidence_lines(text, query)
    payload = {'source_id': source, 'representation': 'EXCERPT', 'full_source_in_raw': True,
               'read': {'tool': 'rolling_history_read', 'source_id': source, 'offset': 0, 'max_chars': 6000},
               'notice': 'Partial exact source excerpts. Retrieve omitted requirements or code before acting on them; use archive pagination, not another identical read_file.',
               'excerpts': []}
    if file_source:
        payload['file_snapshot'] = file_source
        payload['snapshot_read'] = {'tool': 'rolling_snapshot_read',
                                    'snapshot_id': file_source['snapshot_id']}
        payload['raw_result_read'] = payload['read']
        payload['read'] = payload['snapshot_read']
        payload['notice'] = 'Partial exact file excerpts. Use rolling_snapshot_read for omitted lines or literal search; capture rolling_file_snapshot only to refresh disk.'
    if objective:
        payload['kind'] = 'current_task_source'
        payload['notice'] = 'Task source excerpts; requirements remain binding. The active task state records progress. Read omitted sections from RAW before implementing them.'
    def rendered():
        return '[ARCHIVED SOURCE]\n'+dumps(payload)+'\n[/ARCHIVED SOURCE]'
    # Prefer the title, operational markers, constraints, relevant lines, then
    # the source outline. Keep literal line numbers; do not invent summaries.
    priority = rows[:1]+sorted(rows[1:], key=lambda row: (-row[0], row[1]))
    # Deduplicate repeated reference lines and fit a ranked prefix by binary
    # search. Tokenization is remote in production: O(log n) counts, not a
    # full-template request for each of thousands of source lines.
    seen = set(); entries = []
    for score, number, line in priority:
        if line in seen:
            continue
        seen.add(line)
        entries.append({'line': number, 'text': line})
    lo, hi = 0, len(entries)
    while lo < hi:
        mid = (lo+hi+1)//2
        payload['excerpts'] = entries[:mid]
        if engine.counter.text(rendered()) <= limit: lo = mid
        else: hi = mid-1
    payload['excerpts'] = entries[:lo]
    if not lo and entries:
        entry = entries[0]; line = entry['text']; lo, hi = 0, len(line)
        while lo < hi:
            mid = (lo+hi+1)//2
            payload['excerpts'] = [{**entry, 'text': line[:mid], 'partial_line': True}]
            if engine.counter.text(rendered()) <= limit: lo = mid
            else: hi = mid-1
        payload['excerpts'] = [{**entry, 'text': line[:lo], 'partial_line': True}] if lo else []
    payload['excerpts'].sort(key=lambda row: row['line'])
    result = copy.deepcopy(message)
    result.pop('reasoning_content', None)
    result['content'] = rendered()
    if engine.counter.text(result['content']) > limit:
        result['content'] = f'[Exact RAW source {source}; use rolling_history_read with source_id, offset=0, max_chars=6000. Source excerpt omitted for admission.]'
    return result


def project_objectives(engine, body, hashes, query, capacity):
    display = list(body); projected = {}
    # Per-source and aggregate budgets prevent many old objectives from becoming
    # a second unbounded protected set. Warm/recall get no reserved space.
    limit = min(engine.settings.get('global_budget_tokens', 2048), max(256, capacity//8))
    for i, message in enumerate(body):
        if message.get('role') == 'user' and engine.counter.text(message.get('content') or '') > limit:
            display[i] = excerpt(engine, message, hashes[i], limit, query, objective=True)
            projected[hashes[i]] = engine.counter.text(display[i]['content'])
    return display, projected


def admit(engine, selected, head_count, allowance, recovered, query, source_ids, emergency=False):
    """Enforce the message allowance before selection can reach transport.

    Caller removes warm/stale recall first. Excerpt the largest source payloads
    before cutting a single recent atom. Tool-call IDs and grouping survive.
    """
    result = copy.deepcopy(selected); projections = {}
    recovery = {r['tool_call_id']: r for r in recovered}
    tokens = engine.counter.messages(result)
    for floor in (4096, 2048, 768, 256):
        newest_tool = next((i for i in range(len(result)-1, head_count-1, -1)
                            if result[i].get('role') == 'tool'), None)
        last_user = next((i for i in range(len(result)-1, head_count-1, -1)
                         if result[i].get('role') == 'user'), None)
        newest_start = newest_tool
        if newest_start is not None:
            while newest_start > head_count and result[newest_start-1].get('role') == 'tool':
                newest_start -= 1
        def priority(i):
            if not emergency:
                return (0, -engine.counter.text(dumps(result[i])))
            role = result[i].get('role')
            active_state = (result[i].get('content') or '').startswith('[ACTIVE TASK STATE:')
            fresh_tool = role == 'tool' and newest_start is not None and newest_start <= i <= newest_tool
            rank = 4 if active_state else 3 if i == last_user else 2 if fresh_tool else 0 if role == 'tool' else 1
            return (rank, -engine.counter.text(dumps(result[i])))
        candidates = sorted(range(head_count, len(result)), key=priority)
        if emergency:
            candidates = candidates[:8]  # Bounded last-mile work, not normal circulation.
        for i in candidates:
            if tokens <= allowance:
                return result, tokens, projections
            message = result[i]
            size = engine.counter.text(message.get('content') or '')
            if size <= floor:
                continue
            read = recovery.get(message.get('tool_call_id'))
            source = read['source_id'] if read else source_ids.get(digest(selected[i]))
            if not source:
                continue  # Synthetic task state/summary has no standalone RAW record.
            limit = max(floor, size-(tokens-allowance)-128)
            proposal = excerpt(engine, selected[i], source, limit, query, message.get('role') == 'user')
            result[i] = proposal
            projections[source] = engine.counter.text(proposal['content'])
            if read:
                read['representation'] = 'EXCERPT'; read['tokens'] = projections[source]
            tokens = engine.counter.messages(result)
    # Content cannot bound giant historical tool arguments. Preserve call IDs
    # and names with a RAW pointer; these calls have already executed.
    if tokens > allowance:
        for i in range(head_count, len(result)):
            if not result[i].get('tool_calls'):
                continue
            source = source_ids.get(digest(selected[i]))
            if not source:
                continue
            for call in result[i]['tool_calls']:
                fn = call.get('function') or {}
                if engine.counter.text(fn.get('arguments') or '') > 256:
                    fn['arguments'] = dumps({'archived_arguments_source_id': source,
                                             'read_tool': 'rolling_history_read'})
                    projections[source] = engine.counter.text(dumps(result[i]))
            tokens = engine.counter.messages(result)
            if tokens <= allowance:
                break
    return result, tokens, projections


def compact_protected_group(engine, head, selected, allowance, source_ids, recovered):
    """Bound protected result CONTENT and complete pairs instead of raising."""
    result, tokens, info = bounded_fallback(engine, head, selected[len(head):], allowance)
    return result, tokens, info.get('manifest_source_id')


class _RecoveryCounter:
    """Use the exact counter; fall back to conservative byte counts on failure."""
    def __init__(self, counter):
        self.counter = counter
        self.conservative = False
    def text(self, text):
        if not self.conservative:
            try:
                return self.counter.text(text)
            except Exception:
                self.conservative = True
        text = re.sub(r'[\ud800-\udfff]', '\ufffd', str(text))
        return len(text.encode('utf-8'))
    def messages(self, messages):
        if not self.conservative:
            try:
                return self.counter.messages(messages)
            except Exception:
                self.conservative = True
        return sum(self.text(dumps(m))+256+256*len(m.get('tool_calls') or []) for m in messages)


def bounded_fallback(engine, head, body, allowance, query='', scoped_sources=False):
    """Shared emergency pass over existing RAW, independent of a reusable frame.

    Archive before reduction. Keep instructions, current task and the newest
    complete call/result atom; shrink payloads, then remove older whole atoms.
    """
    from types import SimpleNamespace
    original = copy.deepcopy(body)
    engine.store.snapshot(engine.session_id, 'admission_input', original)
    counter = _RecoveryCounter(engine.counter)
    view = SimpleNamespace(counter=counter)
    def source(message):
        h = digest(message)
        return f'relay-raw:{engine.session_id}:{h}' if scoped_sources else h
    ids = {digest(m): source(m) for m in original}
    manifest = {'role':'assistant', 'content':dumps({'kind':'admission_source_manifest',
        'source_ids':list(dict.fromkeys(ids.values())), 'note':'Exact RAW in original order.'})}
    engine.store.snapshot(engine.session_id, 'admission_manifest', [manifest])
    manifest_id = source(manifest)
    pointer = {'role':'assistant', 'content':f'[Admission recovery: exact omitted RAW manifest {manifest_id}; use rolling_history_read with source_id, offset=0, max_chars=6000.]'}
    # Only explicitly marked optional insertions are removed, never task state.
    optional = ('[HISTORICAL CONTEXT:', '[RECALLED HISTORICAL EVIDENCE:')
    kept = [m for m in original if not (m.get('role') == 'assistant' and
            isinstance(m.get('content'), str) and m['content'].startswith(optional))]
    for m in kept:
        m.pop('reasoning_content', None)
    # Map sanitized copies to their original immutable source.
    for old in original:
        normalized = {k:v for k,v in old.items() if k != 'reasoning_content'}
        ids[digest(normalized)] = source(old)
    # Read the existing explicit task state directly; a broken rendering helper
    # must not remove objective/phase/findings from the emergency request.
    if not any((m.get('content') or '').startswith('[ACTIVE TASK STATE:') for m in kept):
        try:
            with engine.store.connect() as db:
                row = db.execute('SELECT state FROM active_task_state WHERE session=?', (engine.session_id,)).fetchone()
            state = json.loads(row[0]) if row else {}
            fields = ('objective','phase','hard_requirements','completed','findings','files','unresolved','next_action')
            state = {k:[{'text':str(r.get('text',''))[:400], 'source_id':r.get('source_id')}
                        for r in state.get(k,[])[:3] if isinstance(r,dict)] for k in fields}
            if any(state.values()):
                text = '[ACTIVE TASK STATE: explicit prior observations; newer user/RAW takes precedence]\n'+dumps(state)
                if counter.text(text) > 2048:
                    state = {k:rows[:1] for k,rows in state.items()}
                    text = '[ACTIVE TASK STATE: explicit prior observations; full state remains durable]\n'+dumps(state)
                kept.insert(0, {'role':'assistant','content':text})
        except Exception:
            pass  # Original user/task evidence and the RAW manifest still remain.
    candidate = copy.deepcopy(head)+kept+[pointer]
    try:
        candidate, tokens, projections = admit(view, candidate, len(head), allowance, [], query, ids, emergency=True)
    except Exception:
        # No dependent selector/visibility/task-state helper is required here.
        projections = {}
        candidate = copy.deepcopy(head)
        for message in kept:
            value = copy.deepcopy(message); h = ids.get(digest(message), source(message))
            text = value.get('content') or ''
            if len(text) > 1024:
                value['content'] = f'[Exact RAW {h}; rolling_history_read]\n'+text[:1024]
                projections[h] = counter.text(value['content'])
            candidate.append(value)
        candidate.append(pointer); tokens = counter.messages(candidate)
    # Atomize without calling coherent boundaries or any frame-dependent helper.
    body_copy = candidate[len(head):]
    atoms = []; i = 0
    while i < len(body_copy):
        message = body_copy[i]
        if message.get('role') == 'tool':
            i += 1; continue  # No orphan result can be forwarded.
        atom = [message]; i += 1
        if message.get('role') == 'assistant' and message.get('tool_calls'):
            while i < len(body_copy) and body_copy[i].get('role') == 'tool':
                atom.append(body_copy[i]); i += 1
            answered = {m.get('tool_call_id') for m in atom[1:]}
            message['tool_calls'] = [c for c in message['tool_calls'] if c.get('id') in answered]
            valid = {c.get('id') for c in message['tool_calls']}
            atom = [message]+[m for m in atom[1:] if m.get('tool_call_id') in valid]
            if not message['tool_calls']:
                message.pop('tool_calls', None)
        atoms.append(atom)
    last_user = next((a for a in reversed(atoms) if a[0].get('role') == 'user'), None)
    task = [a for a in atoms if (a[0].get('content') or '').startswith('[ACTIVE TASK STATE:')]
    objective = next((a for a in reversed(atoms) if a[0].get('role') == 'user' and len(a[0].get('content') or '') > 80), last_user)
    if objective is not None and objective not in task:
        task.append(objective)
    latest = next((a for a in reversed(atoms) if any(m.get('role') == 'tool' for m in a)), None)
    def flatten(): return copy.deepcopy(head)+[m for a in atoms for m in a]
    candidate = flatten(); tokens = counter.messages(candidate)
    removals = 0
    for atom in list(atoms):
        if tokens <= allowance:
            break
        if atom is last_user or atom is latest or atom in task or atom[-1].get('content') == pointer['content']:
            continue
        atoms.remove(atom); removals += 1
        if removals >= 32:
            atoms = [a for a in atoms if a is last_user or a is latest or a in task or a[-1].get('content') == pointer['content']]
            candidate = flatten(); tokens = counter.messages(candidate)
            break
        candidate = flatten(); tokens = counter.messages(candidate)
    # Newest/protected payloads are shrinkable; identities/pairing are retained.
    for limit in (2048, 768, 256, 64):
        if tokens <= allowance:
            break
        for atom in atoms:
            for message in atom:
                if message.get('content') == pointer['content']:
                    continue
                h = ids.get(digest(message))
                if h is None:
                    # Already projected payloads have a source ID in their marker.
                    h = manifest_id
                if counter.text(message.get('content') or '') > limit:
                    try:
                        reduced = excerpt(view, message, h, limit, query, message.get('role') == 'user')
                        message['content'] = reduced['content']
                    except Exception:
                        message['content'] = f'[Exact RAW {h}; rolling_history_read]\n'+(message.get('content') or '')[:limit]
                for call in message.get('tool_calls') or []:
                    fn = call.get('function') or {}
                    if counter.text(fn.get('arguments') or '') > limit:
                        fn['arguments'] = dumps({'archived_arguments_source_id': h})
            candidate = flatten(); tokens = counter.messages(candidate)
            if tokens <= allowance:
                break
    # Pathological batches may have more call-ID framing than physical space.
    # Remove oldest answered PAIRS together, retaining the newest actionable one.
    if latest and tokens > allowance:
        calls = latest[0].get('tool_calls') or []
        pair_removals = 0
        while len(calls) > 1 and tokens > allowance:
            pair_removals += 1
            if pair_removals >= 32:
                calls[:] = calls[-1:]
                keep_id = calls[0].get('id')
                latest[:] = [latest[0]]+[m for m in latest[1:] if m.get('tool_call_id') == keep_id]
                candidate = flatten(); tokens = counter.messages(candidate)
                break
            removed = calls.pop(0).get('id')
            latest[:] = [latest[0]]+[m for m in latest[1:] if m.get('tool_call_id') != removed]
            candidate = flatten(); tokens = counter.messages(candidate)
    if tokens > allowance:
        # Fixed instructions may themselves be impossible. Return a controlled
        # stop notice; the relay's exact ultimate guard decides admission.
        notice = {'role':'user', 'content':f'[Context admission blocked. Exact task/evidence manifest {manifest_id}; rolling_history_read. Resume only after recovering required evidence.]'}
        candidate = copy.deepcopy(head)+[notice]
        tokens = counter.messages(candidate)
    return candidate, tokens, {'manifest_source_id':manifest_id,
        'conservative_count':counter.conservative, 'bounded':tokens<=allowance}
