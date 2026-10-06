"""Request-specific schema measurements; no-tool traffic cannot erase tools."""
import fcntl
import json
import os
import time
import uuid
from pathlib import Path
from .common import digest


def schema_identity(body):
    tools = body.get('tools') or []
    functions = body.get('functions') or []
    value = {k: body[k] for k in ('tool_choice', 'parallel_tool_calls', 'function_call', 'chat_template_kwargs') if k in body}
    value.update(tools=sorted(tools, key=digest), functions=sorted(functions, key=digest))
    return digest(value), bool(tools or functions)


def read_cache(root, profile):
    try:
        value = json.loads((Path(root)/f'{profile}-overhead.json').read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def write_measurement(root, profile, overhead_tokens, **measurement):
    root = Path(root); root.mkdir(parents=True, exist_ok=True)
    path = root/f'{profile}-overhead.json'
    # Serialize read/merge/replace across relay threads and processes.
    with (root/f'.{profile}-overhead.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = read_cache(root, profile)
        tokens = max(0, int(overhead_tokens))
        has_tools = measurement.get('has_tools', tokens > 0)
        row = {'profile': profile, 'overhead_tokens': tokens, 'measured_at': time.time(),
               **measurement, 'has_tools': has_tools}
        records = data.get('measurements', {})
        # Import a valid old single-value cache before writing an auxiliary row.
        if not records and data.get('overhead_tokens', 0) > 0:
            records['legacy'] = {**data, 'has_tools': True}
        key = digest([row.get('session_id'), row.get('request_class'), row.get('schema_fingerprint')])
        records[key] = row
        # Auxiliary floods must never age the valid tool-bearing cache out.
        positive = sorted(((k, v) for k, v in records.items() if v.get('has_tools') and v.get('overhead_tokens',0)>0), key=lambda kv: kv[1].get('measured_at',0))[-256:]
        other = sorted(((k, v) for k, v in records.items() if not (v.get('has_tools') and v.get('overhead_tokens',0)>0)), key=lambda kv: kv[1].get('measured_at',0))[-64:]
        excluded = not (has_tools and tokens > 0)
        if not excluded:
            data.update(row)  # Backward-compatible main measurement stays positive.
        data.update(version=2, measurements=dict(positive+other), last_measurement=row,
                    auxiliary_excluded_from_main=excluded)
        temp = root/f'.{profile}-overhead-{uuid.uuid4().hex}.tmp'
        try:
            fd = os.open(temp, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w') as handle:
                json.dump(data, handle); handle.flush(); os.fsync(handle.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
    return {'schema_measurement_source': 'relay_wire', 'schema_measurement_kind': 'exact',
            'schema_fingerprint': row.get('schema_fingerprint'),
            'schema_request_class': row.get('request_class'),
            'auxiliary_excluded_from_main': excluded}


def schema_budget(engine):
    data = read_cache(engine.store.root, engine.settings['profile'])
    fp = engine.settings.get('tool_schema_fingerprint')
    meta = {'schema_fingerprint': fp, 'schema_measurement_source': 'conservative_fallback',
            'schema_measurement_kind': 'conservative',
            'auxiliary_excluded_from_main': data.get('auxiliary_excluded_from_main', False)}
    if engine.settings.get('tools_enabled') is False:
        return 0, {**meta, 'schema_measurement_source': 'explicit_tools_disabled',
                   'schema_measurement_kind': 'exact'}, {}
    records = list(data.get('measurements', {}).values())
    if not records and data.get('overhead_tokens',0)>0:
        records = [data]
    valid = [r for r in records if r.get('has_tools', True) and r.get('overhead_tokens',0)>0]
    session = engine.session_id
    request_class = engine.settings.get('schema_request_class')
    def latest(rows): return max(rows, key=lambda r:r.get('measured_at',0), default=None)
    matching = [r for r in valid if fp and r.get('schema_fingerprint') == fp
                and (not request_class or r.get('request_class') == request_class)]
    row = latest([r for r in matching if r.get('session_id') == session]) or latest(matching)
    source = 'schema_fingerprint'
    if row is None:
        row = latest([r for r in valid if r.get('session_id') == session])
        source = 'session_tool_bearing'
    if row is None:
        row = latest(valid); source = 'profile_tool_bearing'
    if row is not None:
        return int(row['overhead_tokens']), {**meta, 'schema_measurement_source': source,
            'schema_measurement_kind': 'cached', 'schema_fingerprint': row.get('schema_fingerprint'),
            'requested_schema_fingerprint': fp}, row
    # Unknown is never zero. This is configurable policy, not a learned schema size.
    fallback = max(1, engine.settings.get('schema_fallback_tokens', 16384),
                   engine.settings.get('overhead_reserve',8192)-1024)
    return fallback, meta, {}
