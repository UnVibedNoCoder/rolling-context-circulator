"""Optional independent cold-chunk scheduling; foreground admission never waits."""
import os
import time
import json

from .common import dumps


def schedule_cold(engine, body, hashes, weights, active_tokens, cold_end):
    from .engine import safe_boundaries, spawn_context_thread

    if cold_end <= 0:
        return
    with engine.store.connect() as db:
        jobs = [dict(row) for row in db.execute('SELECT * FROM jobs WHERE session=?', (engine.session_id,))]
        for row in jobs:
            if row['status'] not in ('queued', 'running'):
                continue
            try:
                os.kill(row['owner'], 0)  # Existence check only; never signals another process.
                alive = True
            except ProcessLookupError:
                alive = False
            if not alive or time.time()-row['updated'] >= engine.settings['job_timeout']+60:
                db.execute("UPDATE jobs SET status='interrupted',updated=? WHERE id=?", (time.time(), row['id']))
                row['status'] = 'interrupted'
    if any(row['status'] in ('queued', 'running') and time.time()-row['updated'] < engine.settings['job_timeout']+60 for row in jobs):
        return
    finished = {row['coverage'] for row in jobs if row['coverage_kind'] == 'source_set'
                and row['status'] in ('ready', 'queued', 'running')}
    occupied = {h for value in finished for h in json.loads(value)}
    if engine.settings.get('segmentation_policy') == 'coherent':
        with engine.store.connect() as db:
            occupied |= {r[0] for r in db.execute("SELECT source_id FROM pages WHERE session=? AND state IN ('ACTIVE','RECALL')",(engine.session_id,))}
    failed = {}
    for row in jobs:
        if row['coverage_kind'] == 'source_set' and row['status'] in ('failed', 'interrupted'):
            failed[row['coverage']] = failed.get(row['coverage'], 0)+1
    if engine.settings.get('segmentation_policy') == 'coherent':
        from .coherent import coherent_boundaries
        ends = coherent_boundaries(body, weights, engine.settings.get('segment_max_tokens', 6000))
    else:
        ends = safe_boundaries(body)
    boundaries = [0]+[end for end in ends if end <= cold_end]
    candidates = []
    oversized = 0
    prefix = [0]
    for weight in weights:
        prefix.append(prefix[-1]+weight)
    for n, start in enumerate(boundaries[:-1]):
        if prefix[boundaries[n+1]]-prefix[start] > engine.settings['chunk_max']:
            oversized += 1
        for end in boundaries[n+1:]:
            if engine.settings.get('segmentation_policy') == 'coherent' and any(m.get('role')=='user' for m in body[start+1:end]):
                break  # Never join unrelated task episodes merely to fill a token target.
            tokens = prefix[end]-prefix[start]
            if tokens > engine.settings['chunk_max']:
                break
            coverage = dumps(hashes[start:end])
            if tokens >= engine.settings['chunk_min'] and coverage not in finished and failed.get(coverage, 0) < 2:
                # No overlap with ready/running chunks; preserve independent provenance.
                if not occupied.intersection(hashes[start:end]):
                    candidates.append((start, abs(tokens-engine.settings['chunk_target']), end, tokens, coverage))
    if not candidates:
        engine._emit('chunk_unavailable', active_tokens=active_tokens, oversized_groups=oversized,
                     reason='No eligible independent cold chunk; RAW remains exact')
        return
    start, _, end, tokens, coverage = min(candidates)
    now = time.time()
    with engine.store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if db.execute("SELECT 1 FROM jobs WHERE session=? AND status IN ('queued','running')",(engine.session_id,)).fetchone():
            return
        fresh_ready=[r[0] for r in db.execute("SELECT coverage FROM jobs WHERE session=? AND status='ready' AND coverage_kind='source_set'",(engine.session_id,))]
        if any(set(json.loads(value)).intersection(hashes[start:end]) for value in fresh_ready):
            return
        cursor = db.execute("INSERT INTO jobs(session,created,updated,status,owner,coverage,parent_id,chunk,trigger_tokens,chunk_tokens,coverage_kind) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (engine.session_id, now, now, 'queued', os.getpid(), coverage, None,
             dumps(body[start:end]), active_tokens, tokens, 'source_set'))
        job_id = cursor.lastrowid
    engine._emit('compaction_queued', job_id=job_id, trigger_tokens=active_tokens,
                 chunk_tokens=tokens, chunk_messages=end-start, coverage_kind='source_set',
                 oversized_groups=oversized)
    engine.pages.state(engine.session_id, hashes[start:end], 'COMPACTING')
    session = engine.session_id
    engine._worker = spawn_context_thread(lambda: engine._compact(job_id, session),
        name=f'rolling-compact-{job_id}', daemon=True)
    engine._worker.start()
