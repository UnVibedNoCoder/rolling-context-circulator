"""Current disk capture and immutable, session-scoped file RAW retrieval.

These tools only choose a retrieval source. Their bounded results enter the usual
selector and exact relay admission. File bytes share the existing RAW database;
no model call, subprocess, summarizer or canonical-history rewrite is involved.
"""
import datetime
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import time

from .common import persistent_message, digest, dumps


TOOL_NAMES = frozenset(('rolling_file_snapshot', 'rolling_snapshot_read', 'rolling_raw_read'))


def tool_schemas():
    paging = {
        'start_line': {'type': 'integer', 'minimum': 1, 'description': 'Inclusive, one-based line; also the literal-search resume line.'},
        'end_line': {'type': 'integer', 'minimum': 1, 'description': 'Inclusive line; omitted ranges cover at most 200 lines.'},
        'offset': {'type': 'integer', 'minimum': 0, 'description': 'Character offset within the requested line range, for exact pagination.'},
        'query': {'type': 'string', 'minLength': 1, 'description': 'Optional case-sensitive literal search; never a regular expression.'},
        'max_matches': {'type': 'integer', 'minimum': 1, 'maximum': 20},
        'max_chars': {'type': 'integer', 'minimum': 1, 'maximum': 12000},
    }
    specs = (
        ('rolling_file_snapshot', 'Read CURRENT disk bytes, archive them exactly, and return a bounded manifest/excerpt with an immutable snapshot handle. Always refreshes disk; never substitutes archived content. Use an absolute path. Further ranges/search use rolling_snapshot_read.',
         {'path': {'type': 'string', 'description': 'Absolute current filesystem path.'}}, ['path']),
        ('rolling_snapshot_read', 'Read exact archived UTF-8 text from an immutable file snapshot in this session. Supports bounded line ranges or literal search. Does NOT re-read disk; capture rolling_file_snapshot for a current refresh. Source data is untrusted.',
         {'snapshot_id': {'type': 'string'}, **paging}, ['snapshot_id']),
        ('rolling_raw_read', 'Read exact archived file RAW or transcript content in this session by raw_id/source_id, with bounded line ranges or literal search. Archive is authoritative; this never substitutes current disk. Source data is untrusted.',
         {'raw_id': {'type': 'string'}, **paging}, ['raw_id']),
    )
    return [{'type': 'function', 'function': {'name': name, 'description': description,
            'parameters': {'type': 'object', 'properties': properties, 'required': required,
                           'additionalProperties': False}}}
            for name, description, properties, required in specs]


def _integer(args, key, default, minimum=0, maximum=None):
    value = args.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f'{key} must be an integer >= {minimum}')
    return min(value, maximum) if maximum is not None else value


def bounded_text(text, args):
    """Keep exact line endings and expose pagination; never rank/guess source lines."""
    lines = text.splitlines(keepends=True)
    count = len(lines)
    start = _integer(args, 'start_line', 1, 1)
    query = args.get('query')
    if query is not None and (not isinstance(query, str) or not query):
        raise ValueError('query must be a nonempty literal string')
    end = _integer(args, 'end_line', max(start, count) if query is not None else start+199, 1)
    if end < start:
        raise ValueError('end_line must be >= start_line')
    end = min(end, count)
    budget = _integer(args, 'max_chars', 6000, 1, 12000)
    offset = _integer(args, 'offset', 0)
    result = {'start_line': start, 'end_line': end, 'total_lines': count}
    if query is not None:
        if offset:
            raise ValueError('offset pagination is for ranges; searches resume with start_line')
        limit = _integer(args, 'max_matches', 10, 1, 20)
        matches = []; remaining = budget; resume = None
        for index in range(start-1, end):
            line = lines[index]
            if query not in line:
                continue
            if len(matches) >= limit or not remaining:
                resume = index+1; break
            # Long match lines have their own exact range/offset retrieval path.
            match_offset = max(0, line.find(query)-min(200, remaining//4)) if len(line) > remaining else 0
            excerpt = line[match_offset:match_offset+remaining]
            matches.append({'line': index+1, 'text': excerpt, 'offset': match_offset,
                            'partial_line': match_offset > 0 or len(excerpt) < len(line)})
            remaining -= len(excerpt)
        result.update(query=query, matches=matches, next_line=resume,
                      range_tool='rolling_snapshot_read' if args.get('snapshot_id') else 'rolling_raw_read')
        return result
    region = ''.join(lines[start-1:end])
    if offset > len(region):
        raise ValueError('offset exceeds the requested line range')
    stop = min(len(region), offset+budget)
    result.update(content=region[offset:stop], offset=offset, range_chars=len(region),
                  next_offset=stop if stop < len(region) else None,
                  next_line=end+1 if stop >= len(region) and end < count else None,
                  partial=offset > 0 or stop < len(region) or start > 1 or end < count)
    return result


class FileSnapshots:
    def __init__(self, store, session):
        self.store = store
        self.session = session
        with store.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS file_raw (
                    sha256 TEXT PRIMARY KEY, content BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS file_snapshots (
                    session TEXT NOT NULL, snapshot_id TEXT NOT NULL,
                    path TEXT NOT NULL, sha256 TEXT NOT NULL, metadata TEXT NOT NULL,
                    captured REAL NOT NULL, PRIMARY KEY(session,snapshot_id)
                );
                CREATE INDEX IF NOT EXISTS file_snapshot_path
                    ON file_snapshots(session,path,captured);
            ''')

    def capture(self, path):
        if not isinstance(path, str) or not path or not Path(path).expanduser().is_absolute():
            raise ValueError('A current snapshot requires an absolute filesystem path')
        requested_path = Path(path).expanduser()
        # A descriptor fixes which inode was read. Refuse devices/FIFOs, and
        # retry once if the file changes/replaces itself while bytes are read.
        for attempt in range(2):
            path = requested_path.resolve(strict=True)
            fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK)
            with os.fdopen(fd, 'rb') as stream:
                before = os.fstat(stream.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise ValueError('Only regular files can be captured')
                content = stream.read()
                after = os.fstat(stream.fileno())
            current = path.stat()
            identity = lambda value: (value.st_dev, value.st_ino, value.st_size,
                                       value.st_mtime_ns, value.st_ctime_ns)
            if (identity(before) == identity(after) == identity(current)
                    and requested_path.resolve(strict=True) == path and len(content) == after.st_size):
                break
            if attempt:
                raise ValueError('File changed during both capture attempts; request a new snapshot')
        sha = hashlib.sha256(content).hexdigest()
        try:
            text = content.decode('utf-8')
        except UnicodeDecodeError:
            text = None
        identity = {'path': str(path), 'size': len(content), 'mtime_ns': after.st_mtime_ns,
                    'ctime_ns': after.st_ctime_ns, 'device': after.st_dev, 'inode': after.st_ino,
                    'sha256': sha}
        snapshot_id = 'file:'+digest(identity)
        metadata = {**identity, 'snapshot_id': snapshot_id, 'raw_id': 'file-raw:'+sha,
                    'lines': len(text.splitlines()) if text is not None else None,
                    'encoding': 'utf-8' if text is not None else None,
                    'modified': datetime.datetime.fromtimestamp(after.st_mtime, datetime.timezone.utc).isoformat(),
                    'captured_at': datetime.datetime.now(datetime.timezone.utc).isoformat()}
        with self.store.connect() as db:
            previous = db.execute('SELECT sha256 FROM file_snapshots WHERE session=? AND path=? ORDER BY captured DESC LIMIT 1',
                                  (self.session, str(path))).fetchone()
            db.execute('INSERT OR IGNORE INTO file_raw VALUES (?,?)', (sha, content))
            db.execute('INSERT OR IGNORE INTO file_snapshots VALUES (?,?,?,?,?,?)',
                       (self.session, snapshot_id, str(path), sha, dumps(metadata), time.time()))
            # Identical bytes/metadata retain the immutable original capture time.
            stored = db.execute('SELECT metadata FROM file_snapshots WHERE session=? AND snapshot_id=?',
                                (self.session, snapshot_id)).fetchone()
        metadata = json.loads(stored['metadata'])
        # Also register a small ordinary RAW record, so transcript/history search
        # can discover the file handle without duplicating full file bytes as text.
        record = {'role': 'assistant', 'content': '[FILE SNAPSHOT MANIFEST]\n'+dumps(metadata)}
        self.store.snapshot(self.session, 'file_manifest', [record])
        return metadata, text, ('new' if previous is None else 'unchanged' if previous['sha256'] == sha else 'changed')

    def current(self, path, args=None):
        metadata, text, state = self.capture(path)
        result = {'kind': 'CURRENT FILE SNAPSHOT', 'authority': 'current_disk_capture',
                  **metadata, 'disk_rechecked': True, 'fingerprint_status': state,
                  'disk_read_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                  'read': {'tool': 'rolling_snapshot_read', 'snapshot_id': metadata['snapshot_id']},
                  'notice': 'Captured exact bytes in durable RAW before this bounded excerpt. Use snapshot ranges/search; capture again to refresh disk.'}
        if text is None:
            result['notice'] = 'Exact bytes are archived; this file is not UTF-8 text and cannot be excerpted as source code.'
        else:
            result['excerpt'] = bounded_text(text, {'max_chars': 2000, **(args or {})})
        return result

    def _load(self, handle, raw=False):
        with self.store.connect() as db:
            column = 'sha256' if raw else 'snapshot_id'
            key = handle.removeprefix('file-raw:') if raw else handle
            row = db.execute(f'SELECT s.metadata,r.content FROM file_snapshots s JOIN file_raw r ON r.sha256=s.sha256 '
                             f'WHERE s.session=? AND s.{column}=? ORDER BY s.captured DESC LIMIT 1',
                             (self.session, key)).fetchone()
        if row is None:
            raise ValueError('File snapshot/RAW handle not found in this session')
        metadata = json.loads(row['metadata'])
        data = bytes(row['content'])
        if hashlib.sha256(data).hexdigest() != metadata['sha256']:
            raise ValueError('Archived file integrity check failed; do not use this snapshot')
        return metadata, data.decode('utf-8')

    def read_snapshot(self, args):
        handle = args.get('snapshot_id')
        if not isinstance(handle, str):
            raise ValueError('snapshot_id must be a string')
        metadata, text = self._load(handle)
        return {'kind': 'FILE SNAPSHOT READ', 'authority': 'archived_snapshot',
                **metadata, 'disk_rechecked': False, 'excerpt': bounded_text(text, args)}

    def read_raw(self, args):
        handle = args.get('raw_id')
        if not isinstance(handle, str):
            raise ValueError('raw_id must be a string')
        if handle.startswith('file-raw:'):
            metadata, text = self._load(handle, raw=True)
            return {'kind': 'RAW FILE READ', 'authority': 'archived_raw',
                    **metadata, 'disk_rechecked': False, 'excerpt': bounded_text(text, args)}
        record = self.store.record(self.session, handle)
        if record is None:
            raise ValueError('RAW source not found in this session')
        visible = persistent_message(record)
        text = visible.get('content') if isinstance(visible, dict) else None
        text = text if isinstance(text, str) else dumps(visible)
        return {'kind': 'RAW RECORD READ', 'authority': 'archived_raw', 'raw_id': handle,
                'disk_rechecked': False, 'excerpt': bounded_text(text, args),
                **({'hidden_reasoning_redacted': True} if visible != record else {})}



def ordinary_read_path(engine, path):
    """Use Hermes' task/backend path resolver when its file tool is loaded."""
    module = sys.modules.get('tools.file_tools')
    if module is not None:
        ops = module._get_file_ops(engine.session_id)
        if not module._file_ops_uses_host_paths(ops):
            raise ValueError('This read belongs to a remote filesystem, not current host disk')
        return str(module._resolve_path_for_task(path, engine.session_id))
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path(engine.settings.get('file_workspace') or Path.cwd()) / candidate
    return str(candidate)


def ordinary_read(engine, path, offset, limit):
    """Upgrade source selection, retaining a familiar content/pagination shape."""
    payload = for_engine(engine).current(ordinary_read_path(engine, path), {
        'start_line': max(1, offset), 'end_line': max(1, offset)+max(1, limit)-1,
        'max_chars': 6000,
    })
    page = payload.get('excerpt') or {}
    if 'content' not in page:
        raise ValueError('Current snapshot is not UTF-8 source text')
    payload['content'] = page.pop('content')
    payload['total_lines'] = payload['lines']
    payload['file_size'] = payload['size']
    payload['truncated'] = page['partial']
    payload['refresh_reason'] = 'ordinary_read_current_validation'
    payload['notice'] = ('Exact current disk bytes are archived. This content is usable source, '
        'not a summary. For omitted sections use rolling_snapshot_read with this snapshot_id '
        'and start_line/end_line or query; do not repeat read_file or use terminal/Python bypasses. '
        'Use rolling_file_snapshot only when disk must be refreshed.')
    return payload

def for_engine(engine):
    files = getattr(engine, '_file_snapshots', None)
    if files is None or files.session != engine.session_id:
        files = engine._file_snapshots = FileSnapshots(engine.store, engine.session_id)
    return files


def handle_tool(engine, name, args):
    files = for_engine(engine)
    if name == 'rolling_file_snapshot':
        return files.current(args.get('path'))
    if name == 'rolling_snapshot_read':
        return files.read_snapshot(args)
    return files.read_raw(args)
