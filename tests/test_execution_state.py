import json
import tempfile
import unittest
from test_loop_guard import Harness, Counter
import test_loop_guard
from rolling_context.loop_guard import ReasoningLoopGuard
from rolling_context.common import digest, wire_message

class StateHarness(Harness):
    def __init__(self, user='Build the coding project.'):
        self.events=[];self.history=[];self.session='execution';self.counter=Counter()
        self.guard=ReasoningLoopGuard({'enabled':True,'task_mode':'implementation'},
                                     lambda event,**fields:self.events.append({'event':event,**fields}),
                                     {'enabled':True,'max_tokens':250})
        self.append('user',user)

class ExecutionStateTests(unittest.TestCase):
    def test_explore_implement_after_bounded_plan(self):
        h=StateHarness();self.assertEqual(h.guard.execution_mode,'EXPLORE')
        h.append('assistant','Architecture: use one module and implement it.')
        self.assertEqual(h.guard.execution_mode,'IMPLEMENT');self.assertEqual(h.guard.action_debt,1)
    def test_inspection_is_bounded_and_unchanged_read_does_not_reopen(self):
        h=StateHarness()
        h.tool('read_file',{'path':'a.py'},{'path':'a.py','content':'x=1'})
        self.assertEqual(h.guard.execution_mode,'IMPLEMENT')
        h.tool('read_file',{'path':'a.py'},{'path':'a.py','content':'x=1'})
        self.assertEqual(h.guard.execution_mode,'IMPLEMENT')
        h.tool('read_file',{'path':'a.py'},{'path':'a.py','content':'x=2'})
        self.assertEqual(h.guard.execution_mode,'EXPLORE')
    def test_failure_debug_write_then_verified_implement(self):
        h=StateHarness();h.append('assistant','Plan: implement now.')
        h.tool('terminal',{'command':'pytest test_privacy.py'},{'exit_code':1,'output':'FAILED alias leak'})
        self.assertEqual(h.guard.execution_mode,'DEBUG')
        h.tool('write_file',{'path':'a.py','content':'x=2'},{'files_modified':['a.py']})
        self.assertEqual(h.guard.execution_mode,'DEBUG');self.assertEqual(h.guard.action_debt,0)
        h.tool('terminal',{'command':'pytest test_privacy.py'},{'exit_code':0,'output':'1 passed'})
        self.assertEqual(h.guard.execution_mode,'IMPLEMENT')
    def test_new_user_reopens_and_explicit_review_not_forced(self):
        h=StateHarness();h.append('assistant','Plan: implement now.')
        h.append('user','Build a different UI with new requirements.')
        self.assertEqual(h.guard.execution_mode,'EXPLORE')
        h.append('user','Review the implementation and design only.')
        self.assertIsNone(h.guard.render_execution(h.counter));self.assertFalse(h.guard.active)
    def test_analysis_design_diagnosis_not_forced_even_profile_implementation(self):
        for verb in ('Analyze','Review','Design','Diagnose','Explain'):
            h=StateHarness(verb+' the coding project.')
            h.loop();self.assertIsNone(h.guard.render_execution(h.counter));self.assertFalse(h.guard.active)
    def test_pressure_bounded_resets_writes_and_tests(self):
        h=StateHarness()
        for _ in range(30):h.append('assistant','Plan: implement module and architecture.')
        self.assertEqual(h.guard.action_debt,20)
        h.tool('write_file',{'path':'a.py','content':'pass'},{'files_modified':['a.py']})
        self.assertEqual(h.guard.action_debt,0)
        h.append('assistant','Plan: implement module.');self.assertEqual(h.guard.action_debt,1)
        h.tool('terminal',{'command':'pytest'},{'exit_code':0,'output':'passed'})
        self.assertEqual(h.guard.action_debt,0)
    def test_render_bounded_and_no_private_fields(self):
        h=StateHarness();h.append('assistant','Plan: implement now.',reasoning_content='PRIVATE')
        block=h.guard.render_execution(h.counter)
        self.assertEqual(block['role'],'system');self.assertLessEqual(h.counter.text(block['content']),250)
        self.assertNotIn('PRIVATE',block['content']);self.assertNotIn('PRIVATE',json.dumps(h.events))
        self.assertNotIn(block,h.history)
    def test_new_session_and_restart_clear_debt(self):
        h=StateHarness();h.loop();self.assertGreater(h.guard.action_debt,0)
        h.session='new';h.history=[];h.append('user','Build a new project.')
        self.assertEqual(h.guard.action_debt,0);self.assertEqual(h.guard.execution_mode,'EXPLORE')
        fresh=ReasoningLoopGuard({'enabled':True},lambda *a,**k:None,{'enabled':True})
        fresh.observe('old',[{'role':'user','content':'Build.'},{'role':'assistant','content':'Plan: implement.'}],['u','a'])
        self.assertEqual(fresh.action_debt,0)
    def test_selection_block_absent_raw_and_limits_preserved(self):
        helper=test_loop_guard.EngineIntegrationTests()
        with tempfile.TemporaryDirectory() as root:
            e=helper.make(root);e.settings['execution_state']={'enabled':True}
            history=[{'role':'user','content':'Build a small game.'}]
            selected=e.select_context(history,conversation_messages=history)
            self.assertTrue(any('EXECUTION STATE' in m.get('content','') for m in selected))
            self.assertFalse(any('EXECUTION STATE' in m.get('content','') for m in history))
            with e.store.connect() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM records WHERE body LIKE '%EXECUTION STATE%'").fetchone()[0],0)
            for m in history:self.assertEqual(e.store.record(e.session_id,digest(wire_message(m))),wire_message(m))
            self.assertEqual(e.context_length,73728)
            self.assertEqual((e.settings['generation_reserve_tokens'],e.settings['target_tokens'],e.settings['trigger_tokens']),(8192,32000,36000))
