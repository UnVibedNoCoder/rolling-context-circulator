"""HTTP-level relay tests with small fake llama endpoints; no model inference."""
import fcntl
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest

from rolling_context.relay import PROFILES, RelayServer, RequestError, SSEObserver, bounded_output


SSE_PARTS = [
    b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n',
    'data: {"choices":[{"delta":{"content":"Hello 🦙"}}]}\n\n'.encode(),
    b'data: {"choices":[],"usage":{"prompt_tokens":14,"completion_tokens":2},'
    b'"timings":{"prompt_n":7,"cache_n":7,"prompt_ms":123.0,"prompt_per_second":56.9}}\n\n',
    b'data: [DONE]\n\n',
]


class FakeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def respond(self, value, status=200):
        raw = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self.respond({"status": "ok"} if "health" in self.path else {"data": [{"id": "test-model"}]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.calls.append((self.path, body))
        if self.path == "/apply-template":
            if self.server.count_fail:
                self.respond({"error": {"message": "not available"}}, 404)
            else:
                count = self.server.prompt_count + (self.server.tool_overhead if body.get("tools") else 0)
                self.respond({"prompt": str(count)})
        elif self.path == "/tokenize":
            self.respond({"tokens": [0] * int(body["content"])})
        else:
            self.server.chat_entered.set()
            if self.server.slow:
                self.connection.settimeout(3)
                try:
                    if self.connection.recv(1) == b"":
                        self.server.chat_cancelled.set()
                except OSError:
                    pass
                return
            if body.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Connection", "close")
                self.end_headers()
                for part in SSE_PARTS:
                    self.wfile.write(f"{len(part):x}\r\n".encode() + part + b"\r\n")
                    self.wfile.flush()
                    time.sleep(0.05)
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            else:
                self.respond({"choices": [{"message": {"content": "ok"}}],
                              "usage": {"prompt_tokens": 14, "completion_tokens": 2},
                              "timings": {"prompt_n": 7, "cache_n": 7, "prompt_ms": 123.0,
                                          "prompt_per_second": 56.9}})


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = Path(self.temp.name) / "state"
        self.fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeHandler)
        self.fake.daemon_threads = True
        self.fake.calls = []
        self.fake.prompt_count = 14
        self.fake.tool_overhead = 10
        self.fake.count_fail = False
        self.fake.slow = False
        self.fake.chat_entered = threading.Event()
        self.fake.chat_cancelled = threading.Event()
        self.profile_before = dict(PROFILES["fast"])
        PROFILES["fast"]["upstream_port"] = self.fake.server_port
        self.relay = RelayServer("fast", self.state, port=0, deadline_seconds=2)
        self.fake_thread = threading.Thread(target=self.fake.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.relay_thread = threading.Thread(target=self.relay.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.fake_thread.start()
        self.relay_thread.start()

    def tearDown(self):
        self.relay.shutdown()
        self.relay.server_close()
        self.fake.shutdown()
        self.fake.server_close()
        self.relay_thread.join(1)
        self.fake_thread.join(1)
        PROFILES["fast"].clear()
        PROFILES["fast"].update(self.profile_before)
        self.temp.cleanup()

    def post(self, body, path="/v1/chat/completions"):
        conn = http.client.HTTPConnection("127.0.0.1", self.relay.server_port, timeout=5)
        conn.request("POST", path, json.dumps(body), {"Content-Type": "application/json"})
        response = conn.getresponse()
        return conn, response

    def events(self):
        path = self.state / "telemetry.jsonl"
        for _ in range(100):
            if path.exists():
                # The live relay writer locks each append. Take the matching
                # read lock so an active write cannot look like corrupt JSON.
                # Completed malformed records still fail json.loads below.
                with path.open() as stream:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
                    snapshot = stream.read()
                events = [json.loads(line) for line in snapshot.splitlines()]
                if any(event["event"] in {"relay_request_completed", "relay_request_error"} for event in events):
                    return events
            time.sleep(0.01)
        self.fail("Final telemetry did not arrive")

    def body(self, **fields):
        return {"messages": [{"role": "user", "content": "secret-user-input"}],
                "model": "local", "max_tokens": 100, **fields}

    def test_json_exact_count_includes_tools_and_private_telemetry(self):
        tools = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]
        conn, response = self.post(self.body(tools=tools))
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.read())["choices"][0]["message"]["content"], "ok")
        conn.close()
        completion = [e for e in self.events() if e["event"] == "relay_request_completed"][0]
        self.assertEqual(completion["input_tokens"], 24)
        self.assertEqual(completion["tool_overhead_tokens"], 10)
        self.assertEqual(completion["prompt_ms"], 123.0)
        self.assertEqual(completion["cache_n"], 7)
        self.assertIn("request_hash", completion)
        self.assertNotIn("secret-user-input", (self.state / "telemetry.jsonl").read_text())
        self.assertEqual(os.stat(self.state / "telemetry.jsonl").st_mode & 0o777, 0o600)
        self.assertEqual(json.loads((self.state / "fast-overhead.json").read_text())["overhead_tokens"], 10)
        counts = [body for path, body in self.fake.calls if path == "/tokenize"]
        self.assertTrue(all(body["add_special"] and body["parse_special"] for body in counts))
        outgoing = [body for path, body in self.fake.calls if "chat/completions" in path][0]
        self.assertEqual(outgoing["max_tokens"], 100)
        self.assertEqual(outgoing["n_predict"], 100)

    def test_auxiliary_http_does_not_erase_tool_schema_measurement(self):
        from rolling_context.schema_budget import schema_identity
        tools = [{'type':'function','function':{'name':'lookup','parameters':{'type':'object'}}}]
        self.fake.tool_overhead = 10845
        for body in [self.body(tools=tools), self.body(), self.body()]:
            conn,response=self.post(body)
            self.assertEqual(response.status,200);response.read();conn.close()
        saved=json.loads((self.state/'fast-overhead.json').read_text())
        self.assertEqual(saved['overhead_tokens'],10845)
        self.assertTrue(saved['auxiliary_excluded_from_main'])
        self.assertEqual(saved['schema_fingerprint'],schema_identity(self.body(tools=tools))[0])
        changed=json.loads(json.dumps(tools));changed[0]['function']['description']='Additional schema details'
        self.assertNotEqual(schema_identity({'tools':tools})[0],schema_identity({'tools':changed})[0])
        self.fake.tool_overhead=13721
        conn,response=self.post(self.body(tools=changed));response.read();conn.close()
        saved=json.loads((self.state/'fast-overhead.json').read_text())
        self.assertEqual(saved['overhead_tokens'],13721)
        self.assertFalse(saved['auxiliary_excluded_from_main'])

    def test_sse_bytes_preserved_and_close_framing(self):
        conn, response = self.post(self.body(stream=True))
        self.assertEqual(response.status, 200)
        self.assertEqual(response.version, 10)
        self.assertIsNone(response.getheader("Content-Length"))
        self.assertIsNone(response.getheader("Transfer-Encoding"))
        first = response.read1(16384)
        self.assertTrue(first)
        self.assertEqual(first + response.read(), b"".join(SSE_PARTS))
        conn.close()
        completion = [e for e in self.events() if e["event"] == "relay_request_completed"][0]
        self.assertTrue(completion["stream_done"])
        self.assertIsNotNone(completion["first_content_ms"])
        self.assertEqual(completion["usage"]["completion_tokens"], 2)
        self.assertEqual(completion["prompt_n"], 7)
        outgoing = [body for path, body in self.fake.calls if "chat/completions" in path][0]
        self.assertTrue(outgoing["stream_options"]["include_usage"])

    def test_prompt_cap_prevents_inference(self):
        self.fake.prompt_count = 65536
        conn, response = self.post(self.body())
        self.assertEqual(response.status, 413)
        self.assertEqual(json.loads(response.read())["error"]["code"], "context_limit")
        conn.close()
        self.assertFalse(self.fake.chat_entered.is_set())

    def test_above_old_watermark_is_admitted_when_physical_budget_allows(self):
        self.fake.prompt_count=51000
        conn,response=self.post(self.body())
        self.assertEqual(response.status,200)
        response.read();conn.close()
        self.assertTrue(self.fake.chat_entered.is_set())
        event=next(e for e in self.events() if e["event"]=="relay_request_completed")
        self.assertEqual(event["pressure_band"],"emergency")

    def test_exact_counter_failure_prevents_inference(self):
        self.fake.count_fail = True
        conn, response = self.post(self.body())
        self.assertEqual(response.status, 502)
        self.assertEqual(json.loads(response.read())["error"]["code"], "count_unavailable")
        conn.close()
        self.assertFalse(self.fake.chat_entered.is_set())

    def test_unsupported_inputs_and_alias_output_limits_fail_closed(self):
        for fields in [{"messages": [{"role": "user", "content": [{"type": "image_url"}]}]},
                       {"n_predict": 100000}, {"max_tokens": -1}, {"max_completion_tokens": 10000}]:
            conn, response = self.post(self.body(**fields))
            self.assertEqual(response.status, 400)
            response.read()
            conn.close()
        self.assertFalse(self.fake.chat_entered.is_set())
        conn, response = self.post(self.body(), path="/v1/responses")
        self.assertEqual(response.status, 404)
        response.read()
        conn.close()

    def test_output_budget_is_context_bounded(self):
        body = {"max_tokens": 8192}
        self.assertEqual(bounded_output(body, 65000, 65536), 536)
        self.assertEqual(body["max_tokens"], 536)
        with self.assertRaises(RequestError):
            bounded_output({"n_predict": 100000}, 14, 65536)

    def test_deadline_cancels_upstream_socket(self):
        self.fake.slow = True
        self.relay.deadline_seconds = 0.3
        conn, response = self.post(self.body())
        self.assertEqual(response.status, 504)
        response.read()
        conn.close()
        self.assertTrue(self.fake.chat_cancelled.wait(2))

    def test_client_disconnect_cancels_upstream_socket(self):
        self.fake.slow = True
        client = socket.create_connection(("127.0.0.1", self.relay.server_port), timeout=3)
        raw = json.dumps(self.body()).encode()
        client.sendall(b"POST /v1/chat/completions HTTP/1.1\r\nHost: localhost\r\nContent-Length: " +
                       str(len(raw)).encode() + b"\r\n\r\n" + raw)
        self.assertTrue(self.fake.chat_entered.wait(2))
        client.close()
        self.assertTrue(self.fake.chat_cancelled.wait(2))

    def test_marker_health_and_upstream_discovery(self):
        for path in ["/rolling-context/health", "/health", "/v1/models"]:
            conn = http.client.HTTPConnection("127.0.0.1", self.relay.server_port, timeout=3)
            conn.request("GET", path)
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            data = json.loads(response.read())
            if path == "/rolling-context/health":
                self.assertEqual(data["service"], "rolling-context-relay")
                self.assertEqual(data["profile"], "fast")
            conn.close()

    def test_observer_handles_split_utf8_and_multiline_sse(self):
        observer = SSEObserver(time.monotonic())
        raw = 'data: {"choices":[{"delta":\r\ndata: {"content":"🦙"}}]}\r\n\r\ndata: [DONE]\n\n'.encode()
        for byte in raw:
            observer.consume(bytes([byte]))
        observer.finish()
        self.assertTrue(observer.done)
        self.assertIsNotNone(observer.first_content_ms)


if __name__ == "__main__":
    unittest.main()
