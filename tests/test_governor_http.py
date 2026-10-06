"""Transport-level correction, owned cancellation, privacy and physical admission."""
import json
import socket
import time
import unittest
from unittest.mock import patch
from test_relay import RelayTests, FakeHandler
from rolling_context.governor import DIAGNOSTIC
from rolling_context.relay import RequestWatch

PRIVATE='PRIVATE_REASONING_MUST_NOT_ESCAPE'

class GovernorHTTPTests(RelayTests):
    def setUp(self):
        super().setUp()
        self.relay.provenance['effective_settings']['generation_governor']={'enabled':True}
        self.attempts=0
        self.second_productive=False
        self.productive=False
        self.use_tool=False
        self.length_only=False
        self.short_output=False
        self.cancelled=0
        self.original=FakeHandler.do_POST
        self.patcher=patch.object(FakeHandler,'do_POST',self.handler())
        self.patcher.start()
    def tearDown(self):
        self.patcher.stop();super().tearDown()
    def handler(self):
        test=self
        def serve(h):
            if 'chat/completions' not in h.path:return test.original(h)
            body=json.loads(h.rfile.read(int(h.headers['Content-Length'])))
            h.server.calls.append((h.path,body));test.attempts+=1
            attempt=test.attempts
            h.send_response(200);h.send_header('Content-Type','text/event-stream');h.send_header('Connection','close');h.end_headers()
            def send(delta,count=0,finish=None):
                obj={'choices':[{'index':0,'delta':delta,'finish_reason':finish}],
                     'timings':{'predicted_n':count,'secret':PRIVATE},'usage':{'secret':PRIVATE}}
                raw=('data: '+json.dumps(obj)+'\n\n').encode()
                # Deliberately split SSE across writes.
                h.wfile.write(raw[:9]);h.wfile.flush();h.wfile.write(raw[9:]);h.wfile.flush()
            try:
                productive=test.productive or (test.second_productive and attempt==2)
                if productive:
                    if test.use_tool:
                        send({'tool_calls':[{'index':0,'id':'call1','type':'function','function':{'name':'write_file','arguments':'{"path":'}}]},2)
                        send({'tool_calls':[{'index':0,'function':{'arguments':'"a.py"}'}}]},8192,'tool_calls')
                    else:
                        send({'reasoning_content':PRIVATE},2)
                        send({'content':'x'*10000},5000)
                        send({'content':'done.'},8192,'stop')
                elif test.length_only:
                    send({'reasoning_content':PRIVATE},5)
                    send({'content':'...' if test.short_output else ''},8192,'length')
                else:
                    send({'reasoning_content':PRIVATE},3840)
                    h.connection.settimeout(1)
                    if not h.connection.recv(1): test.cancelled+=1
                    return
                h.wfile.write(b'data: [DONE]\n\n');h.wfile.flush()
            except (OSError, socket.timeout): pass
        return serve
    def result(self,stream=False):
        conn,response=self.post(self.body(stream=stream,max_tokens=8192,reasoning_effort='medium'))
        data=response.read();status=response.status;conn.close()
        self.assertEqual(status,200);self.assertNotIn(PRIVATE.encode(),data)
        return data
    def test_one_retry_second_trip_stop_and_owned_cancellation(self):
        with patch.object(RequestWatch,'close_upstreams',autospec=True,side_effect=RequestWatch.close_upstreams) as cancel:
            data=self.result();self.assertEqual(cancel.call_count,2)
        obj=json.loads(data);self.assertEqual(obj['choices'][0]['finish_reason'],'stop')
        self.assertEqual(obj['choices'][0]['message']['content'],DIAGNOSTIC)
        self.assertEqual(self.attempts,2)
        events=self.events();self.assertEqual(sum(e['event']=='generation_governor_retry' for e in events),1)
        self.assertTrue(any(e['event']=='generation_governor_stopped' for e in events))
        self.assertNotIn(PRIVATE,(self.state/'telemetry.jsonl').read_text())
        chats=[b for p,b in self.fake.calls if 'chat/completions' in p]
        self.assertEqual(chats[0]['reasoning_effort'],'medium');self.assertEqual(chats[1]['reasoning_effort'],'none')
        self.assertTrue(all(b['max_tokens']==8192 and b['stream'] for b in chats))
        self.assertEqual(sum('GENERATION GOVERNOR' in m['content'] for m in chats[1]['messages']),1)
    def test_corrective_retry_recovers_json_without_private_content(self):
        self.second_productive=True
        result=json.loads(self.result());self.assertEqual(self.attempts,2)
        self.assertTrue(result['choices'][0]['message']['content'].endswith('done.'))
        self.assertEqual(result['choices'][0]['finish_reason'],'stop')
    def test_streaming_retry_emits_single_clean_completion(self):
        self.second_productive=True
        data=self.result(True);self.assertEqual(data.count(b'data: [DONE]'),1)
        self.assertNotIn(b'"finish_reason": "length"',data);self.assertEqual(self.attempts,2)
    def test_normal_long_visible_stream_no_retry(self):
        self.productive=True;data=self.result(True)
        self.assertIn(b'done.',data);self.assertEqual(self.attempts,1)
    def test_tool_call_fragments_preserved_json(self):
        self.productive=True;self.use_tool=True
        result=json.loads(self.result());choice=result['choices'][0]
        self.assertEqual(choice['finish_reason'],'tool_calls')
        self.assertEqual(choice['message']['tool_calls'][0]['function']['arguments'],'{"path":"a.py"}')
        self.assertEqual(self.attempts,1)
    def test_length_without_visible_output_never_reaches_client(self):
        self.length_only=True
        data=self.result(True);self.assertIn(DIAGNOSTIC.encode(),data)
        self.assertNotIn(b'"finish_reason": "length"',data);self.assertEqual(self.attempts,2)
    def test_near_empty_length_is_held_before_commit(self):
        self.length_only=True;self.short_output=True
        data=self.result(True);self.assertNotIn(b'"content": "..."',data);self.assertEqual(self.attempts,2)
    def test_retry_recount_blocks_over_physical_limit(self):
        original=self.relay.RequestHandlerClass._count
        def count(handler,watch,body):
            if any('GENERATION GOVERNOR' in m.get('content','') for m in body['messages']):return 60000
            return original(handler,watch,body)
        with patch.object(self.relay.RequestHandlerClass,'_count',count):data=self.result()
        self.assertEqual(self.attempts,1);self.assertEqual(json.loads(data)['choices'][0]['finish_reason'],'stop')
        self.assertTrue(any(e.get('reason_code')=='retry_prompt_limit' for e in self.events()))
    def test_disabled_uses_original_transport(self):
        self.relay.provenance['effective_settings']['generation_governor']={'enabled':False}
        with patch.object(FakeHandler,'do_POST',self.original):
            data=self.result()
        self.assertEqual(json.loads(data)['choices'][0]['message']['content'],'ok')
    def test_deadline_does_not_gain_retry_time(self):
        self.relay.deadline_seconds=.1
        def slow(h):
            if 'chat/completions' not in h.path:return self.original(h)
            h.rfile.read(int(h.headers['Content-Length']));time.sleep(.4)
        with patch.object(FakeHandler,'do_POST',slow):
            conn,response=self.post(self.body(max_tokens=8192));data=response.read();conn.close()
        self.assertEqual(response.status,504);self.assertEqual(json.loads(data)['error']['code'],'deadline')


def load_tests(loader,tests,pattern):
    return unittest.TestSuite(GovernorHTTPTests(name) for name in GovernorHTTPTests.__dict__ if name.startswith('test_'))
