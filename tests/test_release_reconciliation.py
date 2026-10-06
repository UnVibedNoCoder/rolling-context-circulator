"""Source-linked reconciliation, honest compact claims, and durable RAW."""
import copy
import json
import time
import unittest
from test_engine import EngineTests
from rolling_context.common import digest, dumps, wire_message
from rolling_context.engine import RollingContextEngine
from rolling_context.summary import SUMMARY_SECTIONS
from rolling_context.compaction import CompactionFailure


class ReleaseReconciliationTests(EngineTests):
    def setUp(self):
        super().setUp();self.engine.settings['selection_policy']='coherent';self.engine._stop.set()

    def history(self):
        return [{'role':'user','content':'Build the Arena game and preserve its public interface.'},
                {'role':'assistant','content':'Finding: Arena expects stable identifiers.\nDecision: keep the public interface.\nUnresolved: Arena mapping issue\nNext action: verify Arena mapping'}]

    def tool(self, body, name, args, result, cid='tool'):
        body += [{'role':'assistant','content':'','tool_calls':[{'id':cid,'function':{'name':name,'arguments':dumps(args)}}]},
                 {'role':'tool','tool_call_id':cid,'name':name,'content':dumps(result)}]
        return digest(wire_message(body[-1]))

    def state(self):
        with self.engine.store.connect() as db:
            return json.loads(db.execute('SELECT state FROM active_task_state WHERE session=?',(self.engine.session_id,)).fetchone()[0])

    def active(self, selected):
        marker='[ACTIVE TASK STATE: source-linked conclusions; newer RAW/user evidence takes precedence]\n'
        block=next(m['content'] for m in selected if marker in (m.get('content') or ''))
        return json.loads(block.removeprefix(marker).split('\n[/ACTIVE TASK STATE]')[0])

    def test_unresolved_survives_reads_failed_tests_and_changed_wording(self):
        body=self.history();self.select(body)
        self.tool(body,'read_file',{'path':'Arena.lua'},{'content':'local Arena = {}'})
        self.tool(body,'terminal',{'command':'python -m unittest test_arena'},{'exit_code':1,'output':'FAILED test_mapping'},'failed')
        body.append({'role':'assistant','content':'Finding: Arena mapping needs another investigation.'})
        active=self.active(self.select(body))
        self.assertEqual(active['unresolved'][0]['text'],'Arena mapping issue')
        self.assertEqual(active['next_action'][0]['text'],'verify Arena mapping')

    def test_later_explicit_resolution_and_success_keep_history_but_clear_current(self):
        body=self.history();source=digest(wire_message(body[1]));old=copy.deepcopy(body[1])
        before=self.select(body);frozen=copy.deepcopy(before)
        evidence=self.tool(body,'terminal',{'command':'python -m unittest test_arena'},{'exit_code':0,'output':'Ran 1 test\nOK'},'test')
        body.append({'role':'assistant','content':'Resolved: Arena mapping issue.\nCompleted: verify Arena mapping.'})
        resolution=digest(wire_message(body[-1]));selected=self.select(body);active=self.active(selected)
        self.assertNotIn('unresolved',active);self.assertNotIn('next_action',active)
        state=self.state()
        for field in ('unresolved','next_action'):
            self.assertEqual(state[field][0]['status'],'RESOLVED')
            self.assertEqual(state[field][0]['source_id'],source)
            self.assertEqual(state[field][0]['resolved_by'],resolution)
        self.assertEqual(self.engine.store.record(self.engine.session_id,source),old)
        self.assertEqual(self.engine.store.record(self.engine.session_id,evidence),wire_message(body[-2]))
        self.assertEqual(before,frozen)
        self.assertTrue(any(json.loads(l).get('task_state_reconciled') for l in (self.root/'telemetry.jsonl').read_text().splitlines()))
        self.proof={'old_and_resolution_sources':[source,resolution], 'current_unresolved':0,'current_next_actions':0,'original_raw_preserved':True}

    def test_successful_commit_resolves_commit_required_and_current_commit_action(self):
        body=self.history();body[1]['content'] += '\nUnresolved: commit required\nNext action: commit the game changes'
        self.select(body);original=digest(wire_message(body[1]))
        evidence=self.tool(body,'terminal',{'command':'git commit -m "Finish game"'},{'exit_code':0,'output':'[main cac1287] Finish game\n 2 files changed'},'commit')
        active=self.active(self.select(body));state=self.state()
        self.assertEqual([r['text'] for r in active['unresolved']],['Arena mapping issue'])
        self.assertNotIn('next_action',active)
        commit=next(r for r in state['unresolved'] if r['text']=='commit required')
        self.assertEqual(commit['resolved_by'],evidence);self.assertEqual(commit['source_id'],original)
        self.assertEqual(state['next_action'][0]['status'],'RESOLVED')

    def test_failed_commit_dry_run_and_commit_inspection_do_not_resolve(self):
        for command,result in [('git commit -m finish',{'exit_code':1,'output':'nothing to commit'}),
                               ('git commit --dry-run',{'exit_code':0,'output':'[main cac1287] preview'}),
                               ('git log -1',{'exit_code':0,'output':'[main cac1287] previous commit'})]:
            with self.subTest(command=command):
                body=self.history();body[1]['content']='Unresolved: commit required\nNext action: commit the game changes'
                self.select(body);self.tool(body,'terminal',{'command':command},result,command)
                active=self.active(self.select(body));self.assertTrue(active['unresolved']);self.assertTrue(active['next_action'])

    def test_resolution_survives_restart_and_old_summary_cannot_resurrect_action(self):
        body=self.history();self.select(body);source=digest(wire_message(body[1]))
        obj={s:[] for s in SUMMARY_SECTIONS};obj['unresolved']=[{'text':'Arena mapping issue','sources':[source]}]
        obj['next_actions']=[{'text':'verify Arena mapping','sources':[source]}]
        with self.engine.store.connect() as db:
            db.execute('INSERT INTO summaries(session,created,covered,text,tokens,job_id,coverage_kind) VALUES (?,?,?,?,?,?,?)',
                (self.engine.session_id,0,dumps([source]),dumps(obj),len(dumps(obj)),0,'source_set'))
        body.append({'role':'assistant','content':'Resolved: Arena mapping issue\nCompleted: verify Arena mapping'})
        self.select(body)
        restarted=RollingContextEngine(settings=self.engine.settings,counter=self.engine.counter)
        restarted.on_session_start(self.engine.session_id);restarted._stop.set()
        selected=restarted.select_context(body,conversation_messages=body);active=self.active(selected)
        self.assertNotIn('unresolved',active);self.assertNotIn('next_action',active)
        shortened=restarted.select_context([{'role':'user','content':'Continue'}]);active=self.active(shortened)
        self.assertNotIn('unresolved',active);self.assertNotIn('next_action',active)
        self.assertTrue(active['findings']);self.assertTrue(active['decisions'])

    def test_new_user_evidence_reopens_same_item_without_losing_old_resolution(self):
        body=self.history();self.select(body)
        body += [{'role':'assistant','content':'Resolved: Arena mapping issue'},
                 {'role':'user','content':'Unresolved: Arena mapping issue'}]
        active=self.active(self.select(body));self.assertTrue(active['unresolved'])
        self.assertEqual(active['unresolved'][0]['source_id'],digest(wire_message(body[-1])))
        self.assertTrue(self.state()['resolved'])

    def test_summary_and_state_cannot_upgrade_read_to_verified(self):
        body=self.history();source=self.tool(body,'read_file',{'path':'Arena.lua'},{'content':'test text says OK but this was only a read'},'read')
        self.select(body)
        obj={s:[] for s in SUMMARY_SECTIONS};obj['results']=[{'text':'Arena mapping verified','sources':[source],'status':'CURRENT'}]
        job={'coverage':dumps([source]),'parent_id':None,'chunk':dumps(body),'trigger_tokens':100,'chunk_tokens':100}
        for claim in ('Arena mapping verified','Arena mapping completed','Arena mapping committed'):
            with self.subTest(claim=claim):
                obj['results'][0]['text']=claim
                with self.assertRaises(CompactionFailure) as caught:
                    self.engine._persist_summary(self.engine.session_id,job,99,{source},dumps(obj),obj,{},None,time.monotonic(),{})
                self.assertEqual(caught.exception.code,'evidence_overstated')
        obj['results'][0]['text']='Arena mapping verified'
        with self.engine.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM summaries').fetchone()[0],0)
            db.execute('INSERT INTO summaries(session,created,covered,text,tokens,job_id,coverage_kind) VALUES (?,?,?,?,?,?,?)',
                (self.engine.session_id,0,dumps([source]),dumps(obj),len(dumps(obj)),0,'source_set'))
        self.select(body);self.assertNotIn('Arena mapping verified',dumps(self.state()))
        self.assertEqual(self.engine.store.record(self.engine.session_id,source),wire_message(body[-1]))

    def test_genuine_verification_claim_cites_successful_test_not_read(self):
        body=self.history();source=self.tool(body,'terminal',{'command':'python -m unittest test_arena'},
            {'exit_code':0,'output':'Ran 1 test\nOK'},'verify')
        self.select(body)
        obj={s:[] for s in SUMMARY_SECTIONS};obj['results']=[{'text':'Arena mapping verified','sources':[source],'status':'DERIVED'}]
        job={'coverage':dumps([source]),'coverage_kind':'source_set','parent_id':None,'chunk':dumps(body),'trigger_tokens':100,'chunk_tokens':100}
        self.engine._persist_summary(self.engine.session_id,job,99,{source},dumps(obj),obj,{},None,time.monotonic(),{})
        self.select(body);self.assertIn('Arena mapping verified',dumps(self.state()))


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(ReleaseReconciliationTests(n) for n in ReleaseReconciliationTests.__dict__ if n.startswith('test_'))
