from __future__ import annotations

import fcntl
from contextlib import contextmanager
from collections import OrderedDict
import hashlib
import json
import os
import re
from pathlib import Path
import sqlite3
import time
import urllib.request


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


_PRIVATE_REASONING = frozenset(("reasoning", "reasoning_content", "reasoning_details",
    "codex_reasoning_items", "_thinking_prefill", "thinking", "thinking_content"))


def visible_content(text):
    """Remove private assistant think spans, including an unfinished span."""
    if not isinstance(text, str):
        return text
    text = re.sub(r'<\s*think\b[^>]*>.*?<\s*/\s*think\s*>', '', text, flags=re.I | re.S)
    opening = re.search(r'<\s*think\b', text, re.I)
    return text[:opening.start()] if opening else text


def without_reasoning(value, *, strip_spans=False):
    """Copy envelope/state metadata without private reasoning channels."""
    if isinstance(value, dict):
        return {k: without_reasoning(v, strip_spans=strip_spans) for k, v in value.items()
                if k not in _PRIVATE_REASONING}
    if isinstance(value, list):
        return [without_reasoning(v, strip_spans=strip_spans) for v in value
                if not isinstance(v, dict) or v.get('type') not in ('reasoning', 'thinking', 'redacted_thinking')]
    return visible_content(value) if strip_spans and isinstance(value, str) else value


def persistent_message(message):
    value = without_reasoning(message)
    if not isinstance(value, dict):
        return value
    if value.get('role') == 'assistant':
        if isinstance(value.get('content'), str):
            value['content'] = visible_content(value['content'])
        elif isinstance(value.get('content'), list):
            for block in value['content']:
                if isinstance(block, dict) and isinstance(block.get('text'), str):
                    block['text'] = visible_content(block['text'])
    elif value.get('role') == 'tool' and isinstance(value.get('content'), str):
        # Historical retrieval envelopes can contain serialized old messages.
        # Redact the new projection without mutating the archived source.
        try:
            original = json.loads(value['content'])
            result = without_reasoning(original)
            if isinstance(result, dict):
                if isinstance(result.get('untrusted_source'), str):
                    try:
                        source = json.loads(result['untrusted_source'])
                        result['untrusted_source'] = dumps(persistent_message(source))
                    except (ValueError, TypeError):
                        pass  # Bounded partial history pages are already projected by the tool.
                for item in result.get('untrusted_history') or []:
                    if isinstance(item, dict) and isinstance(item.get('message'), dict):
                        item['message'] = persistent_message(item['message'])
                if result.get('kind') == 'RAW RECORD READ' and isinstance(result.get('excerpt'), dict):
                    excerpt = result['excerpt']
                    if isinstance(excerpt.get('content'), str):
                        excerpt['content'] = visible_content(excerpt['content'])
            if result != original:
                value['content'] = dumps(result)
        except (ValueError, TypeError):
            pass
    return value


def wire_message(message):
    # Sanitize before both hashing and persistence; historical records stay exact.
    return {k: v for k, v in persistent_message(message).items() if k in {
        "role", "content", "name", "tool_calls", "tool_call_id", "function_call", "refusal",
    }}


def wire_hash(messages):
    return hashlib.sha256(dumps([wire_message(m) for m in messages]).encode()).hexdigest()


def transport_hash(messages):
    """Exact semantic wire alias for the documented Hermes send-time repairs.

    Keep roles, text, function names, arguments and result relationships. Strip
    only envelope metadata; never collapse internal source/code whitespace.
    """
    import re
    def clean(value):
        if isinstance(value, str):
            return re.sub(r'[\ud800-\udfff]', '\ufffd', value)
        if isinstance(value, list):
            return [clean(item) for item in value]
        if isinstance(value, dict):
            return {clean(k): clean(v) for k, v in value.items()}
        return value
    projected = []; call_ids = {}; call_names = {}; sequence = 0
    for index, message in enumerate(messages):
        calls = message.get('tool_calls') or []
        content = message.get('content')
        empty = content is None or isinstance(content, str) and not content.strip()
        thinking = (message.get('_thinking_prefill') or message.get('reasoning_content') or
                    message.get('reasoning') or message.get('reasoning_details') or
                    any(isinstance(r, dict) and r.get('type') == 'reasoning'
                        for r in message.get('codex_reasoning_items') or []))
        if message.get('role') == 'assistant' and not calls and empty and thinking:
            continue
        value = clean(wire_message(message))
        value.pop('reasoning_content', None)
        for key in ('name','refusal','function_call'):
            if value.get(key) is None:
                value.pop(key, None)
        if isinstance(value.get('content'), list):
            value['content'] = [{k:v for k,v in block.items() if k not in ('cache_control','cache_marker')}
                                if isinstance(block,dict) else block for block in value['content']]
        if isinstance(value.get('content'), str):
            value['content'] = value['content'].strip()
        if empty and value.get('role') == 'assistant':
            value['content'] = '' if calls or index == len(messages)-1 else '[response interrupted]'
        canonical_calls = []
        for call in calls:
            fn = clean(call.get('function') or {})
            arguments = fn.get('arguments')
            try:
                parsed = json.loads(arguments) if isinstance(arguments, str) and arguments.strip() else arguments or {}
                arguments = dumps(clean(parsed))
            except (ValueError, TypeError):
                pass  # Unknown repairs must remain unmatched rather than guessed.
            name = fn.get('name')
            if isinstance(name, str) and not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', name):
                name = re.sub(r'_+', '_', re.sub(r'[^A-Za-z0-9_-]', '_', name.strip())).strip('_')[:64] or 'invalid_tool_call'
            canonical_id = 'call:'+str(sequence); sequence += 1
            original_id = clean(call.get('id'))
            if original_id is not None:
                call_ids[original_id] = canonical_id; call_names[original_id] = name
            canonical_calls.append({'id': canonical_id, 'type': call.get('type') or 'function',
                                    'function': {'name': name, 'arguments': arguments}})
        value.pop('tool_calls', None)
        if canonical_calls:
            value['tool_calls'] = canonical_calls
        if value.get('role') == 'tool':
            original_id = value.get('tool_call_id')
            value['tool_call_id'] = call_ids.get(original_id, original_id)
            if original_id in call_names:
                value['name'] = call_names[original_id]
        if projected and value.get('role') == projected[-1].get('role') == 'user' and isinstance(value.get('content'), str) and isinstance(projected[-1].get('content'), str):
            projected[-1]['content'] = (projected[-1]['content']+'\n\n'+value['content']).strip()
        else:
            projected.append(value)
    return digest(projected)


def digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()


def http_json(base_url, path, payload=None, timeout=10):
    url = base_url.rstrip("/") + path
    data = dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    # Loopback calls must not inherit ambient HTTP proxy settings.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        return json.load(response)


def stream_chat(base_url, payload, timeout=600, *, discard_reasoning=False):
    """Collect a CPU summary via SSE so disconnect cancels this request only."""
    payload = {**payload, "stream": True, "stream_options": {"include_usage": True}}
    request = urllib.request.Request(base_url.rstrip("/")+"/v1/chat/completions",
                                     data=dumps(payload).encode(),headers={"Content-Type":"application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic()+timeout
    content = []
    reasoning = []
    reasoning_present = False
    usage = {}
    timings = None
    finish = None
    with opener.open(request,timeout=timeout) as response:
        while True:
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Compactor stream deadline exceeded")
            # urllib exposes the socket through its buffered HTTP response.
            # Cap each blocking read as well as the total stream lifetime.
            response.fp.raw._sock.settimeout(remaining)
            line = response.readline(4*1024*1024)
            if not line:
                break
            if not line.startswith(b"data:"):
                continue
            data = line[5:].strip()
            if data == b"[DONE]":
                break
            item = json.loads(data)
            usage = item.get("usage") or usage
            timings = item.get("timings") or timings
            for choice in item.get("choices",[]):
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    content.append(delta["content"])
                if delta.get("reasoning_content"):
                    reasoning_present = True
                    if not discard_reasoning:
                        reasoning.append(delta["reasoning_content"])
                finish = choice.get("finish_reason") or finish
    return {"choices":[{"finish_reason":finish,"message":{
        "content":"".join(content),"reasoning_content":"".join(reasoning),
        "reasoning_present": reasoning_present,
    }}],"usage":usage,"timings":timings}


def option_value(argv, names):
    for idx, item in enumerate(argv):
        for name in names:
            if item == name and idx + 1 < len(argv):
                return argv[idx + 1]
            if item.startswith(name + "="):
                return item.split("=", 1)[1]
    return None


def compactor_process_facts():
    # Dependency-light: reads /proc only, so the background worker can import
    # it without pulling in CLI-only modules (e.g. yaml).
    facts = []
    for directory in Path("/proc").glob("[0-9]*"):
        try:
            argv = (directory / "cmdline").read_bytes().decode("utf-8").rstrip("\0").split("\0")
            if not argv or Path(argv[0]).name != "llama-server":
                continue
            if option_value(argv, ["--port"]) != "8083":
                continue
            # PID is diagnostic only; no process-management operation uses it.
            facts.append({"pid": int(directory.name),
                          "gpu_layers": option_value(argv, ["--n-gpu-layers", "--gpu-layers", "-ngl"]),
                          "reasoning": option_value(argv, ["--reasoning"]),
                          "ctx_size": option_value(argv, ["--ctx-size", "-c"])})
        except (OSError, UnicodeError, ValueError):
            continue
    return facts


class TokenCounter:
    def __init__(self, base_url):
        self.base_url = base_url
        self.cache = OrderedDict()
        self.prompt_cache = OrderedDict()

    def text(self, text):
        key = digest(text)
        if key not in self.cache:
            self.cache[key] = len(http_json(self.base_url, "/tokenize", {
                "content": text, "add_special": False, "parse_special": True,
            })["tokens"])
            if len(self.cache) > 32768:
                self.cache.popitem(last=False)
        self.cache.move_to_end(key)
        return self.cache[key]

    def messages(self, messages):
        payload = {"messages": [wire_message(m) for m in messages]}
        key = digest(payload)
        if key in self.prompt_cache:
            self.prompt_cache.move_to_end(key)
            return self.prompt_cache[key]
        prompt = http_json(self.base_url, "/apply-template", payload)["prompt"]
        count = len(http_json(self.base_url, "/tokenize", {
            "content": prompt, "add_special": True, "parse_special": True,
        })["tokens"])
        self.prompt_cache[key] = count
        if len(self.prompt_cache)>512:
            self.prompt_cache.popitem(last=False)
        return count

    def weights(self, messages):
        # Record sizes choose a chunk; the complete selected prompt is separately
        # counted with the actual server chat template at every boundary.
        return [self.text(dumps(wire_message(m))) for m in messages]


class StateStore:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "memory.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS records (
                    hash TEXT PRIMARY KEY, body TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY, session TEXT NOT NULL, kind TEXT NOT NULL,
                    created REAL NOT NULL, hashes TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    UNIQUE(session, kind, fingerprint)
                );
                CREATE TABLE IF NOT EXISTS summaries (
                    id INTEGER PRIMARY KEY, session TEXT NOT NULL, created REAL NOT NULL,
                    covered TEXT NOT NULL, text TEXT NOT NULL, tokens INTEGER NOT NULL,
                    parent_id INTEGER, job_id INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY, session TEXT NOT NULL, created REAL NOT NULL,
                    updated REAL NOT NULL, status TEXT NOT NULL, owner INTEGER NOT NULL,
                    coverage TEXT NOT NULL, parent_id INTEGER, chunk TEXT NOT NULL,
                    trigger_tokens INTEGER NOT NULL, chunk_tokens INTEGER NOT NULL,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS summary_session ON summaries(session, id);
                CREATE INDEX IF NOT EXISTS job_session ON jobs(session, status);
                CREATE TABLE IF NOT EXISTS context_generations (
                    id INTEGER PRIMARY KEY, session TEXT NOT NULL, created REAL NOT NULL,
                    request_hash TEXT NOT NULL, blocks TEXT NOT NULL, covered_messages INTEGER NOT NULL
                );
            """)
            if "pages" not in {row[1] for row in db.execute("PRAGMA table_info(context_generations)")}:
                db.execute("ALTER TABLE context_generations ADD COLUMN pages TEXT NOT NULL DEFAULT '[]'")
            if "transport_hash" not in {row[1] for row in db.execute("PRAGMA table_info(context_generations)")}:
                db.execute("ALTER TABLE context_generations ADD COLUMN transport_hash TEXT")
            db.execute('CREATE INDEX IF NOT EXISTS generation_transport_lookup ON context_generations(transport_hash)')
            if 'admitted_generation' not in {row[1] for row in db.execute("PRAGMA table_info(summaries)")}:
                db.execute("ALTER TABLE summaries ADD COLUMN admitted_generation INTEGER")
            for table in ('jobs', 'summaries'):
                if 'coverage_kind' not in {row[1] for row in db.execute(f'PRAGMA table_info({table})')}:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN coverage_kind TEXT NOT NULL DEFAULT 'prefix'")
        os.chmod(self.path, 0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def snapshot(self, session, kind, messages):
        messages = [persistent_message(m) for m in messages]
        hashes = [digest(m) for m in messages]
        with self.connect() as db:
            db.executemany("INSERT OR IGNORE INTO records VALUES (?,?)",
                           [(h, dumps(m)) for h, m in zip(hashes, messages)])
            db.execute("INSERT OR IGNORE INTO snapshots(session,kind,created,hashes,fingerprint) VALUES (?,?,?,?,?)",
                       (session, kind, time.time(), dumps(hashes), digest(hashes)))

    def compatible_summary(self, session, hashes):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM summaries WHERE session=? ORDER BY id DESC", (session,)).fetchall()
        for row in rows:
            if row['coverage_kind'] != 'prefix':
                continue
            try:
                covered = json.loads(row["covered"])
            except (ValueError, TypeError):
                continue
            if not isinstance(covered, list) or not covered or not all(isinstance(h, str) for h in covered):
                continue
            if hashes[:len(covered)] == covered:
                return dict(row), len(covered)
        return None, 0

    def compatible_page_blocks(self, session, hashes):
        """Independent blocks cover their named sources, never an invented prefix."""
        allowed = set(hashes)
        with self.connect() as db:
            rows = db.execute("SELECT * FROM summaries WHERE session=? AND coverage_kind='source_set' ORDER BY id DESC LIMIT 64", (session,)).fetchall()
        valid = []
        for row in rows:
            try:
                sources = json.loads(row['covered'])
                if isinstance(sources, list) and sources and all(isinstance(h, str) and h in allowed for h in sources):
                    valid.append(dict(row))
            except (ValueError, TypeError):
                continue
        return valid

    def summary_chain(self, row):
        chain = []
        seen = set()
        session = row["session"] if row else None
        with self.connect() as db:
            while row:
                # A damaged pointer must not hang foreground selection or read a
                # different session's semantic history. RAW remains independent.
                if row["id"] in seen or row["session"] != session:
                    return []
                seen.add(row["id"])
                chain.append(dict(row))
                parent_id = row["parent_id"]
                row = db.execute("SELECT * FROM summaries WHERE id=?", (parent_id,)).fetchone() if parent_id else None
                if parent_id and row is None:
                    return []
        return list(reversed(chain))

    def generation(self, session, request_hash, blocks, covered, pages=(), transport_alias=None):
        with self.connect() as db:
            prior = db.execute("SELECT id,request_hash FROM context_generations WHERE session=? ORDER BY id DESC LIMIT 1", (session,)).fetchone()
            if prior and prior["request_hash"] == request_hash:
                if transport_alias is not None:
                    db.execute('UPDATE context_generations SET transport_hash=? WHERE id=?', (transport_alias,prior['id']))
                return prior["id"]
            return db.execute("INSERT INTO context_generations(session,created,request_hash,blocks,covered_messages,pages,transport_hash) VALUES (?,?,?,?,?,?,?)",
                              (session, time.time(), request_hash, dumps(blocks), covered,dumps(pages),transport_alias)).lastrowid

    def ready_compacts(self, session):
        with self.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT s.*,j.id AS ready_job_id,j.coverage AS job_coverage FROM jobs j "
                "LEFT JOIN summaries s ON s.job_id=j.id AND s.session=j.session "
                "WHERE j.session=? AND j.status='ready' ORDER BY j.id", (session,))]

    def admit_compacts(self, session, blocks, generation):
        """One durable first-admission receipt; never change RAW or summary text."""
        admitted = []
        with self.connect() as db:
            for block in set(blocks):
                result = db.execute("UPDATE summaries SET admitted_generation=? "
                    "WHERE session=? AND id=? AND admitted_generation IS NULL",
                    (generation, session, block))
                if result.rowcount:
                    admitted.append(block)
        return sorted(admitted)

    def job_status(self, session):
        with self.connect() as db:
            rows = db.execute("SELECT status,count(*) FROM jobs WHERE session=? GROUP BY status", (session,)).fetchall()
            counts = dict(rows)
            last = db.execute("SELECT updated-created AS duration FROM jobs WHERE session=? AND status='ready' ORDER BY id DESC LIMIT 1", (session,)).fetchone()
            oldest = db.execute("SELECT min(created) AS oldest FROM jobs WHERE session=? AND status='queued'",(session,)).fetchone()['oldest']
            admitted = db.execute("SELECT count(DISTINCT j.id) FROM jobs j JOIN summaries s "
                "ON s.job_id=j.id AND s.session=j.session WHERE j.session=? AND j.status='ready' "
                "AND s.admitted_generation IS NOT NULL", (session,)).fetchone()[0]
        return {"oldest_queue_age_seconds":max(0,time.time()-oldest) if oldest else 0,
                "compaction_failed_jobs":counts.get('failed',0),"compaction_ready_jobs":counts.get('ready',0),
                "compaction_admitted_jobs":admitted, "compaction_pending_ready_jobs":counts.get('ready',0)-admitted,
                "compaction_queue":counts.get("queued",0),"compaction_in_progress":counts.get("running",0),
                "last_compaction_duration":last["duration"] if last else None}


    def record(self, session, record_hash):
        if isinstance(record_hash, str) and record_hash.startswith('relay-raw:'):
            parts = record_hash.split(':', 2)
            if len(parts) != 3 or not parts[1].startswith('relay-emergency-'):
                return None
            session, record_hash = parts[1:]
        with self.connect() as db:
            snapshots = db.execute("SELECT hashes FROM snapshots WHERE session=?", (session,)).fetchall()
            if not any(record_hash in json.loads(row["hashes"]) for row in snapshots):
                return None
            row = db.execute("SELECT body FROM records WHERE hash=?", (record_hash,)).fetchone()
            return json.loads(row["body"]) if row else None

    def emit(self, event, **fields):
        path = self.root / "telemetry.jsonl"
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            data = (dumps(without_reasoning({"ts": time.time(), "event": event, **fields}, strip_spans=True)) + "\n").encode()
            with os.fdopen(fd, "ab", closefd=False) as stream:
                stream.write(data)
                stream.flush()
                os.fsync(fd)
        finally:
            os.close(fd)

    def search(self, session, query, limit=5):
        # Return archived canonical record versions, never unrelated sessions.
        with self.connect() as db:
            snapshots = db.execute("SELECT hashes FROM snapshots WHERE session=? AND kind!='request' ORDER BY id DESC", (session,)).fetchall()
            hashes = dict.fromkeys(h for row in snapshots for h in json.loads(row["hashes"]))
            result = []
            for h in hashes:
                row = db.execute("SELECT body FROM records WHERE hash=?", (h,)).fetchone()
                if row and query.lower() in row["body"].lower():
                    body = json.loads(row["body"])
                    result.append({"hash": h, "message": body})
                    if len(result) >= limit:
                        break
            return result

def generation_for_request(root, request_hash, transport_alias=None):
    path = Path(root)/"memory.sqlite3"
    if not path.is_file():
        return None
    try:
        db = sqlite3.connect(path.as_uri()+"?mode=ro",uri=True,timeout=1)
        try:
            rows=db.execute("SELECT id,session,blocks FROM context_generations WHERE request_hash=? ORDER BY id DESC",(request_hash,)).fetchall()
            basis = 'exact_request_hash'
            if not rows and transport_alias:
                columns={r[1] for r in db.execute('PRAGMA table_info(context_generations)')}
                if 'transport_hash' in columns:
                    rows=db.execute('SELECT id,session,blocks FROM context_generations WHERE transport_hash=? ORDER BY id DESC',(transport_alias,)).fetchall()
                    basis='canonical_transport_hash'
            if not rows:
                return None
            if len({row[1] for row in rows})>1:
                return {"context_generation":None,"generation_link_ambiguous":True}
            row=rows[0];blocks=json.loads(row[2])
            return {"context_generation":row[0],"session_id":row[1],"summary_blocks":blocks,
                    "prefill_after_compaction":bool(blocks),"generation_link_basis":basis}
        finally:
            db.close()
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return None
