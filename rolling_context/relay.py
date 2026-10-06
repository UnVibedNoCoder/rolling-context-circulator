"""Loopback chat relay: exact prompt guard, transparent streaming, private telemetry.

This process owns no model server. It only connects to the existing profile port.
The context-selection plugin retains history; this relay verifies the final wire
request, including tool definitions, before allowing inference.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import select
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .common import without_reasoning

PROFILES = {
    "fast": {"upstream_port": 8082, "relay_port": 8092, "context": 65536},
    "large": {"upstream_port": 8084, "relay_port": 8094, "context": 73728},
}
GENERATION_MARGIN = 1024
MAX_BODY_BYTES = 8 * 1024 * 1024
MAX_JSON_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_SSE_EVENT_BYTES = 4 * 1024 * 1024


def request_hash(messages: list[dict[str, Any]]) -> str:
    # Imported lazily so the relay can also be smoke-tested independently.
    try:
        from .common import wire_hash
    except ImportError:
        def clean(value: Any) -> Any:
            if isinstance(value, dict):
                return {k: clean(v) for k, v in value.items() if not k.startswith("_")}
            if isinstance(value, list):
                return [clean(v) for v in value]
            return value
        return hashlib.sha256(json.dumps(clean(messages), sort_keys=True,
                                        ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    return wire_hash(messages)


def emit(state_dir: Path, profile: str, event: str, **fields: Any) -> None:
    """Append one locked JSON line; never put headers or transcript text here."""
    record = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(),
              "event": event, "profile": profile, **fields}
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(state_dir / "telemetry.jsonl", flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        data = (json.dumps(without_reasoning(record, strip_spans=True), ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def write_overhead(state_dir: Path, profile: str, overhead_tokens: int, **measurement) -> None:
    from .schema_budget import write_measurement
    return write_measurement(state_dir, profile, overhead_tokens, **measurement)


def write_feedback(state_dir: Path, profile: str, fields: dict[str, Any]) -> None:
    """Publish only attributable completion metrics for boundary controllers."""
    if not fields.get("context_generation") or not fields.get("session_id"):
        return
    keys = ("session_id", "context_generation", "transport_hash", "input_tokens",
            "prompt_n", "cache_n", "prompt_ms", "predicted_per_second", "predicted_ms",
            "reasoning_observation", "governor_cancelled")
    value = {key: fields[key] for key in keys if key in fields}
    value["completed_at"] = time.time()
    path = state_dir / f"{profile}-feedback.json"
    temp = state_dir / f".{profile}-feedback-{uuid.uuid4().hex}.tmp"
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


class RequestError(Exception):
    def __init__(self, status: int, message: str, code: str = "invalid_request"):
        self.status, self.message, self.code = status, message, code
        super().__init__(message)


def validate_chat(body: Any) -> None:
    if not isinstance(body, dict):
        raise RequestError(400, "Request body must be a JSON object.")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise RequestError(400, "messages must be a nonempty list.")
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in {
                "system", "developer", "user", "assistant", "tool"}:
            raise RequestError(400, "Each message needs a supported chat role.")
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            # Deliberately fail closed for image/audio and provider content blocks.
            raise RequestError(400, "The rolling-context MVP supports string text content only.",
                               "unsupported_content")
        if "audio" in message or "images" in message:
            raise RequestError(400, "Multimodal requests are outside this text-only MVP.",
                               "unsupported_content")
    if body.get("n", 1) != 1:
        raise RequestError(400, "The MVP supports one completion per request.")
    if "stream" in body and not isinstance(body["stream"], bool):
        raise RequestError(400, "stream must be a boolean.")
    if body.get("modalities", ["text"]) != ["text"] or "audio" in body:
        raise RequestError(400, "Multimodal requests are outside this text-only MVP.",
                           "unsupported_content")


def bounded_output(body: dict[str, Any], prompt_tokens: int, context: int) -> int:
    explicit = [body[key] for key in ("max_tokens", "max_completion_tokens", "n_predict") if key in body]
    if any(isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 8192
           for value in explicit):
        raise RequestError(400, "Every output token limit must be a positive integer at most 8192.")
    requested = min(explicit) if explicit else 8192
    chosen = min(requested, context - prompt_tokens)
    if chosen <= 0:
        raise RequestError(413, "Prompt leaves no room for generation.", "context_limit")
    # llama.cpp releases differ in which alias wins. Set all aliases identically.
    body["max_tokens"] = chosen
    body["max_completion_tokens"] = chosen
    body["n_predict"] = chosen
    return chosen


class SSEObserver:
    """Observe event JSON while the original byte stream goes straight downstream."""
    def __init__(self, started: float):
        self.started = started
        self.buffer = bytearray()
        self.data_lines: list[bytes] = []
        self.event_bytes = 0
        self.timings: dict[str, Any] | None = None
        self.usage: dict[str, Any] | None = None
        self.first_content_ms: float | None = None
        self.done = False
        self.observation_truncated = False
        self.reasoning_characters = 0
        self.reasoning_chunks = 0
        self.content_characters = 0
        self.tool_call_chunks = 0
        self.reasoning_only_streak = 0
        self.longest_reasoning_only_streak = 0
        self.first_reasoning_ms = None
        self.last_reasoning_ms = None
        self.first_tool_call_ms = None
        self.first_visible_content_ms = None
        self.finish_reasons = []

    def consume(self, chunk: bytes) -> None:
        self.buffer.extend(chunk)
        while b"\n" in self.buffer:
            line, _, rest = self.buffer.partition(b"\n")
            self.buffer = bytearray(rest)
            self._line(line.rstrip(b"\r"))
        if len(self.buffer) > MAX_SSE_EVENT_BYTES:
            self.buffer.clear()
            self.data_lines.clear()
            self.event_bytes = 0
            self.observation_truncated = True

    def _line(self, line: bytes) -> None:
        if line.startswith(b"data:"):
            value = line[5:].lstrip(b" ")
            self.event_bytes += len(value)
            if self.event_bytes <= MAX_SSE_EVENT_BYTES:
                self.data_lines.append(value)
            else:
                self.data_lines.clear()
                self.observation_truncated = True
        elif not line:
            data = b"\n".join(self.data_lines)
            self.data_lines.clear()
            self.event_bytes = 0
            if data == b"[DONE]":
                self.done = True
                return
            try:
                obj = json.loads(data)
            except (ValueError, UnicodeDecodeError):
                return
            self.observe_json(obj)

    def observe_json(self, obj: Any) -> None:
        if not isinstance(obj, dict):
            return
        if isinstance(obj.get("timings"), dict):
            from .governor import safe_metadata
            self.timings = safe_metadata(obj)[0]
        if isinstance(obj.get("usage"), dict):
            from .governor import safe_metadata
            self.usage = safe_metadata(obj)[1]
        for choice in obj.get("choices", []):
            delta = choice.get("delta", {}) if isinstance(choice, dict) else {}
            now_ms = round((time.monotonic() - self.started) * 1000, 3)
            reasoning = delta.get("reasoning_content")
            content = delta.get("content")
            action = delta.get("tool_calls")
            if isinstance(reasoning, str) and reasoning:
                self.reasoning_characters += len(reasoning)
                self.reasoning_chunks += 1
                if self.first_reasoning_ms is None:
                    self.first_reasoning_ms = now_ms
                self.last_reasoning_ms = now_ms
            if isinstance(content, str) and content:
                self.content_characters += len(content)
                if self.first_visible_content_ms is None:
                    self.first_visible_content_ms = now_ms
            if action:
                self.tool_call_chunks += 1
                if self.first_tool_call_ms is None:
                    self.first_tool_call_ms = now_ms
            if reasoning and not content and not action:
                self.reasoning_only_streak += 1
                self.longest_reasoning_only_streak = max(self.longest_reasoning_only_streak, self.reasoning_only_streak)
            elif content or action:
                self.reasoning_only_streak = 0
            finish = choice.get("finish_reason") if isinstance(choice, dict) else None
            if finish and finish not in self.finish_reasons:
                self.finish_reasons.append(finish)
            if any(delta.get(k) for k in ("content", "reasoning_content", "tool_calls")):
                if self.first_content_ms is None:
                    self.first_content_ms = round((time.monotonic() - self.started) * 1000, 3)

    def metrics(self) -> dict[str, Any]:
        # Chunks and characters are observations, not invented token counts.
        return {key: getattr(self, key) for key in (
            "reasoning_characters", "reasoning_chunks", "content_characters", "tool_call_chunks",
            "longest_reasoning_only_streak", "first_reasoning_ms", "last_reasoning_ms",
            "first_tool_call_ms", "first_visible_content_ms", "finish_reasons",
        )}

    def finish(self) -> None:
        if self.buffer:
            self._line(bytes(self.buffer).rstrip(b"\r"))
            self.buffer.clear()
        if self.data_lines:
            self._line(b"")


class RequestWatch:
    """Close this request's upstream sockets on deadline or client disconnect."""
    def __init__(self, downstream: socket.socket, timeout: float):
        self.downstream = downstream
        self.deadline = time.monotonic() + timeout
        self.sockets: list[socket.socket] = []
        self.lock = threading.Lock()
        self.finished = threading.Event()
        self.reason: str | None = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def __enter__(self) -> "RequestWatch":
        self.thread.start()
        return self

    def remaining(self) -> float:
        if self.reason:
            raise RequestError(504 if self.reason == "deadline" else 499,
                               "Relay request deadline reached." if self.reason == "deadline"
                               else "Client disconnected.", self.reason)
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise RequestError(504, "Relay request deadline reached.", "deadline")
        return remaining

    def track(self, sock: socket.socket) -> None:
        with self.lock:
            self.sockets.append(sock)
            if self.reason:
                self._close(sock)

    def close_upstreams(self) -> None:
        """Cancel only sockets registered by this request, without changing its verdict."""
        with self.lock:
            for sock in self.sockets:
                self._close(sock)

    @staticmethod
    def _close(sock: socket.socket) -> None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    def _run(self) -> None:
        while not self.finished.wait(0.1):
            reason = "deadline" if time.monotonic() >= self.deadline else None
            if not reason:
                try:
                    readable, _, _ = select.select([self.downstream], [], [], 0)
                    if readable and not self.downstream.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT):
                        reason = "client_disconnected"
                except (OSError, ValueError):
                    reason = "client_disconnected"
            if reason:
                self.reason = reason
                with self.lock:
                    for sock in self.sockets:
                        self._close(sock)
                return

    def __exit__(self, *exc: Any) -> None:
        self.finished.set()
        self.thread.join(timeout=0.2)


class RelayServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, profile: str, state_dir: str | Path, port: int | None = None,
                 deadline_seconds: float = 600):
        if profile not in PROFILES:
            raise ValueError("Unknown profile")
        self.profile = profile
        self.profile_config = PROFILES[profile]
        self.state_dir = Path(state_dir).expanduser()
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.state_dir, 0o700)
        from .provenance import source_provenance
        launch = json.loads(os.environ.get('ROLLING_CONTEXT_LAUNCH_PROVENANCE', '{}'))
        settings = launch.get('effective_settings') or {'profile': profile, 'state_dir': str(self.state_dir)}
        self.provenance = source_provenance(settings, launch.get('explicit_settings', settings))
        if launch and any(launch.get(k) != self.provenance[k] for k in ('relay_source_file','source_fingerprint','policy_fingerprint')):
            raise ValueError('Relay launch provenance mismatch')
        if self.provenance['effective_settings']['profile'] != profile:
            raise ValueError('Relay launch profile mismatch')
        from .governor import validated_policy
        validated_policy(self.provenance['effective_settings'].get('generation_governor') or {})
        self.deadline_seconds = deadline_seconds
        super().__init__(("127.0.0.1", self.profile_config["relay_port"] if port is None else port),
                         RelayHandler)


class RelayHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "RollingContextRelay/0.1"

    def log_message(self, format: str, *args: Any) -> None:
        # Paths may contain user data; structured telemetry deliberately omits them.
        pass

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(min(30, self.server.deadline_seconds))

    def _json(self, status: int, obj: Any) -> None:
        payload = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(payload)
        self.wfile.flush()
        self.close_connection = True

    def _error(self, error: RequestError) -> None:
        self._json(error.status, {"error": {"message": error.message,
                    "type": "invalid_request_error" if error.status < 500 else "server_error",
                    "param": None, "code": error.code}})

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept-Encoding": "identity",
                   "Connection": "close"}
        if self.headers.get("Authorization"):
            headers["Authorization"] = self.headers["Authorization"]
        return headers

    def _open(self, watch: RequestWatch, method: str, path: str,
              payload: dict[str, Any] | None = None) -> tuple[http.client.HTTPConnection,
                                                               http.client.HTTPResponse]:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.profile_config["upstream_port"],
                                                timeout=watch.remaining())
        try:
            connection.connect()
            assert connection.sock is not None
            watch.track(connection.sock)
            body = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
            connection.request(method, path, body, self._headers())
            return connection, connection.getresponse()
        except Exception:
            connection.close()
            raise

    def _upstream_json(self, watch: RequestWatch, path: str, payload: dict[str, Any]) -> Any:
        connection, response = self._open(watch, "POST", path, payload)
        try:
            raw = response.read(MAX_JSON_RESPONSE_BYTES + 1)
            watch.remaining()
            if len(raw) > MAX_JSON_RESPONSE_BYTES:
                raise RequestError(502, "Token-counting response exceeded relay limit.", "count_unavailable")
            if response.status != 200:
                # Never relay arbitrary error text into telemetry or an exception log.
                raise RequestError(502, f"Required llama.cpp {path} returned HTTP {response.status}; "
                                   "request blocked before inference.", "count_unavailable")
            try:
                return json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                raise RequestError(502, "Invalid JSON from llama.cpp token-counting endpoint.",
                                   "count_unavailable") from None
        finally:
            response.close()
            connection.close()

    def _count(self, watch: RequestWatch, body: dict[str, Any]) -> int:
        rendered = self._upstream_json(watch, "/apply-template", body)
        if not isinstance(rendered, dict) or not isinstance(rendered.get("prompt"), str):
            raise RequestError(502, "llama.cpp /apply-template did not return a text prompt.",
                               "count_unavailable")
        result = self._upstream_json(watch, "/tokenize", {
            "content": rendered["prompt"], "add_special": True, "parse_special": True})
        if not isinstance(result, dict) or not isinstance(result.get("tokens"), list):
            raise RequestError(502, "llama.cpp /tokenize did not return tokens.", "count_unavailable")
        return len(result["tokens"])

    def do_GET(self) -> None:
        if self.path == "/rolling-context/health":
            self._json(200, {"service": "rolling-context-relay", "profile": self.server.profile,
                            "upstream_port": self.server.profile_config["upstream_port"],
                            "physical_context": self.server.profile_config["context"],
                            "generation_margin": GENERATION_MARGIN, **self.server.provenance})
            return
        if self.path not in {"/health", "/v1/health", "/v1/models", "/models"}:
            self._error(RequestError(404, "Unsupported endpoint in the bounded chat relay.",
                                     "unsupported_endpoint"))
            return
        try:
            with RequestWatch(self.connection, min(15, self.server.deadline_seconds)) as watch:
                connection, response = self._open(watch, "GET", self.path)
                try:
                    data = response.read(MAX_JSON_RESPONSE_BYTES + 1)
                    watch.remaining()
                    if len(data) > MAX_JSON_RESPONSE_BYTES:
                        raise RequestError(502, "Upstream response exceeds relay limit.")
                    self.send_response(response.status)
                    self.send_header("Content-Type", response.getheader("Content-Type", "application/json"))
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.wfile.write(data)
                finally:
                    response.close()
                    connection.close()
        except RequestError as exc:
            self._error(exc)
        except (OSError, http.client.HTTPException):
            self._error(RequestError(502, "Existing model server is unavailable.", "upstream_unavailable"))

    def do_POST(self) -> None:
        if self.path not in {"/v1/chat/completions", "/chat/completions"}:
            self._error(RequestError(404, "Unsupported endpoint in the bounded chat relay.",
                                     "unsupported_endpoint"))
            return
        request_id = uuid.uuid4().hex
        started = time.monotonic()
        fields: dict[str, Any] = {"request_id": request_id}
        sent_headers = False
        observer = SSEObserver(started)
        try:
            if self.headers.get("Transfer-Encoding"):
                raise RequestError(411, "Chunked request bodies are outside this bounded MVP.")
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                raise RequestError(411, "Content-Length is required.") from None
            if not 0 < length <= MAX_BODY_BYTES:
                raise RequestError(413, "Request body exceeds the relay's 8 MiB limit.")
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise RequestError(400, "Incomplete request body.")
            try:
                body = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                raise RequestError(400, "Request body must be valid JSON.") from None
            validate_chat(body)
            fields["request_hash"] = request_hash(body["messages"])
            from .common import generation_for_request, transport_hash
            fields["transport_hash"] = transport_hash(body["messages"])
            fields.update(generation_for_request(self.server.state_dir,fields["request_hash"],fields["transport_hash"]) or {"context_generation":None})
            fields["selection_generation_unmatched"] = not bool(fields.get("context_generation"))
            from .governor import validated_policy
            policy = validated_policy(self.server.provenance['effective_settings'].get('generation_governor') or {})
            # Validate all output aliases before counting/admission. Semantic
            # backlog never changes the physical budget.
            output_limit = bounded_output(body,0,self.server.profile_config["context"])
            prompt_cap = self.server.profile_config["context"]-output_limit-GENERATION_MARGIN
            fields["physical_prompt_limit"] = prompt_cap
            with RequestWatch(self.connection, self.server.deadline_seconds) as watch:
                count_started = time.monotonic()
                prompt_tokens = self._count(watch, body)
                fields["input_tokens"] = prompt_tokens
                fields["active_tokens"] = prompt_tokens
                fields["pressure_band"] = ("emergency" if prompt_tokens > (44000 if self.server.profile=="fast" else 50000)
                                            else "burst" if prompt_tokens > (36000 if self.server.profile=="fast" else 40000)
                                            else "preferred")
                message_only = dict(body)
                for key in ("tools", "tool_choice", "parallel_tool_calls", "functions", "function_call"):
                    message_only.pop(key, None)
                try:
                    message_tokens = self._count(watch, message_only) if message_only != body else prompt_tokens
                    overhead = max(0, prompt_tokens - message_tokens)
                    head = []
                    for message in body['messages']:
                        if message.get('role') not in ('system', 'developer'):
                            break
                        head.append(message)
                    system_tokens = self._count(watch, {**message_only, 'messages': head}) if head else 0
                    from .common import digest
                    from .schema_budget import schema_identity
                    schema_fp, has_tools = schema_identity(body)
                    fields.update(write_overhead(self.server.state_dir, self.server.profile, overhead,
                                   session_id=fields.get("session_id"),
                                   request_class="tool_bearing" if has_tools else "no_tools",
                                   schema_fingerprint=schema_fp, has_tools=has_tools,
                                   system_tokens=system_tokens, system_head_hash=digest(head),
                                   generation_allowance=output_limit, physical_prompt_allowance=prompt_cap))
                    fields['system_tokens'] = system_tokens
                    fields['content_tokens'] = max(0, message_tokens-system_tokens)
                    fields["tool_overhead_tokens"] = overhead
                except RequestError:
                    # Exact full request was counted; a message-only tool template
                    # may reject existing tool results. Keep the previous reserve.
                    fields["tool_overhead_unavailable"] = True
                if prompt_tokens > prompt_cap:
                    # One emergency pass, independent of generation association.
                    fields['emergency_admission_attempted'] = True
                    fields['emergency_overage_tokens'] = prompt_tokens-prompt_cap
                    try:
                        from types import SimpleNamespace
                        from .common import StateStore, TokenCounter, digest
                        from .admission import bounded_fallback
                        original_messages = body['messages']
                        split = 0
                        while split < len(original_messages) and original_messages[split].get('role') in ('system','developer'):
                            split += 1
                        if any(not isinstance(m.get('content'), (str,type(None))) or
                               m.get('role') in ('system','developer') for m in original_messages[split:]):
                            raise ValueError('Unsupported emergency request form')
                        archive_session = fields.get('session_id') or 'relay-emergency-'+self.server.profile+'-'+request_id
                        store = StateStore(self.server.state_dir)
                        text_counter = TokenCounter('http://127.0.0.1:'+str(self.server.profile_config['upstream_port']))
                        handler = self
                        class FinalWireCounter:
                            def text(self, text):
                                return text_counter.text(text)
                            def messages(self, messages):
                                return handler._count(watch, {**body, 'messages':messages})
                        view = SimpleNamespace(store=store, session_id=archive_session,
                                               counter=FinalWireCounter())
                        reduced, _, recovery = bounded_fallback(view, original_messages[:split],
                            original_messages[split:], prompt_cap,
                            next((m.get('content') or '' for m in reversed(original_messages) if m.get('role')=='user'), ''),
                            scoped_sources=not bool(fields.get('session_id')))
                        proposal = {**body, 'messages':reduced}
                        exact_reduced = self._count(watch, proposal)
                        fields['emergency_reduced_prompt_tokens'] = exact_reduced
                        if exact_reduced <= prompt_cap:
                            body = proposal; prompt_tokens = exact_reduced
                            fields['selection_context_generation'] = fields.get('context_generation')
                            fields['request_hash'] = request_hash(reduced)
                            fields['transport_hash'] = transport_hash(reduced)
                            try:
                                fields['context_generation'] = store.generation(archive_session,
                                    fields['request_hash'], [], 0, transport_alias=fields['transport_hash'])
                                fields['session_id'] = archive_session
                                fields['generation_link_basis'] = 'relay_emergency_selection'
                            except Exception:
                                fields['context_generation'] = None
                            fields['input_tokens'] = prompt_tokens
                            fields['active_tokens'] = prompt_tokens
                            fields['emergency_admission_recovered'] = True
                            fields['emergency_raw_manifest'] = recovery.get('manifest_source_id')
                            # Overhead learned from the original exact wire is
                            # unchanged; no-tool exclusion policy remains intact.
                            reduced_message_only = {**message_only, 'messages':reduced}
                            fields['content_tokens'] = max(0, self._count(watch, reduced_message_only)-fields.get('system_tokens',0))
                            emit(self.server.state_dir, self.server.profile, 'emergency_admission_recovered', **fields)
                    except Exception as error:
                        fields['emergency_admission_error_type'] = type(error).__name__
                    fields.setdefault('emergency_admission_recovered', False)
                if prompt_tokens > prompt_cap:
                    emit(self.server.state_dir, self.server.profile, "prompt_guard_rejected", **fields,
                         prompt_cap=prompt_cap)
                    raise RequestError(413, f"Prompt has {prompt_tokens} tokens including tools; the active "
                                       f"physical prompt allowance is {prompt_cap} after reserving generation. "
                                       "The protected working set cannot fit safely after bounded RAW/source recovery.", "context_limit")
                fields["count_ms"] = round((time.monotonic() - count_started) * 1000, 3)
                fields["output_limit_tokens"] = bounded_output(body, prompt_tokens,
                                                              self.server.profile_config["context"])
                # Ask llama.cpp for final usage/timings where this release supports it.
                # The response bytes themselves are always relayed without alteration.
                if body.get("stream"):
                    stream_options = body.get("stream_options", {})
                    if not isinstance(stream_options, dict):
                        raise RequestError(400, "stream_options must be an object.")
                    body["stream_options"] = {**stream_options, "include_usage": True}
                body["timings_per_token"] = True
                emit(self.server.state_dir, self.server.profile, "relay_request_started", **fields,
                     context_budget=self.server.profile_config["context"])
                if policy['enabled']:
                    from .governed_stream import completion
                    completion(self, watch, body, policy, fields, prompt_cap)
                    fields['duration_ms'] = round((time.monotonic()-started)*1000, 3)
                    emit(self.server.state_dir, self.server.profile, 'relay_request_completed', **fields)
                    write_feedback(self.server.state_dir, self.server.profile, fields)
                    return
                inference_started = time.monotonic()
                connection, response = self._open(watch, "POST", self.path, body)
                try:
                    fields["upstream_status"] = response.status
                    is_sse = "text/event-stream" in response.getheader("Content-Type", "")
                    if is_sse:
                        self.send_response(response.status)
                        self.send_header("Content-Type", response.getheader("Content-Type", "text/event-stream"))
                        self.send_header("Connection", "close")
                        self.send_header("X-Rolling-Context-Request-Id", request_id)
                        self.send_header("Cache-Control", "no-cache")
                        # HTTP/1.0 close framing: do not advertise stale upstream length
                        # or copy its chunked Transfer-Encoding after decoding it.
                        self.end_headers()
                        sent_headers = True
                        while True:
                            chunk = response.read1(16384)
                            if not chunk:
                                break
                            watch.remaining()
                            self.wfile.write(chunk)
                            self.wfile.flush()
                            observer.consume(chunk)
                        observer.finish()
                        fields["stream_done"] = observer.done
                        fields["observation_truncated"] = observer.observation_truncated
                    else:
                        data = response.read(MAX_JSON_RESPONSE_BYTES + 1)
                        watch.remaining()
                        if len(data) > MAX_JSON_RESPONSE_BYTES:
                            raise RequestError(502, "Completion response exceeds relay limit.")
                        self.send_response(response.status)
                        self.send_header("Content-Type", response.getheader("Content-Type", "application/json"))
                        self.send_header("Connection", "close")
                        self.send_header("X-Rolling-Context-Request-Id", request_id)
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        sent_headers = True
                        self.wfile.write(data)
                        self.wfile.flush()
                        try:
                            observer.observe_json(json.loads(data))
                        except (ValueError, UnicodeDecodeError):
                            pass
                    fields["inference_ms"] = round((time.monotonic() - inference_started) * 1000, 3)
                    fields["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
                    fields["first_content_ms"] = observer.first_content_ms
                    fields["timings"] = observer.timings
                    fields["usage"] = observer.usage
                    fields["reasoning_observation"] = observer.metrics()
                    if observer.timings:
                        for key in ("prompt_n", "prompt_ms", "prompt_per_second", "cache_n", "predicted_n", "predicted_ms", "predicted_per_second"):
                            if key in observer.timings:
                                fields[key] = observer.timings[key]
                    emit(self.server.state_dir, self.server.profile, "relay_request_completed", **fields)
                    write_feedback(self.server.state_dir, self.server.profile, fields)
                finally:
                    response.close()
                    connection.close()
        except RequestError as exc:
            emit(self.server.state_dir, self.server.profile, "relay_request_error", **fields,
                 error_code=exc.code, status=exc.status,
                 duration_ms=round((time.monotonic() - started) * 1000, 3))
            if not sent_headers and not getattr(self, '_governor_headers', False):
                try:
                    self._error(exc)
                except (OSError, http.client.HTTPException):
                    pass
        except (OSError, http.client.HTTPException) as exc:
            reason = watch.reason if "watch" in locals() and watch.reason else type(exc).__name__
            # Socket expiry may win the watchdog's next 100 ms tick. Consult
            # the same deadline instead of misreporting cancellation as 502.
            if "watch" in locals() and not watch.reason and time.monotonic() >= watch.deadline:
                reason = "deadline"
            emit(self.server.state_dir, self.server.profile, "relay_request_error", **fields,
                 error_code=reason, duration_ms=round((time.monotonic() - started) * 1000, 3))
            if not sent_headers and not getattr(self, '_governor_headers', False):
                try:
                    self._error(RequestError(504 if reason == "deadline" else 502,
                                "Request deadline reached." if reason == "deadline" else
                                "Model server connection failed or client disconnected.", str(reason)))
                except (OSError, http.client.HTTPException):
                    pass
        finally:
            self.close_connection = True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=PROFILES, required=True)
    parser.add_argument("--state-dir", default="~/.local/state/local-ai-control-centre/rolling-context")
    parser.add_argument("--port", type=int)
    parser.add_argument("--deadline-seconds", type=float, default=600)
    args = parser.parse_args(argv)
    if args.port is not None and not 1024 <= args.port <= 65535:
        parser.error("--port must be an unprivileged TCP port from 1024 to 65535")
    if not 1 <= args.deadline_seconds <= 3600:
        parser.error("--deadline-seconds must be from 1 to 3600")
    server = RelayServer(args.profile, args.state_dir, args.port, args.deadline_seconds)
    print(f"Rolling context relay: http://127.0.0.1:{server.server_port}/v1 -> "
          f"127.0.0.1:{server.profile_config['upstream_port']} ({args.profile})", flush=True)
    emit(server.state_dir, server.profile, "relay_started", relay_port=server.server_port,
         upstream_port=server.profile_config["upstream_port"], generation_margin=GENERATION_MARGIN, **server.provenance)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
