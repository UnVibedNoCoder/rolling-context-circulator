"""Bounded visible-loop reproduction, negative cases and real selector integration."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path.home()/'.hermes/hermes-agent')]
from rolling_context.common import digest,dumps,wire_message
from rolling_context.loop_guard import ReasoningLoopGuard,DEFAULTS,visible_text
from rolling_context.engine import RollingContextEngine
from rolling_context.defaults import VALIDATED_POLICY

FIXTURE=json.loads((Path(__file__).parent/'fixtures/synthetic_coding_loop.json').read_text())
PRIVATE='PRIVATE_CHAIN_OF_THOUGHT_NEVER_LOGGED'

class Counter:
    def text(self,text):return (len(text)+3)//4
    def messages(self,messages):return sum(self.text(dumps(m)) for m in messages)
    def weights(self,messages):return [self.text(dumps(m)) for m in messages]

class Harness:
    def __init__(self,**settings):
        self.events=[];self.history=[];self.guard=ReasoningLoopGuard({'enabled':True,**settings},lambda event,**fields:self.events.append({'event':event,**fields}))
        self.session='fixture';self.counter=Counter()
        self.append('user',FIXTURE['user'])
    def append(self,role,content='',**fields):
        self.history.append({'role':role,'content':content,**fields})
        hashes=[digest(wire_message(m)) for m in self.history]
        self.guard.observe(self.session,self.history,hashes)
        message=self.guard.render(self.counter)
        if message:self.guard.delivered([message])
        return message
    def loop(self,n=6):
        self.append('assistant',FIXTURE['settled'])
        controls=[]
        for i in range(n):controls.append(self.append('assistant',FIXTURE['plans'][i%6]))
        return next((v for v in controls if v),None)
    def tool(self,name,args,result):
        ident='tool'+str(len(self.history))
        self.append('assistant',tool_calls=[{'id':ident,'type':'function','function':{'name':name,'arguments':dumps(args)}}])
        return self.append('tool',dumps(result),tool_call_id=ident)

class LoopGuardTests(unittest.TestCase):
    def test_synthetic_coding_repeated_plans_trigger_before_compaction_threshold(self):
        h=Harness();control=h.loop()
        self.assertIsNotNone(control)
        self.assertEqual(control['role'],'system')
        self.assertIn('SYSTEM-DERIVED, TRANSIENT',control['content'])
        self.assertIn('Planning is already sufficient',control['content'])
        self.assertIn('concrete implementation or verification',control['content'])
        self.assertIn('User labels',control['content'])
        self.assertLess(h.counter.messages(h.history),36000)
        trigger=next(e for e in h.events if e['event']=='loop_guard_triggered')
        self.assertEqual(trigger['reason_code'],'settled_decision_reopened_without_evidence')
        self.assertGreaterEqual(trigger['reopen_attempts'],3)

    def test_normal_coding_writes_and_verification_do_not_trigger(self):
        h=Harness()
        for i in range(12):
            h.append('assistant',FIXTURE['plans'][i%6])
            h.tool('write_file',{'path':'a.py','content':'value='+str(i)},{'bytes_written':10,'files_modified':['a.py'],'verified':True})
            h.tool('terminal',{'command':'python -m unittest'},{'exit_code':0,'output':str(i+1)+' tests passed'})
        self.assertFalse(any(e['event']=='loop_guard_triggered' for e in h.events))

    def test_long_productive_visible_response_does_not_trigger_by_length(self):
        h=Harness();h.append('assistant',('Detailed implementation and design facts. '*2000))
        h.tool('write_file',{'path':'a.py','content':'x=1'},{'bytes_written':3,'files_modified':['a.py']})
        self.assertFalse(h.guard.active)
        self.assertEqual(h.guard.no_progress_turns,0)
        self.assertFalse(any(e['event']=='loop_guard_triggered' for e in h.events))

    def test_real_file_modification_clears_intervention(self):
        h=Harness();h.loop();self.assertTrue(h.guard.active)
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'project.py';path.write_text('return {}\n')
            h.tool('write_file',{'path':str(path),'content':path.read_text()},{'bytes_written':path.stat().st_size,'files_modified':[str(path)],'verified':True})
        self.assertFalse(h.guard.active);self.assertEqual(h.guard.no_progress_turns,0)
        self.assertIsNone(h.guard.control)
        self.assertTrue(any(e['event']=='loop_guard_cleared' and e['reason_code']=='file_modified' for e in h.events))

    def test_successful_build_clears_pressure(self):
        h=Harness();h.loop()
        h.tool('terminal',{'command':'python -m build'},{'exit_code':0,'output':'Build completed'})
        self.assertFalse(h.guard.active)
        self.assertEqual(h.guard.no_progress_turns,0)

    def test_new_failed_test_diagnostics_permit_reopening(self):
        h=Harness();h.loop();self.assertTrue(h.guard.locks)
        h.tool('terminal',{'command':'pytest test_privacy.py'},{'exit_code':1,'output':'FAILED privacy: label exposed to second client'})
        self.assertFalse(h.guard.active);self.assertFalse(h.guard.locks)
        self.assertFalse(h.guard.planning_complete)
        h.append('assistant','Planning alternative implementation based on the new privacy failure.')
        self.assertFalse(h.guard.active)
        self.assertTrue(any(e.get('reason_code')=='new_failure_evidence' for e in h.events))

    def test_same_failure_with_new_duration_is_not_new_evidence(self):
        h=Harness()
        args={'command':'pytest test_privacy.py'}
        h.tool('terminal',args,{'exit_code':1,'output':'FAILED privacy in 0.1s'})
        h.append('assistant',FIXTURE['plans'][0]);before=h.guard.no_progress_turns
        h.tool('terminal',args,{'exit_code':1,'output':'FAILED privacy in 0.2s'})
        self.assertEqual(h.guard.no_progress_turns,before)
        h.tool('terminal',args,{'exit_code':1,'output':'FAILED new owner constraint in 0.2s'})
        self.assertEqual(h.guard.no_progress_turns,0)

    def test_user_changes_requirement_reopens_without_stale_anchor(self):
        h=Harness();h.loop()
        h.append('user','Change requirement: implement a public label opt-in instead.')
        self.assertFalse(h.guard.active);self.assertFalse(h.guard.locks)
        self.assertIsNone(h.guard.next_action)
        self.assertFalse(h.guard.planning_complete)

    def test_new_current_source_evidence_reopens_decisions(self):
        h=Harness();h.loop()
        h.tool('read_file',{'path':'api.py'},{'content':'Synchronous requests unavailable; only asynchronous requests are supported','total_lines':1})
        self.assertFalse(h.guard.active);self.assertFalse(h.guard.locks)

    def test_repeated_unchanged_reads_and_ls_do_not_count_as_progress(self):
        h=Harness();args={'path':'api.py'};result={'content':'stable API','total_lines':1}
        h.tool('read_file',args,result)
        h.append('assistant',FIXTURE['plans'][0]);count=h.guard.no_progress_turns
        h.tool('read_file',args,result)
        h.tool('terminal',{'command':'ls'},{'exit_code':0,'output':'api.py'})
        h.tool('read_file',args,{'dedup':True,'content_returned':False,'status':'unchanged'})
        self.assertEqual(h.guard.no_progress_turns,count)

    def test_fresh_source_investigation_is_allowed_before_first_edit(self):
        h=Harness()
        for i in range(12):
            h.append('assistant',FIXTURE['plans'][i%6])
            h.tool('read_file',{'path':'module'+str(i)+'.lua'},{'content':'source '+str(i),'total_lines':1})
        self.assertFalse(h.guard.active)
        self.assertFalse(any(e['event']=='loop_guard_triggered' for e in h.events))

    def test_analysis_design_review_and_diagnosis_do_not_require_writes(self):
        for request in ['Please review the architecture','Design a privacy protocol','Diagnose the API constraints','Plan the implementation carefully','Explain the code']:
            with self.subTest(request=request):
                h=Harness(task_mode='implementation');h.append('user',request);h.loop(12)
                self.assertFalse(h.guard.active)

    def test_incidental_design_word_does_not_override_implementation_intent(self):
        h=Harness();h.append('user','Implement the agreed design for the feature.');self.assertIsNotNone(h.loop())

    def test_one_phrase_or_lexical_keyword_alone_cannot_trigger(self):
        h=Harness();h.append('assistant',FIXTURE['settled'])
        for text in ['think','plan','architecture','reconsider','wait','design']*3:h.append('assistant',text)
        self.assertFalse(h.guard.active)

    def test_genuinely_distinct_planning_does_not_trigger(self):
        h=Harness();h.append('assistant',FIXTURE['settled'])
        for i in range(8):h.append('assistant','Plan phase '+str(i)+': '+('coordinate'+str(i)+' ')*8)
        self.assertFalse(h.guard.active)

    def test_hidden_reasoning_is_neither_observed_nor_logged(self):
        h=Harness()
        for i in range(10):h.append('assistant','',reasoning_content=PRIVATE+FIXTURE['plans'][0])
        self.assertEqual(h.guard.turn,0);self.assertFalse(h.guard.active)
        h.loop();self.assertNotIn(PRIVATE,dumps(h.events))
        self.assertEqual(visible_text({'content':'Visible<think>'+PRIVATE+'</think> next'}),'Visible next')
        self.assertEqual(visible_text({'content':'Visible<THINK>'+PRIVATE}),'Visible')

    def test_system_control_is_token_bounded_and_retry_stable(self):
        h=Harness();control=h.loop(4);self.assertLessEqual(h.counter.text(control['content']),250)
        hashes=[digest(wire_message(m)) for m in h.history]
        before=copy.deepcopy(h.events);turn=h.guard.turn
        h.guard.observe(h.session,h.history,hashes);again=h.guard.render(h.counter);h.guard.delivered([again])
        self.assertEqual(control,again);self.assertEqual(h.guard.turn,turn)
        self.assertEqual(h.events,before)

    def test_cooldown_avoids_injection_on_every_request(self):
        h=Harness();h.loop()
        interventions=[e for e in h.events if e['event']=='loop_guard_intervention'];self.assertEqual(len(interventions),1)
        for i in range(3):self.assertIsNone(h.append('assistant',FIXTURE['plans'][i]))
        self.assertEqual(len([e for e in h.events if e['event']=='loop_guard_intervention']),1)
        h.append('assistant',FIXTURE['plans'][0]);h.append('assistant',FIXTURE['plans'][1]);h.append('assistant',FIXTURE['plans'][2])
        self.assertEqual(len([e for e in h.events if e['event']=='loop_guard_intervention']),2)

    def test_session_change_and_history_edit_reset_temporary_state(self):
        h=Harness();h.loop();h.session='other';h.history=[]
        h.append('user','Build a different app.');self.assertFalse(h.guard.active);self.assertFalse(h.guard.locks)
        h.loop();h.history[0]['content']='Review the implementation only.'
        hashes=[digest(wire_message(m)) for m in h.history];h.guard.observe(h.session,h.history,hashes)
        self.assertFalse(h.guard.active);self.assertEqual(h.guard.turn,0)

    def test_blocked_execution_anchor_suppresses_intervention(self):
        h=Harness();h.append('assistant',FIXTURE['settled']);h.guard.next_action['blocked_by']=['Need user API credentials']
        for p in FIXTURE['plans']:h.append('assistant',p)
        self.assertFalse(h.guard.active)

    def test_archive_retrieval_is_not_new_corroborating_evidence(self):
        h=Harness();h.loop();count=h.guard.no_progress_turns
        for name in ['rolling_history_read','rolling_history_search','rolling_snapshot_read','rolling_raw_read']:
            h.tool(name,{'source_id':'x'},{'content':FIXTURE['settled']})
        self.assertEqual(h.guard.no_progress_turns,count);self.assertTrue(h.guard.active)

    def test_failed_noop_and_pending_tools_do_not_claim_progress(self):
        h=Harness();h.append('assistant',FIXTURE['plans'][0]);count=h.guard.no_progress_turns
        h.tool('patch',{'path':'a.py'},{'success':True,'no_change':True,'files_modified':['a.py']})
        h.tool('terminal',{'command':'pytest'},{'exit_code':None,'status':'yielded_to_background','output':''})
        self.assertEqual(h.guard.no_progress_turns,count)

    def test_new_terminal_source_reads_support_legitimate_investigation(self):
        h=Harness()
        for i in range(8):
            h.append('assistant',FIXTURE['plans'][i%6])
            h.tool('terminal',{'command':'cat module'+str(i)+'.lua'}, {'exit_code':0,'output':'code '+str(i)})
        self.assertFalse(h.guard.active)

    def test_unknown_completed_shell_action_defers_without_claiming_progress(self):
        h=Harness();h.loop()
        h.tool('terminal',{'command':'python generate_project.py'}, {'exit_code':0,'output':''})
        self.assertFalse(h.guard.active);self.assertEqual(h.guard.progress_events,0)

    def test_new_implementation_error_reopens_but_repeated_error_is_not_new(self):
        h=Harness();h.loop()
        args={'path':'a.py','content':'x'};result={'error':'Filesystem is read-only'}
        h.tool('write_file',args,result)
        self.assertFalse(h.guard.active);self.assertFalse(h.guard.locks)
        h.append('assistant',FIXTURE['plans'][0]);count=h.guard.no_progress_turns
        h.tool('write_file',args,result)
        self.assertEqual(h.guard.no_progress_turns,count)

    def test_same_successful_test_verifies_new_code_after_an_edit(self):
        h=Harness();args={'command':'pytest'};result={'exit_code':0,'output':'3 passed in 0.1s'}
        h.tool('terminal',args,result)
        h.tool('write_file',{'path':'a.py','content':'x=2'},{'bytes_written':3,'files_modified':['a.py']})
        h.append('assistant',FIXTURE['plans'][0])
        h.tool('terminal',args,result)
        self.assertEqual(h.guard.no_progress_turns,0)
        self.assertEqual(h.guard.progress_events,1)

    def test_repeated_process_result_duration_does_not_create_new_progress(self):
        h=Harness();args={'action':'poll','session_id':'one'}
        h.tool('process',args,{'exit_code':0,'output':'Tests passed in 0.1s'})
        h.append('assistant',FIXTURE['plans'][0]);count=h.guard.no_progress_turns
        h.tool('process',args,{'exit_code':0,'output':'Tests passed in 0.2s'})
        self.assertEqual(h.guard.no_progress_turns,count)

    def test_can_disable_and_defaults_remain_conservative(self):
        h=Harness(enabled=False);self.assertIsNone(h.loop());self.assertEqual(h.events,[])
        self.assertFalse(DEFAULTS['enabled']);self.assertFalse(VALIDATED_POLICY['loop_guard']['enabled'])
        for settings in [{'no_progress_turns':1},{'cooldown_turns':0},{'max_control_tokens':1000},{'enabled':'yes'}]:
            with self.assertRaises(ValueError):ReasoningLoopGuard(settings,lambda *a,**k:None)

class EngineIntegrationTests(unittest.TestCase):
    def make(self,root,policy='coherent'):
        e=RollingContextEngine(settings={'state_dir':str(root),'selection_policy':policy,'loop_guard':{'enabled':True}},counter=Counter())
        e.on_session_start('engine-fixture');e._stop.set();return e
    def feed(self,e):
        history=[{'role':'user','content':FIXTURE['user']}]
        e.select_context(history,conversation_messages=history)
        for text in [FIXTURE['settled'],*FIXTURE['plans']]:
            history.append({'role':'assistant','content':text})
            selected=e.select_context(history,conversation_messages=history)
            if any('EXECUTION CONTROL' in m.get('content','') for m in selected):return history,selected
        self.fail('No system execution control reached selected prompt')
    def test_injection_is_counted_selected_and_not_added_to_raw(self):
        with tempfile.TemporaryDirectory() as root:
            e=self.make(root);history,selected=self.feed(e)
            self.assertTrue(any(m['role']=='system' and 'EXECUTION CONTROL' in m['content'] for m in selected))
            self.assertLessEqual(e.counter.messages(selected)+e._reserve(),e.context_length-8192)
            with e.store.connect() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM records WHERE body LIKE '%EXECUTION CONTROL%'").fetchone()[0],0)
                row=db.execute('SELECT request_hash FROM context_generations ORDER BY id DESC LIMIT 1').fetchone()
            from rolling_context.common import wire_hash
            self.assertEqual(row[0],wire_hash(selected))
            for m in history:self.assertEqual(e.store.record(e.session_id,digest(wire_message(m))),wire_message(m))
            frozen=copy.deepcopy(selected)
            history.extend([{'role':'assistant','content':'','tool_calls':[{'id':'edit','function':{'name':'write_file','arguments':dumps({'path':'a.py','content':'x=1'})}}]},
                            {'role':'tool','tool_call_id':'edit','content':dumps({'bytes_written':3,'files_modified':['a.py']})}])
            after=e.select_context(history,conversation_messages=history)
            self.assertFalse(any('EXECUTION CONTROL' in m.get('content','') for m in after));self.assertEqual(selected,frozen)
    def test_all_selection_policies_use_counted_system_control(self):
        for policy in ['circulator','stable','coherent']:
            with self.subTest(policy=policy),tempfile.TemporaryDirectory() as root:
                _,selected=self.feed(self.make(root,policy))
                self.assertTrue(any(m['role']=='system' and 'EXECUTION CONTROL' in m.get('content','') for m in selected))
    def test_disable_after_trigger_clears_control(self):
        with tempfile.TemporaryDirectory() as root:
            e=self.make(root);history,_=self.feed(e);e.settings['loop_guard']['enabled']=False
            selected=e.select_context(history,conversation_messages=history)
            self.assertIsNone(e._loop_guard);self.assertFalse(any('EXECUTION CONTROL' in m.get('content','') for m in selected))
    def test_reset_and_restart_do_not_restore_pressure_counters(self):
        with tempfile.TemporaryDirectory() as root:
            e=self.make(root);history,_=self.feed(e)
            restored=self.make(root);selected=restored.select_context(history,conversation_messages=history)
            self.assertFalse(restored._loop_guard.active);self.assertEqual(restored._loop_guard.turn,0)
            self.assertFalse(any('EXECUTION CONTROL' in m.get('content','') for m in selected))
            e.on_session_reset();self.assertIsNone(e._loop_guard)
    def test_guard_failure_preserves_selector_and_physical_constants(self):
        with tempfile.TemporaryDirectory() as root:
            e=self.make(root);e.settings['loop_guard']['no_progress_turns']=1
            selected=e.select_context([{'role':'user','content':'Build a feature.'}])
            self.assertTrue(selected);self.assertEqual(e.context_length,73728)
            self.assertEqual((e.settings['generation_reserve_tokens'],e.settings['target_tokens'],e.settings['trigger_tokens']),(8192,32000,36000))
            self.assertEqual(e.settings['summary_memory_max_tokens'],4096)
