"""Deterministic error classification when socket expiry precedes watchdog tick."""
import json
import unittest
from unittest.mock import patch
from test_relay import RelayTests,FakeHandler
from rolling_context.relay import RequestWatch

class DeadlineRaceTests(RelayTests):
    def test_socket_deadline_before_watchdog_tick_is_504_and_cancels(self):
        self.fake.slow=True;self.relay.deadline_seconds=.15
        with patch.object(RequestWatch,'_run',lambda watch:watch.finished.wait(2)):
            conn,response=self.post(self.body())
            try:
                self.assertEqual(response.status,504)
                self.assertEqual(json.loads(response.read())['error']['code'],'deadline')
            finally:conn.close()
        self.assertTrue(self.fake.chat_cancelled.wait(2))

    def test_connection_failure_before_deadline_remains_502(self):
        original=FakeHandler.do_POST
        def broken(handler):
            if handler.path!='/v1/chat/completions':return original(handler)
            handler.rfile.read(int(handler.headers['Content-Length']))
            handler.connection.close()
        with patch.object(FakeHandler,'do_POST',broken):
            conn,response=self.post(self.body())
            try:self.assertEqual(response.status,502);response.read()
            finally:conn.close()


def load_tests(loader,tests,pattern):
    return unittest.TestSuite(DeadlineRaceTests(n) for n in DeadlineRaceTests.__dict__ if n.startswith('test_'))
