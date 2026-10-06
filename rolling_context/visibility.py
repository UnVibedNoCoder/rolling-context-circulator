"""Rehydrate an invisible read result when Hermes returns a dedup refusal.

This adapts selected request copies, never canonical history or Hermes code.
Successful reads and dedup refusals gain current snapshots; unrelated failures
pass through unchanged. Historical RAW is explicitly labeled when disk is unavailable.
Recovery projections are frozen per refusal, preserving ordinary prefix stability.
"""
import copy
import json
from .common import digest, dumps


def read_key(call):
    function = call.get('function') or {}
    if function.get('name') != 'read_file':
        return None
    try:
        args = json.loads(function.get('arguments') or '{}')
        path = args['path']
        if not isinstance(path, str):
            return None
        # Defaults are the documented Hermes read_file API, not token tuning.
        return dumps([path, int(args.get('offset', 1)), int(args.get('limit', 2000))])
    except (ValueError, TypeError, KeyError):
        return None


def result_value(message):
    try:
        value = json.loads(message.get('content') or '')
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def dedup_refusal(value):
    return ((value.get('dedup') is True and value.get('content_returned') is False and value.get('status') == 'unchanged') or
            (value.get('already_read', 0) >= 3 and isinstance(value.get('error'), str) and
             value['error'].startswith('BLOCKED: You have called read_file') and 'earlier' in value['error']))


class ReadVisibility:
    def __init__(self, engine):
        self.engine = engine
        self.seen = []
        self.calls = {}
        self.recoveries = {}
        with engine.store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS archived_file_reads(session TEXT, region TEXT, source_id TEXT, PRIMARY KEY(session,region))')
            self.reads = {r['region']: r['source_id'] for r in db.execute('SELECT * FROM archived_file_reads WHERE session=?', (engine.session_id,))}

    def observe(self, body, hashes):
        start = len(self.seen) if hashes[:len(self.seen)] == self.seen else 0
        if start == 0:
            self.calls = {}
        with self.engine.store.connect() as db:
            for message, source in zip(body[start:], hashes[start:]):
                for call in message.get('tool_calls') or []:
                    key = read_key(call)
                    if key:
                        self.calls[call.get('id')] = key
                if message.get('role') != 'tool':
                    continue
                key = self.calls.get(message.get('tool_call_id'))
                value = result_value(message)
                if key and isinstance(value.get('content'), str) and not value.get('error') and not dedup_refusal(value):
                    self.reads[key] = source
                    db.execute('INSERT OR REPLACE INTO archived_file_reads VALUES (?,?,?)', (self.engine.session_id, key, source))
        self.seen = list(hashes)

    def reconcile(self, selected):
        calls = {}
        latest = {}
        for index, message in enumerate(selected):
            for call in message.get('tool_calls') or []:
                key = read_key(call)
                if key:
                    calls[call.get('id')] = key
            if message.get('role') != 'tool':
                continue
            key = calls.get(message.get('tool_call_id'))
            if not key:
                continue
            value = result_value(message)
            if dedup_refusal(value) or (isinstance(value.get('content'), str) and not value.get('error')):
                latest[key] = index
        recovered = []
        for key, index in latest.items():
            value = result_value(selected[index])
            stub_source = digest(selected[index])
            recovery = self.recoveries.get(stub_source)
            if recovery is None:
                # Freeze each result projection after its first disk validation.
                # New read calls validate again; archive reads never touch disk.
                from .file_reads import ordinary_read
                path, offset, limit = json.loads(key)
                try:
                    payload = ordinary_read(self.engine, path, offset, limit)
                except (OSError, ValueError, TypeError) as error:
                    source = self.reads.get(key) if dedup_refusal(value) else stub_source
                    original = self.engine.store.record(self.engine.session_id, source) if source else None
                    payload = result_value(original or {})
                    payload.update(authority='archived_raw', disk_rechecked=False,
                        current_validation_error=str(error), archived_source_id=source,
                        current_read_tool='rolling_file_snapshot',
                        notice='Historical read only: current host disk could not be validated. Use rolling_file_snapshot with an absolute path for current host source; use RAW for archived evidence.')
                    if not payload.get('content'):
                        # Region/session mismatch cannot invent an earlier read.
                        continue
                replacement = copy.deepcopy(selected[index])
                replacement['content'] = dumps(payload)
                # Archive the recovery result too, so admission can excerpt it
                # using the existing source_id machinery without losing handles.
                self.engine.store.snapshot(self.engine.session_id, 'file_read_recovery', [replacement])
                recovery = (replacement['content'], digest(replacement))
                self.recoveries[stub_source] = recovery
            if not recovered:
                selected = copy.deepcopy(selected)
            content, source = recovery
            selected[index]['content'] = content
            recovered.append({'source_id': source, 'stub_source_id': stub_source,
                              'tool_call_id': selected[index]['tool_call_id'], 'region': key,
                              'tokens': self.engine.counter.text(content)})
        return selected, recovered
