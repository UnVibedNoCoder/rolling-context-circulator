import copy
import unittest
from test_continuity import ContinuityTests, coding_history, cycle

class BurstSettlingTests(unittest.TestCase):
    setUp=ContinuityTests.setUp
    tearDown=ContinuityTests.tearDown
    select=ContinuityTests.select
    event=ContinuityTests.event
    def burst(self):
        messages=coding_history();self.select(messages)
        messages+=cycle('pressure',14000);self.select(messages)
        self.assertGreater(self.event()['content_tokens'],36000)
        return messages
    def test_burst_settles_after_quiet_turns_then_keeps_prefix(self):
        messages=self.burst();before=self.event();events=[]
        for i in range(3):
            messages+=cycle('quiet-'+str(i),100);self.select(messages);events.append(self.event())
        self.assertFalse(events[0]['burst_settling']);self.assertFalse(events[1]['burst_settling'])
        self.assertTrue(events[2]['burst_settling'])
        self.assertLessEqual(events[-1]['content_tokens'],32000)
        self.assertGreaterEqual(events[-1]['tail_tokens'],19200)
        prefix=copy.deepcopy(self.engine._stable_frame['prefix'])
        for i in range(3,6):
            messages+=cycle('quiet-'+str(i),100);self.select(messages)
            self.assertFalse(self.event()['prefix_rebuilt'])
            self.assertEqual(self.engine._stable_frame['prefix'],prefix)
        self.assertLess(self.event()['content_tokens'],36000)
    def test_identical_selections_do_not_age_burst_and_new_pressure_restarts_grace(self):
        messages=self.burst()
        for _ in range(5):self.select(messages);self.assertFalse(self.event()['burst_settling'])
        messages+=cycle('quiet',100);self.select(messages)
        messages+=cycle('new-pressure',3500);self.select(messages)
        self.assertFalse(self.event()['burst_settling'])
        messages+=cycle('quiet-again',100);self.select(messages)
        self.assertFalse(self.event()['burst_settling'])
    def test_physical_source_projection_stays_identical_on_small_append(self):
        messages=coding_history()+cycle('large-source',65000)
        messages[-2]['content']='\n'.join(f'cache_line_{i} = ({i} * 7 + 19) % 97' for i in range(1800))
        selected=self.select(messages)
        self.assertGreater(self.event()['content_tokens'],36000)
        projections=copy.deepcopy(self.engine._stable_frame['source_overrides'])
        self.assertTrue(projections)
        messages+=cycle('small',50);next_selected=self.select(messages)
        for source,text in projections.items():
            self.assertEqual(self.engine._stable_frame['source_overrides'].get(source),text)
        self.assertLessEqual(self.event()['estimated_transport_prompt_tokens'],64512)
        for i in range(2):
            messages+=cycle('more-small-'+str(i),50);self.select(messages)
        self.assertTrue(self.event()['burst_settling'])
        self.assertLessEqual(self.event()['content_tokens'],32000)

del ContinuityTests
