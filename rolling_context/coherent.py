"""Append-stable epochs with bounded, demand-driven evidence and coherent cuts.

RAW and completed snapshots are immutable. New evidence is inserted at the latest
request boundary; existing evidence is retained until an occupancy epoch ends.
The legacy moving/stable policies remain available for rollback.
"""
import copy
import json
import time
from .common import digest,dumps,wire_hash,transport_hash
from .selection import _global_anchor
from .pages import terms,identifiers


def coherent_boundaries(body, weights, max_tokens=6000):
    from .engine import safe_boundaries
    safe=[0]+safe_boundaries(body)
    # Primitive atoms include a call and every result. Also keep a result's
    # immediate explanatory response together, including an error/fix report.
    safe=[b for b in safe if not (b and body[b-1].get('role')=='tool' and
          body[b].get('role')=='assistant' and not body[b].get('tool_calls'))]
    cuts=[0];size=0
    for start,end in zip(safe,safe[1:]+[len(body)]):
        n=sum(weights[start:end]);m=body[start]
        natural=m.get('role')=='user'
        # Small request/action clusters remain together; never split an atom to
        # satisfy this soft limit. Oversized atoms are exact and separately cool.
        if start>cuts[-1] and (size+n>max_tokens or natural):
            cuts.append(start);size=0
        size+=n
    return cuts[1:]


def _ready_results(engine, hashes):
    from .summary import validate_memory_summary
    ready = engine.store.ready_compacts(engine.session_id)
    valid = {}; deferred = {}
    for row in ready:
        job = row['ready_job_id']
        if row['id'] is None:
            deferred[job] = 'missing_summary'
            continue
        try:
            coverage = json.loads(row['covered'])
            if not isinstance(coverage, list) or not coverage or not all(isinstance(h, str) for h in coverage):
                raise ValueError('Invalid coverage')
            if row['coverage_kind'] not in ('prefix', 'source_set') or coverage != json.loads(row['job_coverage']):
                deferred[job] = 'invalid_coverage'
                continue
            if not set(coverage).issubset(hashes) or row['coverage_kind'] == 'prefix' and hashes[:len(coverage)] != coverage:
                deferred[job] = 'source_history_changed'
                continue
            if row['coverage_kind'] == 'prefix' and not engine.store.summary_chain(row):
                deferred[job] = 'invalid_lineage'
                continue
            validate_memory_summary(row['text'], set(coverage))
            valid[row['id']] = row
        except (ValueError, TypeError, KeyError):
            deferred[job] = 'invalid_summary'
    return ready, valid, deferred


def select_coherent(engine, head, body, hashes, weights, incoming_message, started):
    from .continuity import TaskState, content_budget, recent_allowance, choose_warm
    from .visibility import ReadVisibility
    from .admission import project_objectives, admit, compact_protected_group
    from .working_set import ElasticWorkingSet, segment_metrics

    if not getattr(engine, '_task_state', None):
        engine._task_state = TaskState(engine)
    if not getattr(engine, '_read_visibility', None):
        engine._read_visibility = ReadVisibility(engine)
    engine._task_state.observe(body, hashes)
    engine._read_visibility.observe(body, hashes)
    groups = engine.pages.macro_groups(engine.session_id, body, hashes, weights,
                                       engine.settings.get('segment_max_tokens', 16000))
    last_user = next((i for i in range(len(body)-1, -1, -1) if body[i].get('role') == 'user'), None)
    incoming = incoming_message.get('content') if isinstance(incoming_message, dict) else incoming_message
    query = incoming or (body[last_user].get('content') if last_user is not None else '') or ''
    elastic = engine.settings.get('elastic') or {}
    if elastic.get('enabled'):
        if not getattr(engine, '_elastic_working_set', None):
            engine._elastic_working_set = ElasticWorkingSet(elastic)
        demand_words = [w for w in terms(query) if w.lower() not in {
            'continue', 'the', 'local', 'city', 'fixture', 'current', 'task', 'please', 'and', 'preserve', 'requirements'}]
        seeds = engine.pages.recall(engine.session_id, query, set(hashes), 2400, relevance_first=True) if demand_words else []
        seed_ids = {r['source_id'] for r in seeds}
        demand = sum(g['tokens'] for g in groups if seed_ids.intersection(g['source_ids'])) + engine.settings['minimum_tail_tokens']
        target, change = engine._elastic_working_set.observe(digest(hashes), engine.settings['target_tokens'], demand, groups[-1]['tokens'])
        if change:
            engine.settings.update(target_tokens=target, trigger_tokens=min(65536, target+4000),
                                   tail_tokens=int(target*.6), recall_budget_tokens=min(28000, int(target*.45)))
            engine._emit('elastic_target_changed', **change)

    budget = content_budget(engine, head)
    capacity = budget['content_capacity_tokens']
    display, objective_projections = project_objectives(engine, body, hashes, query, capacity)
    display_weights = engine.counter.weights(display)
    source_ids = {}
    for message, source in zip(display, hashes):
        normalized = {k: v for k, v in message.items() if k != 'reasoning_content'}
        source_ids[digest(normalized)] = source
    target = min(engine.settings['target_tokens'], capacity)
    trigger = min(max(target, engine.settings['trigger_tokens']), capacity)
    frame = engine._stable_frame
    reusable = bool(frame and frame.get('budget_mode') == 'content' and frame['session'] == engine.session_id
                    and frame['head_hash'] == digest(head) and hashes[:len(frame['seen'])] == frame['seen'])
    previous_tail = sum(display_weights[frame['cut']:len(frame['seen'])]) if reusable else 0
    previous_raw = set(frame.get('raw_sources', hashes[frame['cut']:len(frame['seen'])])) if reusable else set()
    boundaries = [0] + coherent_boundaries(body, display_weights, engine.settings.get('segment_max_tokens', 16000))
    protected = next((b for b in reversed(boundaries) if b < len(body)), 0)
    boundaries = [b for b in boundaries if b <= protected]

    def assemble(candidate):
        result = copy.deepcopy(head) + copy.deepcopy(candidate['prefix'])
        cursor = candidate['cut']
        for position, message in candidate['insertions']:
            if position < cursor:
                continue
            result += copy.deepcopy(display[cursor:position])
            result.append(copy.deepcopy(message)); cursor = position
        result += copy.deepcopy(display[cursor:])
        for message in result:
            message.pop('reasoning_content', None)
        result, recovered = engine._read_visibility.reconcile(result)
        recovered_ids = {r['tool_call_id']: r['source_id'] for r in recovered}
        for message in result:
            source = recovered_ids.get(message.get('tool_call_id')) or source_ids.get(digest(message))
            cached = candidate.get('source_overrides', {}).get(source)
            if cached is not None:
                message['content'] = cached
                source_ids[digest(message)] = source
        for row in recovered:
            if row['source_id'] in candidate.get('source_overrides', {}):
                row['representation'] = 'EXCERPT'
                row['tokens'] = engine.counter.text(candidate['source_overrides'][row['source_id']])
        return result, recovered

    def measure(candidate):
        selected, recovered = assemble(candidate)
        total = engine.counter.messages(selected)
        return selected, max(0, total-budget['system_tokens']), total, recovered

    selected, content, tokens, recovered = measure(frame) if reusable else (None, 0, 0, [])
    epoch_limit = min(capacity, max(trigger, frame.get('epoch_limit', trigger))) if reusable else trigger
    changed = reusable and hashes != frame['seen']
    introduced = sum(display_weights[len(frame['seen']):]) if reusable else 0
    cooldown = max(0, frame.get('burst_cooldown', 0)-int(changed)) if reusable else 0
    quiet = frame.get('burst_quiet_turns', 0) if reusable else 0
    if changed:
        quiet = quiet+1 if introduced <= engine.settings.get('burst_quiet_tokens', 2000) else 0
    settling = bool(reusable and content > trigger and quiet >= engine.settings.get('burst_quiet_turns', 3) and not cooldown)
    rebuild = (not reusable or content > epoch_limit or frame.get('target') != target or settling)
    overrides = copy.deepcopy(frame.get('source_overrides', {})) if reusable else {}
    # A new user request may need different exact excerpts. Ordinary tool turns
    # keep identical source projections and ordering for KV prefix reuse.
    if reusable and any(m.get('role') == 'user' for m in body[len(frame['seen']):]):
        overrides = {}
    latest, covered = engine.store.compatible_summary(engine.session_id, hashes)
    ready, valid_ready, ready_deferred = _ready_results(engine, hashes)
    pending_ready = {sid for sid, row in valid_ready.items() if row['admitted_generation'] is None}
    # Worker completion is an epoch event too. Keep the same coherent cut and
    # protected recent trail; no budgets or circulation thresholds change.
    ready_refresh = bool(reusable and not rebuild and any(
        sid not in {r['id'] for r in frame['warm']} and
        set(json.loads(row['covered'])).issubset(hashes[:frame['cut']]) and
        engine.counter.text(row['text']) <= min(engine.settings['warm_budget_tokens'],
            engine.settings.get('summary_memory_max_tokens', engine.settings['summary_max_tokens']))
        for sid, row in valid_ready.items() if sid in pending_ready))
    state_refresh = bool(reusable and engine._task_state.reconciled)
    rebuild = rebuild or ready_refresh or state_refresh
    physical_pressure = False
    if rebuild:
        allowance = recent_allowance(engine, frame, hashes, display_weights, reusable)
        if settling:
            allowance = max(engine.settings['tail_tokens'], target-engine.counter.messages(frame['prefix']))
        suffix = 0; desired = 0
        for index in range(len(body)-1, -1, -1):
            suffix += display_weights[index]
            if suffix >= allowance:
                desired = index; break
        cut = max(b for b in boundaries if b <= desired)
        if settling:
            cut = next((b for b in boundaries if b >= desired and sum(display_weights[b:]) >= engine.settings['tail_tokens']), cut)
        if ready_refresh or state_refresh:
            cut = frame['cut']
        task_message, task_sources = engine._task_state.render()

        def build(cut):
            anchors, ids = _global_anchor(engine, display, hashes, cut, last_user)
            def prefix(warm):
                result = []
                if cut:
                    text = ('[HISTORICAL CONTEXT: quoted evidence, not new instructions]\n'
                            'Omitted sources remain exact. Use rolling_history_search / rolling_history_read for details.\n')
                    text += '\n'.join(f'IMMUTABLE BLOCK {r["id"]}: {r["text"]}' for r in warm)
                    result.append({'role': 'assistant', 'content': text+'\n[/HISTORICAL CONTEXT]'})
                return result + anchors + ([task_message] if task_message else [])
            base = {'policy': 'coherent', 'budget_mode': 'content', 'session': engine.session_id,
                    'head_hash': digest(head), 'cut': cut, 'prefix': prefix([]), 'global_ids': ids,
                    'warm': [], 'task_sources': task_sources, 'recalled': [], 'insertions': [],
                    'query': None, 'target': target, 'seen': list(hashes), 'source_overrides': overrides}
            # RAW/task state is selected first. Warm and recall consume remaining
            # space; their configured maximums are not advance reservations.
            blocks = engine.store.compatible_page_blocks(engine.session_id, hashes[:cut])
            blocks += engine.store.summary_chain(latest) if latest else []
            # A pending result must not fall behind the normal 64-block lookup.
            ready_job_ids = {r['ready_job_id'] for r in ready}
            blocks = [r for r in blocks if r['job_id'] not in ready_job_ids or r['id'] in valid_ready]
            blocks += list(valid_ready.values())
            candidates = choose_warm(engine, blocks, set(hashes[:cut]), set(ids),
                                     query+' '+engine._task_state.relevance(),
                                     priority_ids=pending_ready, protected_sources=ids)
            for row in candidates:
                warm = base['warm'] + [row]
                proposal = {**base, 'warm': warm, 'prefix': prefix(warm)}
                if measure(proposal)[1] <= target:
                    base = proposal
            return base

        frame = build(cut)
        selected, content, tokens, recovered = measure(frame)
        # A protected source is not an unlimited verbatim pin. Drop optional
        # warm/recall (build admits them only below target), then excerpt source
        # payloads before considering any loss of the recent trail.
        projected_sources = {}
        if content > capacity:
            physical_pressure = True
            selected, tokens, projected_sources = admit(engine, selected, len(head),
                budget['physical_prompt_allowance']-budget['tool_schema_tokens'], recovered, query, source_ids)
            content = max(0, tokens-budget['system_tokens'])
        while content > capacity:
            next_cut = next((b for b in boundaries if b > cut), None)
            if next_cut is None:
                # Even source pointers/message framing cannot fit. Do not return
                # an over-limit protected set; the relay also guards exact wire.
                break  # Final protected-group projection below also bounds framing.
            cut = next_cut; frame = build(cut)
            selected, content, tokens, recovered = measure(frame)
            selected, tokens, projected_sources = admit(engine, selected, len(head),
                budget['physical_prompt_allowance']-budget['tool_schema_tokens'], recovered, query, source_ids)
            content = max(0, tokens-budget['system_tokens'])
        # Avoid a rebuild on every boundary while a necessary atomic burst cools.
        frame['epoch_limit'] = min(capacity, max(trigger, content+max(1, trigger-target)))
    cut = frame['cut']
    query_key = digest(query)
    useful = [w for w in terms(query) if w.lower() not in {
        'the', 'and', 'that', 'with', 'this', 'from', 'have', 'please', 'again', 'should',
        'continue', 'current', 'task', 'fixture', 'implementation', 'preserve', 'earlier',
        'requirements', 'respond', 'only', 'json', 'keys', 'string', 'integer', 'what', 'were', 'must'}]
    recalled_new = []; miss = False; budget_pressure = False
    represented = set(hashes[cut:]) | set(frame['global_ids']) | {r['source_id'] for r in recovered}
    represented |= {h for r in frame['recalled'] for h in r.get('source_ids', [r['source_id']])}
    for row in frame['warm']:
        represented |= {s for items in json.loads(row['text']).values() for item in items for s in item['sources']}
    if useful and not settling and frame['query'] != query_key:
        allowed = set(hashes[:cut]) - represented
        remaining = max(0, engine.settings['recall_budget_tokens'] - sum(r['tokens'] for r in frame['recalled']))
        room = min(remaining, max(0, min(trigger, capacity)-content-384))
        budget_pressure = bool(allowed and room < 256)
        if engine.settings.get('recall_policy') == 'macro':
            recalled_new = engine.pages.recall_segments(engine.session_id, query, body, hashes, weights,
                                                        allowed, room, engine.settings.get('segment_max_tokens', 16000))
        else:
            recalled_new = engine.pages.recall(engine.session_id, query, allowed, room, relevance_first=True)
        miss = bool(allowed and not recalled_new)
        position = last_user if last_user is not None and last_user >= cut else protected
        while recalled_new:
            message = {'role': 'assistant', 'content': '[RECALLED HISTORICAL EVIDENCE: later decisions supersede earlier observations]\n'+dumps(recalled_new)+'\n[/RECALLED HISTORICAL EVIDENCE]'}
            proposal = {**frame, 'insertions': frame['insertions']+[(position, message)]}
            if measure(proposal)[1] <= min(trigger, capacity):
                frame['insertions'].append((position, copy.deepcopy(message)))
                frame['recalled'] += recalled_new
                break
            recalled_new.pop(); budget_pressure = True
        frame['query'] = query_key
    selected, content, tokens, recovered = measure(frame)
    physical_pressure = physical_pressure or content > capacity
    unprojected = copy.deepcopy(selected)
    message_allowance = budget['physical_prompt_allowance']-budget['tool_schema_tokens']
    if content > capacity:
        # Leave append headroom so a physical burst does not rewrite the same
        # excerpt on each small tool turn while the quiet timer is running.
        message_allowance -= min(max(0, trigger-target), max(0, capacity-target))
    if settling:
        message_allowance = min(message_allowance, target+budget['system_tokens'])
    selected, tokens, projected_sources = admit(engine, selected, len(head),
        message_allowance, recovered, query, source_ids)
    recovered_ids = {r['tool_call_id']: r['source_id'] for r in recovered}
    for original, projected in zip(unprojected, selected):
        source = recovered_ids.get(original.get('tool_call_id')) or source_ids.get(digest(original))
        if source and projected.get('content') != original.get('content'):
            frame['source_overrides'][source] = projected['content']
    for source, text in frame.get('source_overrides', {}).items():
        if source in hashes[frame['cut']:] or source in recovered_ids.values():
            projected_sources[source] = engine.counter.text(text)
    frame['burst_quiet_turns'] = 0 if settling else quiet
    frame['burst_cooldown'] = engine.settings.get('burst_cooldown_turns', 3) if settling else cooldown
    if settling:
        frame['epoch_limit'] = trigger

    content = max(0, tokens-budget['system_tokens'])
    admission_manifest = None
    if tokens+budget['tool_schema_tokens'] > budget['physical_prompt_allowance']:
        selected, tokens, admission_manifest = compact_protected_group(engine, head, selected,
            budget['physical_prompt_allowance']-budget['tool_schema_tokens'], source_ids, recovered)
        content = max(0, tokens-budget['system_tokens'])
    if tokens+budget['tool_schema_tokens'] > budget['physical_prompt_allowance']:
        from .admission import bounded_fallback
        selected, tokens, recovery = bounded_fallback(engine, head, body,
            budget['physical_prompt_allowance']-budget['tool_schema_tokens'], query)
        admission_manifest = recovery.get('manifest_source_id')
        content = max(0, tokens-budget['system_tokens'])
        engine._emit('admission_fallback', bounded=recovery.get('bounded', False),
                     reason='protected_prompt_over_capacity')
    projected_sources.update({h: n for h, n in objective_projections.items()
                              if h in hashes[frame['cut']:] or h in frame['global_ids']})
    frame['seen'] = list(hashes); engine._stable_frame = frame
    # Receipts and telemetry describe the final selected prompt, including
    # emergency admission which may have removed optional compact blocks.
    warm = [r for r in frame['warm'] if any(
        f'IMMUTABLE BLOCK {r["id"]}: {r["text"]}' in (m.get('content') or '') for m in selected)]
    recalled = frame['recalled']
    ledger = [{'page_id': h, 'source_id': h, 'representation': 'RAW', 'reason': 'coherent_recent'} for h in dict.fromkeys(hashes[cut:])]
    ledger += [{'page_id': h, 'source_id': h, 'representation': 'RAW', 'reason': 'global_objective'} for h in frame['global_ids']]
    ledger += [{'page_id': r['page_id'], 'source_id': h, 'representation': r['representation'], 'version': r['version'], 'reason': 'recall'} for r in recalled for h in dict.fromkeys(r.get('source_ids', [r['source_id']]))]
    for row in warm:
        ledger += [{'page_id': f'compact:{row["id"]}', 'source_id': h, 'representation': 'COMPACT', 'version': row['id'], 'reason': 'warm_block'} for h in {s for items in json.loads(row['text']).values() for item in items for s in item['sources']}]
    ledger += [{'page_id': 'active-task', 'source_id': h, 'representation': 'FACTS', 'reason': 'active_task_state'} for h in frame.get('task_sources', [])]
    for read in recovered:
        for row in ledger:
            if row.get('source_id') == read['stub_source_id']:
                row['representation'] = 'REHYDRATED'; row['reason'] = 'dedup_stub_replaced'
        ledger.append({'page_id': read['source_id'], 'source_id': read['source_id'], 'representation': read.get('representation', 'RAW'), 'reason': 'rehydrated_read'})
        engine._emit('read_result_rehydrated', source_id=read['source_id'], tool_call_id=read['tool_call_id'], tokens=read['tokens'])
    for row in ledger:
        if row.get('source_id') in projected_sources and row['representation'] == 'RAW':
            row['representation'] = 'EXCERPT'
    ledger += [{'page_id': digest(m), 'representation': 'RAW', 'reason': 'pinned_instructions'} for m in head]
    if admission_manifest:
        for row in ledger:
            if row.get('source_id'):
                row['representation'] = 'INDEX'
                projected_sources[row['source_id']] = 0
        ledger.append({'page_id': admission_manifest, 'source_id': admission_manifest,
                       'representation': 'INDEX', 'reason': 'admission_source_manifest'})
    rh = wire_hash(selected)
    generation = engine.store.generation(engine.session_id, rh, [r['id'] for r in warm], cut, ledger, transport_alias=transport_hash(selected))
    admitted = engine.store.admit_compacts(engine.session_id, [r['id'] for r in warm], generation)
    for sid in admitted:
        row = next(r for r in warm if r['id'] == sid)
        engine._emit('summary_applied', summary_id=sid, job_id=row['job_id'], generation=generation,
                     source_ids=json.loads(row['covered']), reason='coherent_compact_admission')
    admission_state = []
    selected_ids = {r['id'] for r in warm}
    selected_sources = {h for r in warm for items in json.loads(r['text']).values() for i in items for h in i['sources']}
    for row in ready:
        sid = row['id']; reason = ready_deferred.get(row['ready_job_id'])
        if sid in selected_ids:
            state = 'admitted' if sid in admitted else 'retained'
        else:
            state = 'deferred'
            if reason is None:
                if row['admitted_generation'] is not None:
                    reason = 'previously_admitted_not_required'
                elif not set(json.loads(row['covered'])).issubset(hashes[:cut]):
                    reason = 'protected_recent_sources'
                elif engine.counter.text(row['text']) > engine.settings.get('summary_memory_max_tokens', engine.settings['summary_max_tokens']):
                    reason = 'memory_budget'
                elif engine.counter.text(row['text']) > engine.settings['warm_budget_tokens']:
                    reason = 'warm_budget'
                elif any(r['id'] == sid for r in frame['warm']):
                    reason = 'physical_admission_removed'
                elif len(warm) >= engine.settings.get('warm_max_blocks', 2):
                    reason = 'warm_slot_limit'
                else:
                    sources = {h for items in json.loads(row['text']).values() for i in items for h in i['sources']}
                    reason = 'overlapping_compact_sources' if (sources-set(frame['global_ids'])) & selected_sources else 'content_target_or_admission_budget'
        admission_state.append({'job_id':row['ready_job_id'], 'summary_id':sid,
                                'state':state, 'reason':reason, 'generation':generation})
    active = {r['source_id'] for r in ledger if 'source_id' in r}
    recall_ids = {h for r in recalled for h in r.get('source_ids', [r['source_id']])} | {r['source_id'] for r in recovered}
    evicted = engine.pages.cool_others(engine.session_id, active, generation)
    engine.pages.state(engine.session_id, active-recall_ids, 'ACTIVE', generation)
    engine.pages.state(engine.session_id, recall_ids, 'RECALL', generation)
    engine._last_message_tokens = tokens
    engine._latest_plan = (engine.session_id, copy.deepcopy(body), hashes, weights, tokens+engine._reserve(), cut)
    if sum(weights[:cut]) >= engine.settings['chunk_min']:
        engine._schedule(body, hashes, weights, latest, covered, tokens+engine._reserve(), max_end=cut)
    metrics, engine._segment_observation = segment_metrics(groups, ledger, dict(zip(hashes, weights)),
        getattr(engine, '_segment_observation', None), extra_units=
        [{'id': r['page_id'], 'tokens': engine.counter.text(dumps(r))} for r in recalled] +
        [{'id': f'compact:{r["id"]}', 'tokens': engine.counter.text(r['text'])} for r in warm] +
        [{'id': r['source_id'], 'tokens': r['tokens']} for r in recovered], raw_start=cut, anchor_ids=frame['global_ids'])
    current_raw = set(hashes[cut:]) - set(projected_sources)
    frame['raw_sources'] = list(current_raw)
    visible_tail = sum(projected_sources.get(h, w) for h, w in zip(hashes[cut:], display_weights[cut:]))
    source_weights = dict(zip(hashes, weights))
    raw_retained = sum(source_weights.get(h, 0) for h in current_raw & previous_raw)
    raw_previous = sum(source_weights.get(h, 0) for h in previous_raw)
    engine._emit('selection', **metrics, **budget, request_hash=rh, generation=generation,
        context_generation=generation, active_tokens=tokens+budget['tool_schema_tokens']+1024,
        estimated_transport_prompt_tokens=tokens+budget['tool_schema_tokens'], content_tokens=content,
        target_tokens=target, trigger_tokens=trigger, configured_content_target=engine.settings['target_tokens'],
        configured_content_trigger=engine.settings['trigger_tokens'], budget_mode='content',
        raw_history_tokens=sum(weights), message_tokens=tokens, overhead_reserve=engine._reserve(),
        resident_pages=len({r['page_id'] for r in ledger}), recalled_pages=len(recall_ids), evicted_pages=len(evicted),
        raw_pages=sum(r['representation']=='RAW' for r in ledger), compact_pages=sum(r['representation']=='COMPACT' for r in ledger),
        facts_pages=sum(r['representation']=='FACTS' for r in ledger), page_ids=ledger, evicted_page_ids=evicted,
        tail_tokens=visible_tail, tail_source_tokens=sum(weights[cut:]), previous_tail_tokens=previous_tail, cold_messages=cut,
        warm_blocks=[r['id'] for r in warm], warm_tokens=sum(engine.counter.text(r['text']) for r in warm),
        selection_policy='coherent', prefix_rebuilt=rebuild, applied=bool(admitted),
        newly_admitted_blocks=admitted, compact_blocks_active=bool(warm), ready_compaction_admission=admission_state,
        ready_compaction_refresh=ready_refresh, task_state_reconciled=state_refresh,
        recall_new_pages=len(recalled_new), recall_miss=miss, recall_budget_pressure=budget_pressure,
        rehydrated_reads=len(recovered), content_burst=content>target, protected_burst=content>trigger,
        physical_continuity_pressure=physical_pressure, physical_budget_exceeded=content>capacity,
        continuity_raw_retained_fraction=raw_retained/max(1,raw_previous) if previous_raw else 1,
        continuity_raw_retained_tokens=raw_retained,
        continuity_raw_jaccard=len(current_raw & previous_raw)/max(1,len(current_raw | previous_raw)) if previous_raw else 1,
        task_state_sources=frame.get('task_sources', []),
        protected_content_budget_tokens=capacity, projected_source_tokens=projected_sources,
        admission_source_manifest=admission_manifest,
        burst_settling=settling, burst_quiet_turns=frame['burst_quiet_turns'],
        burst_cooldown_turns=frame['burst_cooldown'],
        cached_source_projections=len(frame.get('source_overrides', {})),
        content_capacity_clamped=engine.settings['target_tokens']>capacity,
        epoch_content_limit=frame.get('epoch_limit', trigger),
        fast_loop_ms=round((time.monotonic()-started)*1000, 3), page_cache=engine.pages.status(engine.session_id),
        **engine.store.job_status(engine.session_id))
    return selected
