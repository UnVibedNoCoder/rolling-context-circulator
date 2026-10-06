"""Strict additive execution memory, lossless deduplication and durable reuse."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path[:0]=[str(Path(__file__).resolve().parents[1]),str(Path.home()/'.hermes/hermes-agent')]
from test_librarian import LibrarianTests,block
from test_loop_guard import Counter,Harness,FIXTURE
from rolling_context.common import digest,dumps,wire_message
from rolling_context.engine import RollingContextEngine
from rolling_context.librarian import (LIBRARIAN_INSTRUCTIONS,REOPEN_IF,validate_librarian,
                                       librarian_schema,condense_librarian)
from rolling_context.summary import validate_summary,validate_memory_summary


def execution_block(source):
    obj=block(source)
    obj['decisions']=[{'text':'User labels stay private to the submitting client and never cross clients.',
        'sources':[source],'status':'DERIVED','decision_lock':{'reopen_if':list(REOPEN_IF)}}]
    obj['next_actions']=[{'text':'Create the worker project files and run a concrete build.',
        'sources':[source],'status':'DERIVED','execution':{'planning_complete':True,'blocked_by':[]}}]
    return obj

class SchemaTests(unittest.TestCase):
    def test_old_baseline_and_librarian_formats_remain_readable(self):
        for librarian in (True,False):
            obj=block('s0',librarian)
            self.assertEqual(validate_memory_summary(dumps(obj),{'s0'}),obj)
        with self.assertRaises(ValueError):validate_summary(dumps(execution_block('s0')),{'s0'})

    def test_strict_execution_schema_preserves_sources_and_derived_status(self):
        obj=execution_block('s0');self.assertEqual(validate_librarian(dumps(obj),{'s0'}),obj)
        schema=librarian_schema()
        self.assertFalse(schema['additionalProperties'])
        self.assertIn('decision_lock',schema['properties']['decisions']['items']['properties'])
        self.assertNotIn('decision_lock',schema['properties']['task_state']['items']['properties'])
        for section,key in [('decisions','decision_lock'),('next_actions','execution')]:
            bad=copy.deepcopy(obj);bad[section][0]['status']='CURRENT'
            with self.assertRaises(ValueError):validate_librarian(dumps(bad),{'s0'})
            bad=copy.deepcopy(obj);bad['results']=[bad[section][0]]
            with self.assertRaises(ValueError):validate_librarian(dumps(bad),{'s0'})
        bad=copy.deepcopy(obj);bad['decisions'][0]['sources']=['invented-source']
        with self.assertRaises(ValueError):validate_librarian(dumps(bad),{'s0'})

    def test_invalid_blockers_conditions_extra_keys_and_think_leak_fail_strictly(self):
        mutations=[lambda x:x['next_actions'][0]['execution'].update(planning_complete=1),
                   lambda x:x['next_actions'][0]['execution'].update(blocked_by=['x'*181]),
                   lambda x:x['next_actions'][0]['execution'].update(blocked_by=['<think>private</think>']),
                   lambda x:x['decisions'][0]['decision_lock'].update(reopen_if=['never_reopen']),
                   lambda x:x['decisions'][0]['decision_lock'].update(reopen_if=[REOPEN_IF[0]]*2),
                   lambda x:x['decisions'][0]['decision_lock'].update(reasoning_content='secret'),
                   lambda x:x['next_actions'][0]['execution'].update(irreversible=True)]
        for mutate in mutations:
            obj=execution_block('s0');mutate(obj)
            with self.assertRaises(ValueError):validate_librarian(dumps(obj),{'s0'})

    def test_exact_repetition_collapses_without_losing_evidence_or_conflicts(self):
        obj=execution_block('s0')
        dup=copy.deepcopy(obj['decisions'][0]);dup['sources']=['s1','s0'];obj['decisions'].append(dup)
        conflict=copy.deepcopy(dup);conflict.pop('decision_lock');conflict['status']='CONFLICTING';obj['decisions'].append(conflict)
        original=copy.deepcopy(obj);value=condense_librarian(obj)
        self.assertEqual(obj,original);self.assertEqual(len(value['decisions']),2)
        self.assertEqual(value['decisions'][0]['sources'],['s0','s1'])
        self.assertEqual(value['decisions'][1]['status'],'CONFLICTING')
        self.assertEqual(validate_librarian(dumps(value),{'s0','s1'}),value)
        for i in range(2,11):
            item=copy.deepcopy(dup);item['sources']=['s'+str(i)];obj['decisions'].append(item)
        value=condense_librarian(obj)
        self.assertTrue(all(len(item['sources'])<=8 for item in value['decisions']))
        self.assertEqual({s for i in value['decisions'] for s in i['sources']},{'s'+str(i) for i in range(11)})

    def test_librarian_instructions_collapse_plans_but_preserve_genuine_evidence(self):
        for phrase in ['collapse repeated restatements','genuine conflicts','underlying RAW sources',
                       'never another lock as independent','freshest action','real blockers']:
            self.assertIn(phrase,LIBRARIAN_INSTRUCTIONS)

class DurableMemoryTests(unittest.TestCase):
    def test_restart_reuses_locks_and_next_action_without_restoring_pressure(self):
        with tempfile.TemporaryDirectory() as root:
            history=[{'role':'user','content':FIXTURE['user']},{'role':'assistant','content':'The implementation plan is sufficient.'}]
            settings={'state_dir':root,'loop_guard':{'enabled':True}}
            e=RollingContextEngine(settings=settings,counter=Counter());e.on_session_start('same-session');e._stop.set()
            ids=[digest(wire_message(m)) for m in history]
            e.store.snapshot(e.session_id,'wire',history)
            obj=execution_block(ids[1]);text=dumps(obj)
            with e.store.connect() as db:
                db.execute('INSERT INTO summaries(session,created,covered,text,tokens,job_id,coverage_kind) VALUES (?,?,?,?,?,?,?)',
                           (e.session_id,1,dumps(ids),text,e.counter.text(text),0,'source_set'))
            restored=RollingContextEngine(settings=settings,counter=Counter());restored.on_session_start('same-session');restored._stop.set()
            restored.select_context(history,conversation_messages=history)
            guard=restored._loop_guard
            self.assertEqual(len(guard.locks),1)
            self.assertEqual(guard.next_action['text'],obj['next_actions'][0]['text'])
            self.assertEqual(guard.next_action['sources'],[ids[1]])
            self.assertTrue(guard.planning_complete);self.assertFalse(guard.active);self.assertEqual(guard.no_progress_turns,0)
            for plan in FIXTURE['plans']:
                history.append({'role':'assistant','content':plan});selected=restored.select_context(history,conversation_messages=history)
                if guard.active:break
            self.assertTrue(guard.active)
            with restored.store.connect() as db:
                self.assertEqual(db.execute('SELECT text FROM summaries').fetchone()[0],text)
            newer=history+[{'role':'user','content':'Change requirement: implement an explicitly public label option.'}]
            restored.select_context(newer,conversation_messages=newer)
            self.assertFalse(guard.locks);self.assertIsNone(guard.next_action)
            # The same stale durable representation is not reintroduced.
            newer.append({'role':'assistant','content':'Plan the changed requirement.'})
            restored.select_context(newer,conversation_messages=newer)
            self.assertFalse(guard.locks)

    def test_fresh_anchor_can_cite_old_requirement_plus_new_evidence(self):
        h=Harness();h.tool('read_file',{'path':'api.py'},{'content':'New API constraint'})
        h.append('assistant','The current implementation action follows the newly observed constraint.')
        hashes=[digest(wire_message(m)) for m in h.history]
        obj=execution_block(hashes[0])
        for item in obj['decisions']+obj['next_actions']:item['sources'].append(hashes[-1])
        row={'id':1,'covered':dumps([hashes[0],hashes[-1]]),'text':dumps(obj)}
        h.history.append({'role':'assistant','content':'Visible continuation.'})
        hashes=[digest(wire_message(m)) for m in h.history]
        h.guard.observe(h.session,h.history,hashes,[row])
        self.assertTrue(h.guard.planning_complete);self.assertIsNotNone(h.guard.next_action)
        self.assertEqual(h.guard.next_action['sources'],[hashes[0],hashes[-2]])

    def test_duplicate_derived_lock_is_not_independent_corroboration(self):
        h=Harness();source=digest(wire_message(h.history[0]));obj=execution_block(source)
        rows=[{'id':i,'covered':dumps([source]),'text':dumps(obj)} for i in range(1,4)]
        h.guard.observe(h.session,h.history,[source],rows)
        # A fresh request observes durable memory; identical-request retries
        # are frozen, so extend with a neutral assistant first.
        h.append('assistant','Acknowledged task constraints.')
        hashes=[digest(wire_message(m)) for m in h.history]
        # Use fresh guard to hydrate against the initial full RAW history.
        from rolling_context.loop_guard import ReasoningLoopGuard
        g=ReasoningLoopGuard({'enabled':True},lambda *a,**k:None)
        g.observe('fixture',h.history,hashes,rows)
        self.assertEqual(len(g.locks),1)
        self.assertEqual(next(iter(g.locks.values()))['sources'],[source])
        self.assertEqual(g.progress_events,0)
        invalid={'id':4,'covered':dumps(['invented']),'text':dumps(execution_block('invented'))}
        g.observe('fixture',h.history+[{'role':'assistant','content':'Additional visible context.'}],hashes+[digest({'role':'assistant','content':'Additional visible context.'})],[invalid])
        self.assertEqual(len(g.locks),1)

class CompactionMemoryTests(LibrarianTests):
    def test_real_dispatch_retains_execution_metadata_after_alias_expansion(self):
        def response(url,payload,timeout,**kw):
            source=json.loads(payload['messages'][1]['content'])['transcript_chunk'][0]['source_id']
            obj=execution_block(source);obj['decisions'].append(copy.deepcopy(obj['decisions'][0]))
            return {'choices':[{'finish_reason':'stop','message':{'content':dumps(obj),'reasoning_content':'PRIVATE_NOT_RETAINED'}}]}
        self.invoke(response)
        with self.engine.store.connect() as db:row=dict(db.execute('SELECT * FROM summaries').fetchone())
        obj=validate_memory_summary(row['text'],set(json.loads(row['covered'])))
        self.assertEqual(len(obj['decisions']),1)
        self.assertEqual(obj['decisions'][0]['status'],'DERIVED')
        self.assertEqual(len(obj['decisions'][0]['sources'][0]),64)
        self.assertTrue(obj['next_actions'][0]['execution']['planning_complete'])
        self.assertNotIn('PRIVATE_NOT_RETAINED',dumps([obj,self.events()]))


def load_tests(loader,tests,pattern):
    suite=unittest.TestSuite()
    for cls in (SchemaTests,DurableMemoryTests):suite.addTests(loader.loadTestsFromTestCase(cls))
    suite.addTests(CompactionMemoryTests(n) for n in CompactionMemoryTests.__dict__ if n.startswith('test_'))
    return suite
