import json
import unittest
from test_continuity import ContinuityTests, coding_history
from rolling_context.relay import write_overhead

class SchemaAccountingTests(unittest.TestCase):
    setUp=ContinuityTests.setUp
    tearDown=ContinuityTests.tearDown
    select=ContinuityTests.select
    event=ContinuityTests.event
    def normal(self, tokens=10845, fingerprint='schema-a', session='long-coding'):
        return write_overhead(self.root,'large',tokens,session_id=session,request_class='tool_bearing',schema_fingerprint=fingerprint,has_tools=True)
    def auxiliary(self):
        return write_overhead(self.root,'large',0,session_id=None,request_class='no_tools',schema_fingerprint='no-tools',has_tools=False,system_tokens=238)
    def test_auxiliary_cannot_poison_next_normal_selection(self):
        self.normal();messages=coding_history();messages[0]['content']='s'*4294
        self.select(messages);before=self.event()
        for _ in range(6):self.auxiliary()
        self.engine._stable_frame=None
        selected=self.select(messages);after=self.event()
        self.assertEqual(after['tool_schema_tokens'],10845)
        self.assertEqual(after['content_capacity_tokens'],49371)
        self.assertLessEqual(self.engine.counter.messages(selected)+10845,64512)
        self.assertEqual(before['tool_schema_tokens'],after['tool_schema_tokens'])
    def test_changed_fingerprint_and_restart(self):
        self.normal();self.normal(13721,'schema-b')
        self.engine.settings['tool_schema_fingerprint']='schema-a'
        self.assertEqual(self.engine._reserve()-1024,10845)
        self.engine.settings['tool_schema_fingerprint']='schema-b'
        self.assertEqual(self.engine._reserve()-1024,13721)
        from rolling_context.engine import RollingContextEngine
        restarted=RollingContextEngine(settings={**self.engine.settings},counter=self.engine.counter)
        restarted.on_session_start('long-coding');restarted._stop.set()
        self.assertEqual(restarted._reserve()-1024,13721)
    def test_session_fallback_and_unknown_new_toolset(self):
        self.normal(12021,session='another-session');self.normal(10937,session='long-coding')
        self.engine.settings['tool_schema_fingerprint']='not-yet-measured'
        self.assertEqual(self.engine._reserve()-1024,10937)
        self.engine.on_session_start('new-session');self.engine._stop.set()
        self.assertEqual(self.engine._reserve()-1024,10937)
    def test_tools_disabled_requires_explicit_selection_knowledge(self):
        self.normal();self.auxiliary()
        self.assertGreater(self.engine._reserve()-1024,0)
        self.engine.settings['tools_enabled']=False
        self.assertEqual(self.engine._reserve()-1024,0)
        self.engine.settings['tools_enabled']=True
        self.assertEqual(self.engine._reserve()-1024,10845)
    def test_new_or_legacy_zero_measurement_is_unknown(self):
        (self.root/'large-overhead.json').write_text('{"overhead_tokens":0}')
        self.assertGreater(self.engine._reserve()-1024,0)
        (self.root/'large-overhead.json').unlink()
        self.auxiliary()
        self.assertGreater(self.engine._reserve()-1024,0)

del ContinuityTests
