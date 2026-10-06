"""Focused regressions for the production overhead/rebuild/dedup failure."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path.home()/'.hermes/hermes-agent'))
from rolling_context.engine import RollingContextEngine, SUMMARY_SECTIONS
from rolling_context.common import digest, dumps, wire_message

class Counter:
    def text(self,text):return len(text)
    def weights(self,messages):return [len(m.get('content') or '')+len(dumps(m.get('tool_calls') or [])) for m in messages]
    def messages(self,messages):return sum(self.weights(messages))

def coding_history():
    messages=[{'role':'system','content':'Preserve project requirements. '*18},
              {'role':'user','content':'Objective: Repair fixture/processor.py; preserve ERROR_731 and run the verification.'}]
    for i in range(92):
        messages += cycle(i,5600)
    messages += cycle('active-a',12000)+cycle('active-b',5600)
    return messages

def cycle(index,size):
    return [{'role':'assistant','content':'Phase: inspect and verify','tool_calls':[{'id':str(index),'type':'function','function':{'name':'terminal','arguments':dumps({'command':f'check fixture {index}'})}}]},
            {'role':'tool','tool_call_id':str(index),'content':f'Fixture output {index}\n'+('v'*size)},
            {'role':'assistant','content':'Finding: Existing test failure is ERROR_731. Next action: patch fixture/processor.py.'}]

class ContinuityTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.engine=RollingContextEngine(settings={'state_dir':str(self.root)},counter=Counter())
        self.engine.on_session_start('long-coding');self.engine._stop.set()
        (self.root/'large-overhead.json').write_text(dumps({'overhead_tokens':10845}))
    def tearDown(self):self.temp.cleanup()
    def select(self,messages):
        return self.engine.select_context(messages,conversation_messages=messages,incoming_message=next((m for m in reversed(messages) if m['role']=='user'),None))
    def event(self):
        return next(x for x in map(json.loads,reversed((self.root/'telemetry.jsonl').read_text().splitlines())) if x['event']=='selection')
    def record(self,name,record):
        target=os.environ.get('RC_CONTINUITY_EVIDENCE')
        if target:
            p=Path(target);data=json.loads(p.read_text()) if p.exists() else {};data[name]=record;p.write_text(json.dumps(data,indent=2)+'\n')
    def warm(self,messages):
        h=digest(wire_message(messages[2]));obj={s:[] for s in SUMMARY_SECTIONS}
        obj['task_state']=[{'text':'Repair fixture/processor.py ERROR_731 '+('x'*1100),'sources':[h]} for i in range(3)]
        text=dumps(obj)
        with self.engine.store.connect() as db:
            db.execute("INSERT INTO summaries(session,created,covered,text,tokens,job_id,coverage_kind) VALUES (?,?,?,?,?,?,?)",('long-coding',0,dumps([h]),text,len(text),0,'source_set'))
    def test_overhead_is_separate_from_content_trigger(self):
        messages=coding_history();self.select(messages);first=self.event()
        # Grow past the old 36K transport threshold, but below the 36K content threshold.
        messages += cycle('append',4200);self.select(messages);second=self.event()
        self.record('overhead',{'before':first,'after':second})
        self.assertFalse(second['prefix_rebuilt'])
        self.assertGreater(second['active_tokens'],36000)
        self.assertLess(second['content_tokens'],36000)
    def test_rebuild_preserves_recent_trail_ahead_of_warm_blocks(self):
        messages=coding_history();self.select(messages);first=self.event();self.warm(messages)
        # Force a request-boundary epoch to represent warm availability/model overhead change.
        self.engine._stable_frame['target']=0
        original=copy.deepcopy(messages);selected=self.select(messages);second=self.event()
        self.record('rebuild',{'before':first,'after':second})
        self.assertEqual(messages,original)
        self.assertGreater(first['raw_history_tokens'],500000)
        self.assertGreaterEqual(second['tail_tokens'],16000)
        self.assertGreaterEqual(second['source_token_retained_fraction'],.85)
        self.assertGreaterEqual(second['sources_jaccard'],.7)
        self.assertLess(self.engine.counter.messages(selected)+10845,64512)
    def test_evicted_dedup_read_rehydrates_exact_raw(self):
        result=dumps({'content':'1|EXACT_FILE_VALUE_731 = 419','truncated':False})
        path='fixture/processor.py'
        call=lambda cid:{'role':'assistant','tool_calls':[{'id':cid,'type':'function','function':{'name':'read_file','arguments':dumps({'path':path})}}]}
        messages=coding_history();messages[2:2]=[call('original-read'),{'role':'tool','tool_call_id':'original-read','content':result}]
        self.select(messages)
        messages += [call('reread'),{'role':'tool','tool_call_id':'reread','content':dumps({'status':'unchanged','dedup':True,'content_returned':False,'path':path})}]
        original=copy.deepcopy(messages);selected=self.select(messages)
        recovered=next(m for m in selected if m.get('tool_call_id')=='reread')
        self.record('dedup',{'rehydrated':json.loads(recovered['content'])['content']==json.loads(result)['content'],'selection':self.event()})
        restored=json.loads(recovered['content'])
        self.assertEqual(restored['content'],json.loads(result)['content'])
        self.assertEqual(restored['authority'],'archived_raw')
        self.assertFalse(restored['disk_rechecked'])
        self.assertEqual(restored['archived_source_id'],digest(wire_message(messages[3])))
        self.assertEqual(messages,original)
        self.assertEqual(self.engine.store.record('long-coding',digest(wire_message(messages[-1]))),wire_message(messages[-1]))

    def test_content_burst_keeps_trail_and_does_not_rebuild_every_turn(self):
        messages=coding_history();self.select(messages)
        messages += cycle('one',4200);self.select(messages);before=self.event()
        messages += cycle('two',2200);selected=self.select(messages);burst=self.event()
        frozen=copy.deepcopy(selected)
        self.assertTrue(burst['prefix_rebuilt']);self.assertTrue(burst['content_burst'])
        self.assertGreater(burst['content_tokens'],36000)
        self.assertGreaterEqual(burst['continuity_raw_retained_fraction'],.85)
        self.assertLess(burst['estimated_transport_prompt_tokens'],64512)
        messages += cycle('three',200);self.select(messages)
        self.assertFalse(self.event()['prefix_rebuilt']);self.assertEqual(selected,frozen)
        self.record('burst',{'before':before,'after':burst,'next':self.event()})
    def test_physical_pressure_is_separate_and_admission_stays_bounded(self):
        messages=coding_history();messages[0]['content']='system overhead '*2400
        selected=self.select(messages);ev=self.event()
        self.assertTrue(ev['content_capacity_clamped'])
        self.assertTrue(ev['physical_continuity_pressure'])
        self.assertLessEqual(self.engine.counter.messages(selected)+10845,64512)
        self.assertFalse(ev['physical_budget_exceeded'])
    def test_explicit_task_state_survives_restart_without_hidden_reasoning(self):
        messages=[{'role':'user','content':'Objective: Fix fixture/processor.py'},
                  {'role':'assistant','content':'Phase: verify\nFinding: wrong modulus\nUnresolved: test ERROR_731\nNext action: patch the expression',
                   'reasoning_content':'SECRET_CHAIN_OF_THOUGHT_731'}]
        self.select(messages)
        with self.engine.store.connect() as db:state=json.loads(db.execute('SELECT state FROM active_task_state').fetchone()[0])
        self.assertNotIn('SECRET_CHAIN',dumps(state))
        for field in ['objective','phase','findings','unresolved','next_action']:self.assertTrue(state[field])
        restarted=RollingContextEngine(settings={'state_dir':str(self.root)},counter=Counter())
        restarted.on_session_start('long-coding');restarted._stop.set()
        selected=restarted.select_context(messages+[{'role':'user','content':'Continue'}])
        self.assertNotIn('SECRET_CHAIN',dumps(selected))
        self.assertIn('Next action',dumps(selected))
    def test_region_mismatch_and_fresh_results_are_not_replaced(self):
        path='fixture/processor.py'
        def call(cid,offset):return {'role':'assistant','tool_calls':[{'id':cid,'type':'function','function':{'name':'read_file','arguments':dumps({'path':path,'offset':offset})}}]}
        old=dumps({'content':'1|old value','truncated':False})
        stub=dumps({'status':'unchanged','dedup':True,'content_returned':False,'path':path})
        messages=[{'role':'user','content':'Objective: inspect processor'},call('old',1),{'role':'tool','tool_call_id':'old','content':old}]
        self.select(messages)
        messages += [call('other',21),{'role':'tool','tool_call_id':'other','content':stub}]
        selected=self.select(messages)
        self.assertEqual(selected[-1]['content'],stub)
        fresh=dumps({'content':'1|NEW_FILE_VALUE_719','truncated':False})
        messages += [call('fresh',1),{'role':'tool','tool_call_id':'fresh','content':fresh}]
        restored=json.loads(self.select(messages)[-1]['content'])
        self.assertEqual(restored['content'],json.loads(fresh)['content'])
        self.assertEqual(restored['authority'],'archived_raw')
        self.assertFalse(restored['disk_rechecked'])
    def test_archive_read_index_survives_restart_and_stays_session_scoped(self):
        call={'role':'assistant','tool_calls':[{'id':'old','type':'function','function':{'name':'read_file','arguments':dumps({'path':'fixture/a.py'})}}]}
        content=dumps({'content':'1|DURABLE_RAW_731','truncated':False})
        self.select([{'role':'user','content':'Objective: inspect a.py'},call,{'role':'tool','tool_call_id':'old','content':content}])
        call=copy.deepcopy(call);call['tool_calls'][0]['id']='new'
        stub={'role':'tool','tool_call_id':'new','content':dumps({'status':'unchanged','dedup':True,'content_returned':False})}
        restarted=RollingContextEngine(settings={'state_dir':str(self.root)},counter=Counter());restarted.on_session_start('long-coding');restarted._stop.set()
        selected=restarted.select_context([{'role':'user','content':'Continue'},call,stub])
        restored=json.loads(selected[-1]['content'])
        self.assertEqual(restored['content'],json.loads(content)['content'])
        self.assertEqual(restored['authority'],'archived_raw')
        self.assertFalse(restored['disk_rechecked'])
        self.assertEqual(restarted.store.record('long-coding',restored['archived_source_id'])['content'],content)
        restarted.on_session_start('another-session')
        self.assertEqual(restarted.select_context([{'role':'user','content':'Continue'},call,stub])[-1]['content'],stub['content'])

    def recover(self, source):
        parts=[];offset=0
        while True:
            page=json.loads(self.engine.handle_tool_call('rolling_history_read',{'source_id':source,'offset':offset,'max_chars':12000}))
            parts.append(page['untrusted_source'])
            if page['next_offset'] is None:return ''.join(parts)
            offset=page['next_offset']

    def test_huge_current_objective_is_bounded_but_operationally_complete(self):
        messages=coding_history()
        objective=('Objective: Repair fixture/processor.py while preserving the public API.\n'
                   'Hard requirement: preserve ERROR_731.\n'+('Historical design detail without an active action.\n'*600)+
                   'Invariant: keep output reserve 8192.\n'+('Optional example data for reference.\n'*600)+
                   'Do NOT change the physical server.\nImportant files: fixture/processor.py\n'
                   'Phase: verification\nCompleted: baseline reproduced\nUnresolved: wrong modulus\n'
                   'Next action: patch the expression and run verification.')
        self.assertGreater(len(objective),40000)
        messages[1]['content']=objective
        self.warm(messages)
        selected=self.select(messages);ev=self.event()
        self.record('huge_objective',ev)
        self.assertLessEqual(self.engine.counter.messages(selected)+10845,64512)
        self.assertNotIn(objective,[m.get('content') for m in selected])
        self.assertGreaterEqual(ev['tail_tokens'],16000)
        rendered=dumps(selected)
        for value in ['ERROR_731','keep output reserve 8192','Do NOT change the physical server','baseline reproduced','wrong modulus','patch the expression','fixture/processor.py']:
            self.assertIn(value,rendered)
        self.assertEqual(self.recover(digest(wire_message(messages[1]))),dumps(wire_message(messages[1])))
        self.assertFalse(ev['physical_budget_exceeded'])

    def test_three_large_dedup_recoveries_cannot_overrun_admission(self):
        messages=coding_history();originals=[];calls=[]
        for i,size in enumerate([48000,20000,15000]):
            call={'role':'assistant','tool_calls':[{'id':f'old-{i}','type':'function','function':{'name':'read_file','arguments':dumps({'path':f'fixture/part-{i}.py'})}}]}
            result={'role':'tool','tool_call_id':f'old-{i}','content':dumps({'content':'EXACT_NEEDED_VALUE_731\n'+('source line\n'*(size//12)),'truncated':False})}
            originals.append(result);calls.append(call)
        messages[2:2]=[m for pair in zip(calls,originals) for m in pair]
        self.select(messages);before=self.event()
        for i,call in enumerate(calls):
            call=copy.deepcopy(call);call['tool_calls'][0]['id']=f'new-{i}'
            messages += [call,{'role':'tool','tool_call_id':f'new-{i}','content':dumps({'status':'unchanged','dedup':True,'content_returned':False})}]
        messages.append({'role':'assistant','content':'Next action: patch EXACT_NEEDED_VALUE_731 in the relevant source.'})
        selected=self.select(messages);ev=self.event();self.record('large_recoveries',{'before':before,'after':ev})
        self.assertLessEqual(self.engine.counter.messages(selected)+10845,64512)
        self.assertGreaterEqual(ev['continuity_raw_retained_fraction'],.85)
        self.assertGreaterEqual(ev['tail_tokens'],16000)
        for original in originals:
            self.assertEqual(self.recover(digest(wire_message(original))),dumps(wire_message(original)))
        for i in range(3):
            value=next(m['content'] for m in selected if m.get('tool_call_id')==f'new-{i}')
            self.assertIn('EXACT_NEEDED_VALUE_731',value)
            self.assertNotIn('"status":"unchanged"',value)
        self.assertFalse(ev['physical_budget_exceeded'])

    def test_oversized_atomic_tool_framing_uses_durable_manifest(self):
        messages=[{'role':'user','content':'Objective: verify the current fixture'}]
        calls=[{'id':'frame-'+str(i)+'x'*180,'type':'function','function':{'name':'read_file','arguments':dumps({'path':f'fixture/{i}'})}} for i in range(230)]
        messages.append({'role':'assistant','tool_calls':calls})
        messages += [{'role':'tool','tool_call_id':c['id'],'content':'ok'} for c in calls]
        selected=self.select(messages);ev=self.event()
        self.assertLessEqual(self.engine.counter.messages(selected)+10845,64512)
        self.assertIsNotNone(ev['admission_source_manifest'])
        manifest=json.loads(self.recover(ev['admission_source_manifest']))
        ids=json.loads(manifest['content'])['source_ids']
        self.assertIn(digest(wire_message(messages[-1])),ids)
        self.assertIn('rolling_history_read',dumps(selected))
        self.assertIn('verify the current fixture',dumps(selected))

if __name__=='__main__':unittest.main()
