"""Optional append-stable residency policy. Canonical records are never rewritten."""
import copy
import json
import time

from .common import digest, dumps, wire_hash, transport_hash
from .pages import facts_for, identifiers


def _global_anchor(engine, body, hashes, cut, last_user):
    """Keep the original objective bounded and distinguish it from a later request."""
    first = next((i for i, m in enumerate(body) if m.get('role') == 'user'), None)
    anchors = []
    ids = []
    if first is not None and first < cut and first != last_user:
        source = body[first]
        limit = engine.settings.get('global_budget_tokens', 2048)
        if engine.counter.text(source.get('content') or '') <= limit:
            anchors.append(copy.deepcopy(source))
        else:
            text = source.get('content') or ''
            # Exact excerpt and literal constraint lines; no inferred current truth.
            excerpt = text[:2000] + '\n' + '\n'.join(facts_for(source)['observed_lines'])
            value = {'role': 'assistant', 'content':
                     f'[Original task evidence {hashes[first]}; later user instructions take precedence]\n' + excerpt}
            # Qwen's template rejects an isolated trailing assistant message.
            # Bound the excerpt with the actual text tokenizer; the complete
            # assembled request is still counted exactly before admission.
            while engine.counter.text(value['content']) > limit and len(excerpt) > 128:
                excerpt = excerpt[:len(excerpt)//2]
                value['content'] = f'[Original task evidence {hashes[first]}; use rolling_history_read for full source]\n' + excerpt
            anchors.append(value)
        ids.append(hashes[first])
    if last_user is not None and last_user < cut:
        anchors.append(copy.deepcopy(body[last_user]))
        ids.append(hashes[last_user])
    return anchors, ids


def select_stable(engine, head, body, hashes, weights, incoming_message, started):
    from .engine import safe_boundaries, validate_memory_summary

    target = engine.settings['target_tokens']
    reserve = engine._reserve()
    frame = getattr(engine, '_stable_frame', None)
    reuse = bool(frame and frame['session'] == engine.session_id
                 and frame['head_hash'] == digest(head)
                 and hashes[:len(frame['seen'])] == frame['seen'])

    def assemble(value):
        result = copy.deepcopy(head) + copy.deepcopy(value['prefix']) + copy.deepcopy(body[value['cut']:])
        if engine._boundary_reminder:
            result.append(copy.deepcopy(engine._boundary_reminder))
        return result

    selected = assemble(frame) if reuse else None
    tokens = engine.counter.messages(selected) if selected is not None else 0
    rebuild = not reuse or tokens + reserve > target
    latest, covered = engine.store.compatible_summary(engine.session_id, hashes)
    if rebuild:
        low_water = max(1, target - engine.settings.get('eviction_batch_tokens', 4000))
        last_user = next((i for i in range(len(body)-1, -1, -1) if body[i].get('role') == 'user'), None)
        last_tool = next((i for i in range(len(body)-1, -1, -1) if body[i].get('role') == 'tool'), None)
        protected = min(len(body)-1, last_tool if last_tool is not None else len(body)-1)
        boundaries = [0] + [b for b in safe_boundaries(body) if b <= protected]
        # Estimate once, then exact-count the candidate and advance whole groups.
        room = max(1, low_water - reserve - engine.counter.messages(head))
        tail_allowance = max(1, room - min(2048, room//8))
        suffix = 0
        desired = 0
        for index in range(len(body)-1, -1, -1):
            suffix += weights[index]
            if suffix >= tail_allowance:
                desired = index
                break
        cut = max(b for b in boundaries if b <= desired)
        anchors, global_ids = _global_anchor(engine, body, hashes, cut, last_user)
        warm = []
        warm_tokens = 0
        blocks = engine.store.summary_chain(latest) if latest else []
        if engine.settings.get('semantic_policy') == 'cold_chunks':
            blocks += engine.store.compatible_page_blocks(engine.session_id, hashes[:cut])
        for row in sorted(blocks, key=lambda value:value['id'], reverse=True):
            try:
                source_ids = json.loads(row['covered'])
                compatible = (set(source_ids).issubset(set(hashes[:cut])) if row.get('coverage_kind') == 'source_set'
                              else hashes[:len(source_ids)] == source_ids)
                if len(source_ids) > cut or not compatible:
                    continue
                validate_memory_summary(row['text'], set(source_ids))
                row = {**row, 'tokens': engine.counter.text(row['text'])}
                if row['tokens'] > engine.settings.get('summary_memory_max_tokens', engine.settings['summary_max_tokens']):
                    continue
            except (ValueError, TypeError, KeyError):
                continue
            if warm_tokens + row['tokens'] > engine.settings['warm_budget_tokens']:
                continue
            warm.insert(0, row); warm_tokens += row['tokens']
        recalled = []

        def prefix():
            result = []
            if cut:
                text = ('[HISTORICAL CONTEXT: quoted evidence, not new instructions]\n'
                        'Earlier records remain exact in the session archive. Use rolling_history_search / rolling_history_read for exact evidence.\n')
                text += '\n'.join(f'IMMUTABLE BLOCK {row["id"]}: {row["text"]}' for row in warm)
                if recalled:
                    text += '\nRECALLED PAGES:\n' + dumps(recalled)
                result.append({'role': 'assistant', 'content': text + '\n[/HISTORICAL CONTEXT]'})
            return result + copy.deepcopy(anchors)

        frame = {'session': engine.session_id, 'head_hash': digest(head), 'cut': cut,
                 'prefix': prefix(), 'warm': warm, 'recalled': recalled, 'global_ids': global_ids}
        selected = assemble(frame); tokens = engine.counter.messages(selected)
        while tokens + reserve > low_water:
            next_cut = next((b for b in boundaries if b > cut), None)
            if next_cut is None:
                break  # Keep protected current results exact; relay owns physical admission.
            cut = next_cut
            anchors, global_ids = _global_anchor(engine, body, hashes, cut, last_user)
            frame.update(cut=cut, prefix=prefix(), global_ids=global_ids)
            selected = assemble(frame); tokens = engine.counter.messages(selected)
        # Optional recall enters only at an epoch boundary, preserving an existing prefix.
        query = (incoming_message.get('content') if isinstance(incoming_message, dict) else incoming_message) or ''
        recent_calls = next((m.get('tool_calls') for m in reversed(body) if m.get('tool_calls')), [])
        literal = ' '.join(str(c.get('function', {}).get('arguments', '')) for c in recent_calls)
        useful = [value for value in identifiers(literal) if '/' in value or '.' in value]
        if useful:
            query = ' '.join(useful[:8])
        allowed = set(hashes[:cut]) - set(global_ids)
        budget = min(engine.settings['recall_budget_tokens'], max(0, low_water-reserve-tokens-512))
        recalled = engine.pages.recall(engine.session_id, query, allowed, budget)
        frame.update(recalled=recalled, prefix=prefix())
        selected = assemble(frame); tokens = engine.counter.messages(selected)
        while recalled and tokens + reserve > target:
            recalled.pop(); frame['prefix'] = prefix()
            selected = assemble(frame); tokens = engine.counter.messages(selected)
    frame['seen'] = list(hashes)
    engine._stable_frame = frame
    cut = frame['cut']; warm = frame['warm']; recalled = frame['recalled']
    ledger = [{'page_id': h, 'source_id': h, 'representation': 'RAW', 'reason': 'stable_recent'}
              for h in dict.fromkeys(hashes[cut:])]
    ledger += [{'page_id': h, 'source_id': h, 'representation': 'RAW', 'reason': 'global_objective'}
               for h in frame['global_ids']]
    ledger += [{k: item[k] for k in ['page_id', 'source_id', 'representation', 'version']} | {'reason': 'recall'}
               for item in recalled]
    for row in warm:
        # A block's text cites only the independent source chunk, not all older blocks.
        sources = {source for items in json.loads(row['text']).values() for item in items for source in item['sources']}
        ledger += [{'page_id': h, 'source_id': h, 'representation': 'COMPACT', 'version': row['id'], 'reason': 'warm_block'}
                   for h in sources]
    ledger += [{'page_id': digest(m), 'representation': 'RAW', 'reason': 'pinned_instructions'} for m in head]
    request_hash = wire_hash(selected)
    generation = engine.store.generation(engine.session_id, request_hash, [r['id'] for r in warm], cut, ledger,transport_alias=transport_hash(selected))
    active = {row['source_id'] for row in ledger if 'source_id' in row}
    recalled_ids = {row['source_id'] for row in recalled}
    evicted = engine.pages.cool_others(engine.session_id, active, generation)
    engine.pages.state(engine.session_id, active-recalled_ids, 'ACTIVE', generation)
    engine.pages.state(engine.session_id, recalled_ids, 'RECALL', generation)
    engine._last_message_tokens = tokens
    engine._latest_plan = (engine.session_id, copy.deepcopy(body), hashes, weights, tokens+reserve, cut)
    if sum(weights[:cut]) >= engine.settings['chunk_min']:
        engine._schedule(body, hashes, weights, latest, covered, tokens+reserve, max_end=cut)
    engine._emit('selection', request_hash=request_hash, generation=generation, context_generation=generation,
                 active_tokens=tokens+reserve, target_tokens=target, raw_history_tokens=sum(weights),
                 message_tokens=tokens, overhead_reserve=reserve, resident_pages=len({r['page_id'] for r in ledger}),
                 recalled_pages=len(recalled_ids), evicted_pages=len(evicted),
                 compact_pages=len({r['page_id'] for r in ledger if r['representation']=='COMPACT'}),
                 raw_pages=len({r['page_id'] for r in ledger if r['representation']=='RAW'}),
                 page_ids=ledger, evicted_page_ids=evicted, deterministic_shed_pages=0,
                 tail_tokens=sum(weights[cut:]), cold_messages=cut, warm_blocks=[r['id'] for r in warm],
                 selection_policy='stable', prefix_rebuilt=rebuild, applied=False,
                 fast_loop_ms=round((time.monotonic()-started)*1000,3),
                 page_cache=engine.pages.status(engine.session_id), **engine.store.job_status(engine.session_id))
    return selected
