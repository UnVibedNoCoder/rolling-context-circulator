"""Actual selector-to-relay HTTP on ephemeral ports with an isolated fake model."""
import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from test_relay import FakeHandler
from test_loop_guard import EngineIntegrationTests,Counter
from rolling_context.relay import PROFILES,RelayServer

class LoopGuardHTTPTests(unittest.TestCase):
    def test_control_crosses_counted_wire_and_clearing_stays_transient(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);fixture=EngineIntegrationTests()
            engine=fixture.make(root)
            history,selected=fixture.feed(engine)
            fake=ThreadingHTTPServer(('127.0.0.1',0),FakeHandler)
            fake.daemon_threads=True;fake.calls=[];fake.prompt_count=20;fake.tool_overhead=0
            fake.count_fail=False;fake.slow=False
            fake.chat_entered=threading.Event();fake.chat_cancelled=threading.Event()
            isolated={**PROFILES['large'],'upstream_port':fake.server_port}
            with patch.dict(PROFILES,{'large':isolated}):
                relay=RelayServer('large',root,port=0,deadline_seconds=3)
                threads=[threading.Thread(target=s.serve_forever,kwargs={'poll_interval':.02},daemon=True) for s in (fake,relay)]
                for t in threads:t.start()
                try:
                    self.assertNotIn(fake.server_port,(8083,8084,8094));self.assertNotIn(relay.server_port,(8083,8084,8094))
                    body={'model':'qwen38-27b-atx-73k','messages':selected,'max_tokens':8192,'stream':False}
                    conn=http.client.HTTPConnection('127.0.0.1',relay.server_port,timeout=5)
                    try:
                        conn.request('POST','/v1/chat/completions',json.dumps(body),{'Content-Type':'application/json'})
                        response=conn.getresponse();self.assertEqual(response.status,200);response.read()
                    finally:conn.close()
                    forwarded=next(b for path,b in fake.calls if path=='/v1/chat/completions')
                    self.assertEqual(forwarded['messages'],selected)
                    self.assertTrue(any(m['role']=='system' and 'EXECUTION CONTROL' in m['content'] for m in forwarded['messages']))
                    self.assertEqual(forwarded['max_tokens'],8192)
                    self.assertEqual(relay.profile_config['context'],73728)
                    with engine.store.connect() as db:
                        self.assertEqual(db.execute("SELECT count(*) FROM records WHERE body LIKE '%EXECUTION CONTROL%'").fetchone()[0],0)
                finally:
                    for s in (relay,fake):s.shutdown();s.server_close()
                    for t in threads:t.join(2)


def load_tests(loader,tests,pattern):
    return loader.loadTestsFromTestCase(LoopGuardHTTPTests)
