"""Four targeted regressions, entirely in temporary state with mocked inference."""
import copy
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from test_engine import EngineTests
from test_loop_guard import Harness, Counter as TokenCounter
from rolling_context.common import StateStore, digest, dumps, wire_message
from rolling_context.engine import RollingContextEngine
from rolling_context.summary import SUMMARY_SECTIONS
from rolling_context.continuity import TaskState
from rolling_context.relay import emit as relay_emit

PRIVATE = 'PRIVATE_REASONING_MUST_NOT_BE_PERSISTED_421'


class ReadyLifecycleTests(EngineTests):
    def setUp(self):
        super().setUp()
        self.engine.settings.update(selection_policy='coherent', target_tokens=8000,
            trigger_tokens=9000, tail_tokens=900, minimum_tail_tokens=300,
            warm_budget_tokens=1800, recall_budget_tokens=4000, segment_max_tokens=700,
            semantic_policy='cold_chunks', compactor_mode='librarian')
        self.engine._stop.set()

    def history(self):
        return [{'role':'user','content':'Build fixture/a.py and preserve the interface.'},
                {'role':'assistant','content':'UNIQUE_REQUIRED_421 '+('old implementation evidence '*100)}] + [
            {'role':'assistant','content':str(i)+' '+('unrelated filler '*30)} for i in range(30)] + [
            {'role':'user','content':'Continue UNIQUE_REQUIRED_421'}]

    def ready(self, messages, positions, job_id=88, block=None):
        chunk = [wire_message(messages[i]) for i in positions]
        sources = [digest(m) for m in chunk]
        block = block or {s:[] for s in SUMMARY_SECTIONS}
        if not any(block.values()):
            block['results']=[{'text':'UNIQUE_REQUIRED_421: confirmed implementation finding.',
                              'sources':sources,'status':'DERIVED'}]
        job={'coverage':dumps(sources),'coverage_kind':'source_set','parent_id':None,
             'trigger_tokens':8000,'chunk_tokens':sum(self.engine.counter.weights(chunk))}
        with self.engine.store.connect() as db:
            db.execute('INSERT INTO jobs(id,session,created,updated,status,owner,coverage,parent_id,chunk,trigger_tokens,chunk_tokens,coverage_kind) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (job_id,self.engine.session_id,0,0,'running',os.getpid(),job['coverage'],None,
                 dumps(chunk),8000,job['chunk_tokens'],'source_set'))
        def response(url,payload,timeout,**kw):
            self.assertNotIn(PRIVATE,dumps(payload))
            return {'choices':[{'finish_reason':'stop','message':{'content':dumps(block),
                    'reasoning_content':PRIVATE,'reasoning':PRIVATE}}],
                    'usage':{'prompt_tokens':10,'completion_tokens':20,'reasoning':PRIVATE}}
        with patch('rolling_context.engine.stream_chat',side_effect=response):
            self.engine._run_compaction(self.engine.session_id,job,job_id,chunk,set(sources),{},set(sources),{},time.monotonic())
        with self.engine.store.connect() as db:
            return dict(db.execute('SELECT * FROM summaries WHERE job_id=?',(job_id,)).fetchone())

    def event(self):
        return next(json.loads(line) for line in reversed((self.root/'telemetry.jsonl').read_text().splitlines())
                    if json.loads(line)['event']=='selection')

    def test_ready_replaces_selected_raw_recall_and_receipt_is_not_duplicated(self):
        messages=self.history();old=digest(wire_message(messages[1]))
        raw={'page_id':old,'source_id':old,'source_ids':[old], 'representation':'RAW',
             'version':0,'content':messages[1]['content'],'tokens':len(messages[1]['content'])}
        def recall(session,query,body,hashes,weights,allowed,budget,*args,**kw):
            return [raw] if old in allowed else []
        with patch.object(self.engine.pages,'recall_segments',side_effect=recall):
            first=self.select(messages);frozen=copy.deepcopy(first)
            before=self.engine.counter.messages(first)
            row=self.ready(messages,[1])
            second=self.select(messages);event=self.event()
            self.assertIn('IMMUTABLE BLOCK '+str(row['id']),dumps(second))
            self.assertLess(self.engine.counter.messages(second),before)
            self.proof={'selected_context_before':before,'selected_context_after':self.engine.counter.messages(second),
                        'summary_tokens':row['tokens'],'summary_id':row['id'],'first_generation':event['generation'],
                        'selected_warm_blocks':event['warm_blocks'],'counter_unit':'fixture character counter'}
            self.assertTrue(event['applied']);self.assertTrue(event['ready_compaction_refresh'])
            self.assertEqual(event['warm_blocks'],[row['id']])
            self.assertEqual(event['ready_compaction_admission'][0]['state'],'admitted')
            self.assertEqual(first,frozen)
            third=self.select(messages);self.assertEqual(second,third)
            self.assertFalse(self.event()['applied']);self.assertTrue(self.event()['compact_blocks_active'])
        with self.engine.store.connect() as db:
            receipt=db.execute('SELECT admitted_generation FROM summaries WHERE id=?',(row['id'],)).fetchone()[0]
            self.assertIsNotNone(receipt)
        applied=[json.loads(l) for l in (self.root/'telemetry.jsonl').read_text().splitlines() if json.loads(l)['event']=='summary_applied']
        self.assertEqual(len(applied),1)
        self.proof.update(admission_events=len(applied),raw_unchanged=True)
        self.assertEqual(self.engine.store.record(self.engine.session_id,old),wire_message(messages[1]))
        self.assertNotIn(PRIVATE,(self.root/'telemetry.jsonl').read_text())
        restarted=RollingContextEngine(settings=self.engine.settings,counter=self.engine.counter)
        restarted.on_session_start(self.engine.session_id);restarted._stop.set()
        restarted.select_context(messages,conversation_messages=messages)
        applied=[json.loads(l) for l in (self.root/'telemetry.jsonl').read_text().splitlines() if json.loads(l)['event']=='summary_applied']
        self.assertEqual(len(applied),1)

    def test_two_ready_results_have_explicit_lifecycle_and_real_provenance(self):
        messages=self.history();self.select(messages)
        rows=[self.ready(messages,[1],88),self.ready(messages,[2],89)]
        selected=self.select(messages);event=self.event()
        self.assertEqual({r['job_id'] for r in event['ready_compaction_admission']},{88,89})
        self.assertTrue(all(r['state']=='admitted' for r in event['ready_compaction_admission']))
        self.assertEqual(len(event['warm_blocks']),len(set(event['warm_blocks'])))
        for row in rows:
            self.assertEqual(dumps(selected).count('IMMUTABLE BLOCK '+str(row['id'])+':'),1)
            self.assertTrue(any(r.get('version')==row['id'] and r['representation']=='COMPACT' for r in event['page_ids']))
        self.assertEqual(event['compaction_pending_ready_jobs'],0)
        events=(self.root/'telemetry.jsonl').read_text()
        self.assertFalse(any(json.loads(line)['event']=='librarian_fallback' for line in events.splitlines()))

    def test_protected_recent_result_is_explicitly_deferred(self):
        messages=self.history();self.select(messages);self.ready(messages,[len(messages)-2])
        selected=self.select(messages)
        self.assertEqual(self.event()['ready_compaction_admission'][0]['reason'],'protected_recent_sources')
        self.assertIn(messages[-2]['content'],dumps(selected))

    def test_changed_source_and_insufficient_warm_budget_defer_safely(self):
        messages=self.history();self.select(messages);row=self.ready(messages,[1])
        self.engine.settings['warm_budget_tokens']=1
        self.select(messages)
        self.assertEqual(self.event()['ready_compaction_admission'][0]['reason'],'warm_budget')
        edited=copy.deepcopy(messages);edited[1]['content']='Changed authoritative evidence'
        self.select(edited)
        self.assertEqual(self.event()['ready_compaction_admission'][0]['reason'],'source_history_changed')
        with self.engine.store.connect() as db:
            self.assertIsNone(db.execute('SELECT admitted_generation FROM summaries WHERE id=?',(row['id'],)).fetchone()[0])


    def test_pending_ready_is_admitted_even_beyond_normal_block_lookup(self):
        messages=self.history();self.select(messages);row=self.ready(messages,[1])
        with self.engine.store.connect() as db:
            for i in range(65):
                db.execute('INSERT INTO summaries(session,created,covered,text,tokens,job_id,coverage_kind) VALUES (?,?,?,?,?,?,?)',
                    (self.engine.session_id,0,row['covered'],row['text'],row['tokens'],1000+i,'source_set'))
        self.select(messages)
        self.assertIn(row['id'],self.event()['warm_blocks'])
        self.assertEqual(self.event()['ready_compaction_admission'][0]['state'],'admitted')

    def test_mismatched_ready_job_coverage_is_explicitly_deferred(self):
        messages=self.history();self.select(messages);row=self.ready(messages,[1])
        with self.engine.store.connect() as db:
            db.execute('UPDATE jobs SET coverage=? WHERE id=88',(dumps([digest(wire_message(messages[2]))]),))
        self.select(messages)
        self.assertEqual(self.event()['ready_compaction_admission'][0]['reason'],'invalid_coverage')
        self.assertNotIn(row['id'],self.event()['warm_blocks'])


class PrivacyBoundaryTests(ReadyLifecycleTests):
    def test_every_archive_kind_removes_private_channels_but_keeps_visible_evidence(self):
        message={'role':'assistant','content':'Finding: visible conclusion\n<THINK>'+PRIVATE+'</THINK>Next action: run a test',
                 'reasoning':PRIVATE,'reasoning_content':PRIVATE,
                 'metadata':{'reasoning_details':PRIVATE},
                 'codex_reasoning_items':[{'type':'reasoning','text':PRIVATE}]}
        original=copy.deepcopy(message)
        for kind in ('canonical','request','wire','final','admission_input'):
            self.engine.store.snapshot('privacy',kind,[message])
        with self.engine.store.connect() as db:
            records=[r[0] for r in db.execute('SELECT body FROM records')]
        self.assertNotIn(PRIVATE,dumps(records))
        for value in records:
            self.assertNotIn('reasoning_content',value);self.assertNotIn('reasoning_details',value)
        self.assertIn('visible conclusion',dumps(records));self.assertEqual(message,original)
        user={'role':'user','content':'Keep literal <think> tags in the source code.'}
        self.engine.store.snapshot('privacy','user',[user])
        self.assertEqual(self.engine.store.record('privacy',digest(user)),user)

    def test_existing_historical_record_is_not_deleted_or_rewritten(self):
        old={'role':'assistant','content':'Visible','reasoning':PRIVATE,'reasoning_content':PRIVATE}
        old_text=dumps(old);old_hash=digest(old)
        with self.engine.store.connect() as db:
            db.execute('INSERT INTO records VALUES (?,?)',(old_hash,old_text))
            db.execute('INSERT INTO snapshots(session,kind,created,hashes,fingerprint) VALUES (?,?,?,?,?)',
                       ('historical','old',0,dumps([old_hash]),digest([old_hash])))
        self.engine.store.snapshot('historical','canonical',[old])
        with self.engine.store.connect() as db:
            self.assertEqual(db.execute('SELECT body FROM records WHERE hash=?',(old_hash,)).fetchone()[0],old_text)
            fresh=db.execute("SELECT hashes FROM snapshots WHERE kind='canonical' AND session='historical'").fetchone()[0]
        clean=self.engine.store.record('historical',json.loads(fresh)[0])
        self.assertNotIn(PRIVATE,dumps(clean));self.assertEqual(self.engine.store.record('historical',old_hash),old)

    def test_legacy_retrieval_cannot_copy_reasoning_into_new_records(self):
        old={'role':'assistant','content':'Visible conclusion<THINK>'+PRIVATE+'</THINK>',
             'reasoning':PRIVATE,'reasoning_content':PRIVATE}
        source=digest(old);old_text=dumps(old)
        with self.engine.store.connect() as db:
            db.execute('INSERT INTO records VALUES (?,?)',(source,old_text))
            db.execute('INSERT INTO snapshots(session,kind,created,hashes,fingerprint) VALUES (?,?,?,?,?)',
                (self.engine.session_id,'historical',0,dumps([source]),digest([source])))
        responses=[self.engine.handle_tool_call('rolling_history_read',{'source_id':source}),
                   self.engine.handle_tool_call('rolling_raw_read',{'raw_id':source})]
        with patch.object(self.engine.pages,'search_ids',return_value=[source]):
            responses.append(self.engine.handle_tool_call('rolling_history_search',{'query':'Visible'}))
        for response in responses:
            self.assertNotIn(PRIVATE,response);self.assertIn('Visible conclusion',response)
        for envelope in ({'untrusted_source':old_text},{'untrusted_history':[{'message':old}]},
                         {'content':'Visible conclusion','reasoning':PRIVATE}):
            self.engine.store.snapshot(self.engine.session_id,'legacy_projection',
                [{'role':'tool','content':dumps(envelope)}])
        with self.engine.store.connect() as db:
            self.assertEqual(db.execute('SELECT body FROM records WHERE hash=?',(source,)).fetchone()[0],old_text)
            self.assertNotIn(PRIVATE,dumps([r[0] for r in db.execute('SELECT body FROM records WHERE hash!=?',(source,))]))

    def test_legacy_job_cannot_create_invented_sanitized_source_provenance(self):
        old={'role':'assistant','content':'Visible','reasoning_content':PRIVATE}
        original_source=digest(old);clean_source=digest(wire_message(old))
        obj={section:[] for section in SUMMARY_SECTIONS}
        obj['results']=[{'text':'Visible','sources':[clean_source]}]
        with self.assertRaises(ValueError) as caught:
            self.engine._persist_summary(self.engine.session_id,
                {'coverage':dumps([original_source]),'parent_id':None},99,{clean_source},
                dumps(obj),obj,{},None,time.monotonic(),{})
        self.assertEqual(caught.exception.code,'provenance_invalid')
        with self.engine.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM summaries').fetchone()[0],0)

    def test_compaction_and_task_state_store_conclusions_without_private_reasoning(self):
        messages=self.history()
        messages[1]={'role':'assistant','content':'Finding: confirmed bug\nDecision: preserve the interface\nNext action: add a regression\n<THINK>Finding: '+PRIVATE,
                     'reasoning':PRIVATE,'reasoning_content':PRIVATE}
        self.select(messages);row=self.ready(messages,[1])
        self.select(messages)
        with self.engine.store.connect() as db:
            for table,column in [('records','body'),('summaries','text'),('active_task_state','state')]:
                self.assertNotIn(PRIVATE,dumps([r[0] for r in db.execute('SELECT '+column+' FROM '+table)]))
        self.assertNotIn(PRIVATE,(self.root/'telemetry.jsonl').read_text())
        self.proof={'new_raw_compact_task_telemetry_private_occurrences':0,'compaction_source_ids':json.loads(row['text'])['results'][0]['sources']}
        self.assertEqual(json.loads(row['text'])['results'][0]['sources'],[digest(wire_message(messages[1]))])

    def test_both_telemetry_writers_drop_private_channels_and_spans(self):
        fields={'reasoning':PRIVATE,'nested':{'reasoning_content':PRIVATE,'reasoning_tokens':17},
                'message':'visible<THINK>'+PRIVATE+'</THINK> conclusion'}
        self.engine.store.emit('privacy_test',**fields)
        relay_emit(self.root,'large','privacy_test',**fields)
        text=(self.root/'telemetry.jsonl').read_text()
        self.assertNotIn(PRIVATE,text);self.assertIn('reasoning_tokens',text)

    def test_invalid_private_compact_memory_is_rejected_at_the_write_boundary(self):
        source=digest({'role':'assistant','content':'Visible evidence'})
        obj={s:[] for s in SUMMARY_SECTIONS}
        obj['results']=[{'text':'Visible','sources':[source],'reasoning':PRIVATE}]
        with self.assertRaises(ValueError):
            self.engine._persist_summary(self.engine.session_id,{'coverage':dumps([source]),'parent_id':None},99,
                                         {source},dumps(obj),obj,{},None,time.monotonic(),{})
        with self.engine.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM summaries').fetchone()[0],0)


class WorkingStateTests(EngineTests):
    def setUp(self):
        super().setUp();self.engine.settings['selection_policy']='coherent';self.engine._stop.set()

    def test_markdown_conclusions_survive_a_rebuild_and_restart(self):
        messages=[{'role':'user','content':'Build fixture/a.py and keep the public API.'},
            {'role':'assistant','content':'- **Confirmed finding:** API rejects mixed input.\n- **Decision:** validate at the boundary.\n- **Unresolved issue:** error coverage is missing.\n- **Next action:** add a falsifiable regression.\n<THINK>Decision: '+PRIVATE}]
        self.select(messages)
        source=digest(wire_message(messages[1]))
        self.engine._stable_frame=None
        rebuilt=self.select(messages+[{'role':'user','content':'Continue'}])
        rendered=dumps(rebuilt)
        for value in ('API rejects mixed input','validate at the boundary','error coverage is missing','add a falsifiable regression'):
            self.assertIn(value,rendered)
        restarted=RollingContextEngine(settings=self.engine.settings,counter=self.engine.counter)
        restarted.on_session_start(self.engine.session_id);restarted._stop.set()
        output=restarted.select_context([{'role':'user','content':'Continue'}])
        self.assertIn('API rejects mixed input',dumps(output));self.assertIn('add a falsifiable regression',dumps(output))
        with restarted.store.connect() as db:state=json.loads(db.execute('SELECT state FROM active_task_state').fetchone()[0])
        for field in ('findings','decisions','unresolved','next_action'):
            self.assertEqual(state[field][0]['source_id'],source)
        self.assertNotIn(PRIVATE,dumps(state))
        self.proof={'rebuilt_and_restarted_fields':{field:state[field] for field in ('findings','decisions','unresolved','next_action')},'hidden_reasoning_occurrences':0}

    def test_budget_keeps_current_findings_decisions_and_next_action_before_old_lists(self):
        state=TaskState(self.engine)
        source='a'*64
        for field in ('objective','findings','decisions','unresolved','next_action'):
            state.state[field]=[{'text':'current '+field+' '+('detail '*40),'source_id':source}]
        for field in ('files','hard_requirements','completed'):
            state.state[field]=[{'text':'old '+field+' '+('detail '*80),'source_id':source} for _ in range(6)]
        block,sources=state.render();self.assertIsNotNone(block)
        self.assertLessEqual(self.engine.counter.text(block['content']),self.engine.settings['task_state_budget_tokens'])
        for field in ('findings','decisions','unresolved','next_action'):self.assertIn('current '+field,block['content'])
        self.assertEqual(sources,[source])

    def test_valid_librarian_conclusions_hydrate_working_state_without_superseded_claims(self):
        messages=[{'role':'user','content':'Build fixture/a.py.'},{'role':'assistant','content':'Observed the actual API result.'}]
        self.select(messages);source=digest(wire_message(messages[1]))
        obj={s:[] for s in SUMMARY_SECTIONS}
        for section,text in [('results','Confirmed API constraint'),('decisions','Use explicit validation'),
                             ('unresolved','Regression remains missing'),('next_actions','Write the regression')]:
            obj[section]=[{'text':text,'sources':[source],'status':'DERIVED'}]
        obj['decisions'].append({'text':'Superseded architecture','sources':[source],'status':'SUPERSEDED'})
        with self.engine.store.connect() as db:
            db.execute('INSERT INTO summaries(session,created,covered,text,tokens,job_id,coverage_kind) VALUES (?,?,?,?,?,?,?)',
                       (self.engine.session_id,0,dumps([source]),dumps(obj),len(dumps(obj)),0,'source_set'))
        self.engine._stable_frame=None;self.select(messages)
        with self.engine.store.connect() as db:state=json.loads(db.execute('SELECT state FROM active_task_state').fetchone()[0])
        for field in ('findings','decisions','unresolved','next_action'):
            self.assertTrue(state[field]);self.assertEqual(state[field][0]['source_ids'],[source])
            self.assertTrue(state[field][0]['derived'])
        self.assertNotIn('Superseded architecture',dumps(state))


class InspectionTests(unittest.TestCase):
    def repeat(self, h, name='read_file', args=None, result=None, count=6):
        for _ in range(count):
            h.tool(name,args or {'path':'a.py','offset':20,'limit':40},
                   result or {'content':'def target_function(): return 1'})

    def test_unchanged_file_region_triggers_without_planning_language(self):
        h=Harness();self.repeat(h)
        self.assertTrue(h.guard.active)
        self.assertEqual(h.guard.reason,'repeated_inspection_no_new_evidence')
        control=h.guard.render(h.counter)
        self.assertEqual(control['role'],'system');self.assertIn('falsifiable test',control['content'])
        self.assertLessEqual(h.counter.text(control['content']),250)
        self.assertFalse(any('REPEATED INSPECTION CONTROL' in m.get('content','') for m in h.history))
        before=copy.deepcopy(h.events);h.guard.observe(h.session,h.history,[digest(wire_message(m)) for m in h.history])
        h.guard.render(h.counter);h.guard.delivered([control]);self.assertEqual(h.events,before)

    def test_snapshot_search_and_same_function_inspections_are_detected(self):
        for name,args,result in [
            ('rolling_file_snapshot',{'path':'a.py'},{'sha256':'abc','excerpt':{'content':'def target_function(): pass','start_line':20}}),
            ('rolling_snapshot_read',{'snapshot_id':'file:abc','start_line':20,'end_line':40},{'excerpt':{'content':'def target_function(): pass','start_line':20}}),
            ('rolling_history_read',{'source_id':'old'},{'untrusted_source':dumps({'role':'assistant','content':'Visible finding'})}),
            ('rolling_history_search',{'query':'target_function'},{'untrusted_history':[{'hash':'old','message':{'role':'assistant','content':'def target_function(): pass'}}]}),
            ('terminal',{'command':"rg -n -A 12 'target_function' a.py"},{'exit_code':0,'output':'20:def target_function(): pass'})]:
            with self.subTest(tool=name):
                h=Harness();self.repeat(h,name,args,result)
                self.assertEqual(h.guard.reason,'repeated_inspection_no_new_evidence')
                self.assertTrue(h.guard.active)

    def test_changed_regions_changed_content_and_review_are_not_loops(self):
        h=Harness()
        for i in range(8):h.tool('read_file',{'path':'a.py','offset':i*40,'limit':40},{'content':'New function '+str(i)})
        self.assertFalse(h.guard.active)
        h=Harness();self.repeat(h,count=4)
        h.tool('read_file',{'path':'a.py','offset':20,'limit':40},{'content':'Changed authoritative code'})
        self.assertEqual(h.guard.inspections.repeats,0);self.assertFalse(h.guard.active)
        h=Harness();h.append('user','Review the implementation without changing files.');self.repeat(h,count=10)
        self.assertFalse(h.guard.active)

    def test_new_test_error_and_write_reset_and_allow_necessary_rereading(self):
        for name,args,result in [
            ('terminal',{'command':'pytest test_target.py'},{'exit_code':1,'output':'FAILED new boundary diagnostic'}),
            ('terminal',{'command':'python app.py'},{'exit_code':1,'output':'TypeError: new runtime evidence'}),
            ('write_file',{'path':'a.py','content':'fixed'},{'files_modified':['a.py']}),
            ('terminal',{'command':'pytest test_target.py'},{'exit_code':0,'output':'1 passed'})]:
            with self.subTest(progress=name,result=result):
                h=Harness();self.repeat(h);self.assertTrue(h.guard.active)
                h.tool(name,args,result)
                self.assertFalse(h.guard.active);self.assertEqual(h.guard.inspections.repeats,0)
                self.repeat(h,count=4);self.assertFalse(h.guard.active)

    def test_deduplicated_unchanged_reads_count_but_new_failure_evidence_clears(self):
        h=Harness();self.repeat(h,count=1)
        self.repeat(h,result={'status':'unchanged','dedup':True,'content_returned':False},count=5)
        self.assertTrue(h.guard.active)
        h.tool('read_file',{'path':'a.py','offset':20,'limit':40},{'error':'new permission constraint'})
        self.assertFalse(h.guard.active)

    def test_governor_only_session_detects_inspection_without_feedback_and_never_archives_control(self):
        with tempfile.TemporaryDirectory() as root:
            e=RollingContextEngine(settings={'state_dir':root,'loop_guard':{'enabled':False},
                    'generation_governor':{'enabled':True}},counter=TokenCounter())
            e.on_session_start('governor-inspection');e._stop.set()
            body=[{'role':'user','content':'Build the coding project.'}]
            e.select_context(body,conversation_messages=body)
            initial=copy.deepcopy(e.settings)
            for i in range(6):
                body += [{'role':'assistant','content':'','tool_calls':[{'id':str(i),'function':{'name':'read_file',
                         'arguments':dumps({'path':'a.py','offset':20,'limit':40})}}]},
                         {'role':'tool','tool_call_id':str(i),'content':dumps({'content':'def target_function(): pass'})}]
                selected=e.select_context(body,conversation_messages=body)
            controls=[m for m in selected if 'REPEATED INSPECTION CONTROL' in m.get('content','')]
            self.assertEqual(len(controls),1);self.assertEqual(controls[0]['role'],'system')
            self.assertEqual(e.settings,initial)
            with e.store.connect() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM records WHERE body LIKE '%REPEATED INSPECTION CONTROL%'").fetchone()[0],0)
            self.assertFalse(any('REPEATED INSPECTION CONTROL' in m.get('content','') for m in body))
            self.assertEqual(e.context_length,73728)
            self.proof={'completed_repeated_reads':6,'reason':e._loop_guard.reason,'transient_control_messages':len(controls),'control_raw_records':0,'settings_unchanged':True}
            e.on_session_start('unrelated');fresh=e.select_context([{'role':'user','content':'Build another project.'}])
            self.assertFalse(any('REPEATED INSPECTION CONTROL' in m.get('content','') for m in fresh))


def load_tests(loader, tests, pattern):
    suite=unittest.TestSuite()
    for cls in (ReadyLifecycleTests,PrivacyBoundaryTests,WorkingStateTests,InspectionTests):
        suite.addTests(cls(name) for name in cls.__dict__ if name.startswith('test_'))
    return suite
