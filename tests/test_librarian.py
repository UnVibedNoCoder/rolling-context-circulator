"""Reasoning, safe fallback, privacy and actual stored-memory reuse regressions."""
import copy
import io
import json
import time
import types
import unittest
from unittest.mock import patch

from test_engine import EngineTests, Counter
from rolling_context.common import digest, dumps, stream_chat, wire_message
from rolling_context.compaction import CompactionFailure, failure_fields, request_timeout
from rolling_context.continuity import choose_warm
from rolling_context.engine import RollingContextEngine
from rolling_context.librarian import validate_librarian
from rolling_context.summary import SUMMARY_SECTIONS, validate_summary, validate_memory_summary
from rolling_context.cli import generated_config

SECRET = 'PRIVATE_REASONING_SENTINEL_NOT_FOR_STORAGE'


def block(source='s0', librarian=True):
    value = {s: [] for s in SUMMARY_SECTIONS}
    item = {'text': 'fixture recorded; preserve exact paths', 'sources': [source]}
    if librarian:
        item['status'] = 'RAW_HISTORY'
    value['task_state'] = [item]
    return value


class LibrarianTests(EngineTests):
    def setUp(self):
        super().setUp()
        self.engine.settings.update(compactor_mode='librarian', compact_source_aliases=True)

    def response(self, payload, *, hidden=SECRET, finish='stop'):
        source = json.loads(payload['messages'][1]['content'])['transcript_chunk'][0]['source_id']
        value = block(source, payload['chat_template_kwargs']['enable_thinking'])
        return {'choices': [{'finish_reason': finish, 'message': {
            'content': json.dumps(value, indent=2), 'reasoning_content': hidden}}],
            'usage': {'prompt_tokens': 90, 'completion_tokens': 40},
            'timings': {'prompt_ms': 10, 'reasoning_content': SECRET}}

    def events(self):
        return [json.loads(line) for line in (self.root/'telemetry.jsonl').read_text().splitlines()]

    def invoke(self, response=None):
        self.select()
        self.finish(response or (lambda url, payload, timeout, **kw: self.response(payload)))

    def direct(self, mode, response=None):
        chunk = self.messages[1:3]
        ids = {digest(wire_message(m)) for m in chunk}
        aliases = {f's{i}': digest(wire_message(m)) for i, m in enumerate(chunk)}
        names = {s: a for a, s in aliases.items()}
        with patch('rolling_context.engine.stream_chat', side_effect=response or
                   (lambda u, p, timeout, **kw: self.response(p, hidden=SECRET if mode=='librarian' else ''))):
            return getattr(self.engine, '_summarize_'+mode)(chunk, ids, aliases, set(aliases), names, time.monotonic())

    def test_separate_reasoning_accepted_discarded_and_never_stored_or_logged(self):
        calls=[]; responses=[]
        def capture(url,payload,timeout,**kw):
            calls.append((payload, timeout, kw)); result=self.response(payload);responses.append(result);return result
        original=copy.deepcopy(self.messages)
        self.invoke(capture)
        with self.engine.store.connect() as db:
            row=dict(db.execute('SELECT * FROM summaries').fetchone())
            job=dict(db.execute('SELECT * FROM jobs').fetchone())
            records=[tuple(r) for r in db.execute('SELECT * FROM records')]
        self.assertEqual(job['status'],'ready')
        self.assertNotIn(SECRET,dumps([row,job,records,self.events()]))
        self.assertNotIn('reasoning_content',responses[0]['choices'][0]['message'])
        self.assertEqual(self.messages,original)
        for m in original[1:]:
            self.assertEqual(self.engine.store.record('session-a',digest(wire_message(m))),wire_message(m))
        p, timeout, kw=calls[0]
        self.assertTrue(p['chat_template_kwargs']['enable_thinking'])
        self.assertEqual(p['reasoning_effort'],'medium')
        self.assertEqual(p['max_tokens'],4096)
        self.assertEqual(kw,{'discard_reasoning':True})
        self.assertLessEqual(timeout,420)
        ready=next(e for e in self.events() if e['event']=='compaction_ready')
        self.assertEqual((ready['mode'],ready['thinking_enabled'],ready['reasoning_effort'],ready['fallback_used']),('librarian',True,'medium',False))
        self.assertEqual(ready['retained_source_count'],1)
        self.assertGreater(ready['source_count'],1)
        self.assertEqual(ready['timings'],{'prompt_ms':10})
        self.assertEqual(ready['result_tokens'],row['tokens'])

    def test_newlines_are_valid_json_and_successful_fallback_is_persisted(self):
        calls=[]
        def capture(url,payload,timeout,**kw):
            calls.append(payload)
            if payload['chat_template_kwargs']['enable_thinking']:
                return self.response(payload,finish='length')
            return self.response(payload,hidden='')
        self.invoke(capture)
        ready=next(e for e in self.events() if e['event']=='compaction_ready')
        fallback=next(e for e in self.events() if e['event']=='librarian_fallback')
        self.assertEqual(fallback['error_code'],'incomplete_output')
        self.assertEqual(fallback['finish_reason'],'length')
        self.assertEqual((ready['mode'],ready['thinking_enabled'],ready['reasoning_effort'],ready['fallback_used']),('baseline',False,'none',True))
        self.assertEqual(calls[1]['max_tokens'],1536)
        self.assertNotIn(SECRET,dumps(self.events()))
        with self.engine.store.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM jobs').fetchone()[0],'ready')
            self.assertEqual(db.execute('SELECT count(*) FROM summaries').fetchone()[0],1)

    def test_librarian_blocks_reused_after_restart_without_mutating_inflight_request(self):
        before=self.select();frozen=copy.deepcopy(before)
        self.finish(lambda u,p,timeout,**kw:self.response(p))
        self.assertEqual(before,frozen)
        self.assertIn('IMMUTABLE BLOCK',dumps(self.select()))
        restored=RollingContextEngine(settings={**self.settings,'compactor_mode':'librarian'},counter=Counter())
        restored.on_session_start('session-a')
        selected=restored.select_context(self.messages,conversation_messages=self.messages)
        self.assertIn('IMMUTABLE BLOCK',dumps(selected))
        self.assertIn('RAW_HISTORY',dumps(selected))
        with self.engine.store.connect() as db:
            row=dict(db.execute('SELECT * FROM summaries').fetchone())
        represented=set()
        warm=choose_warm(self.engine,[row],set(json.loads(row['covered'])),represented,'preserve paths')
        self.assertEqual([r['id'] for r in warm],[row['id']])

    def test_visible_think_including_escaped_tags_is_rejected(self):
        for tag in ['<think>'+SECRET+'</think>','<THINK>'+SECRET+'</THINK>','</think>',r'\u003cthink\u003e']:
            with self.subTest(tag=tag):
                def capture(u,p,timeout,**kw):
                    result=self.response(p)
                    self.assertIn('fixture recorded',result['choices'][0]['message']['content'])
                    if tag.startswith('\\u'):
                        result['choices'][0]['message']['content']=result['choices'][0]['message']['content'].replace('fixture recorded',tag)
                    else:
                        result['choices'][0]['message']['content']=result['choices'][0]['message']['content'].replace('fixture recorded',tag)
                    return result
                with self.assertRaises(CompactionFailure) as caught:self.direct('librarian',capture)
                self.assertEqual(caught.exception.code,'visible_reasoning')
                self.assertNotIn(SECRET,str(caught.exception))
        def baseline(u,p,timeout):
            return self.response(p)
        with self.assertRaises(CompactionFailure) as caught:self.direct('baseline',baseline)
        self.assertEqual(caught.exception.code,'unexpected_reasoning')

    def test_failures_have_distinct_safe_diagnostics(self):
        for code,mutate in [
            ('json_invalid',lambda r:r['choices'][0]['message'].update(content='{'+SECRET)),
            ('schema_invalid',lambda r:r['choices'][0]['message'].update(content=dumps({'invalid':SECRET}))),
            ('provenance_invalid',lambda r:r['choices'][0]['message'].update(content=dumps(block('unknown')))),
            ('schema_invalid',lambda r:r['choices'][0]['message'].update(content=dumps({**block(), 'task_state':[{'text':'fact','sources':['s0'],'status':'BOGUS'}]}))),
            ('incomplete_output',lambda r:r['choices'][0].update(finish_reason='length')),
        ]:
            with self.subTest(code=code):
                def capture(u,p,timeout,**kw):r=self.response(p);mutate(r);return r
                with self.assertRaises(CompactionFailure) as caught:self.direct('librarian',capture)
                self.assertEqual(caught.exception.code,code)
                self.assertNotIn(SECRET,dumps(failure_fields(caught.exception)))
        self.engine.settings['summary_memory_max_tokens']=1
        with self.assertRaises(CompactionFailure) as caught:self.direct('librarian')
        self.assertEqual(caught.exception.code,'memory_budget')

    def test_baseline_fallback_failure_preserves_raw_and_reports_both_stages(self):
        def capture(u,p,timeout,**kw):
            if p['chat_template_kwargs']['enable_thinking']:
                return self.response(p,finish='length')
            r=self.response(p,hidden='');r['choices'][0]['message']['content']='{'+SECRET;return r
        self.invoke(capture)
        event=next(e for e in self.events() if e['event']=='compaction_failed')
        self.assertEqual((event['failure_stage'],event['error_code'],event['fallback_used'],event['librarian_error_code']),('baseline','json_invalid',True,'incomplete_output'))
        self.assertNotIn(SECRET,dumps(self.events()))
        with self.engine.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM summaries').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT status FROM jobs').fetchone()[0],'failed')
        for m in self.messages[1:]:
            self.assertEqual(self.engine.store.record('session-a',digest(wire_message(m))),wire_message(m))

    def test_unknown_exception_message_cannot_expose_reasoning(self):
        def capture(u,p,timeout,**kw):raise ValueError(SECRET)
        self.invoke(capture)
        events=self.events()
        self.assertNotIn(SECRET,dumps(events))
        self.assertEqual(next(e for e in events if e['event']=='compaction_failed')['error_code'],'internal_error')

    def test_generation_allowance_is_bounded_independently_of_memory(self):
        for allowance in [0,4097,True,'4096']:
            self.engine.settings['librarian_max_tokens']=allowance
            with self.assertRaises(CompactionFailure) as caught:self.direct('librarian')
            self.assertEqual(caught.exception.code,'configuration_invalid')
        self.assertEqual(self.engine.settings['summary_memory_max_tokens'],4096)
        self.assertEqual(self.engine.settings['generation_reserve_tokens'],8192)
        self.assertEqual((self.engine.settings['target_tokens'],self.engine.settings['trigger_tokens']),(2000,36000))

    def test_fallback_receives_reserved_time_without_resetting_job_deadline(self):
        observed=[]
        now=[100.0]
        def capture(u,p,timeout,**kw):
            observed.append(timeout)
            if p['chat_template_kwargs']['enable_thinking']:
                now[0]+=timeout
                raise TimeoutError(SECRET)
            return self.response(p,hidden='')
        chunk=self.messages[1:3];ids={digest(wire_message(m)) for m in chunk}
        with patch('rolling_context.compaction.time.monotonic',side_effect=lambda:now[0]),patch('rolling_context.engine.stream_chat',side_effect=capture),patch.object(self.engine,'_persist_summary') as persist:
            self.engine._run_compaction('session-a',{},1,chunk,ids,{},ids,{},100.0)
        self.assertEqual(observed,[420.0,180.0])
        self.assertTrue(persist.call_args.args[-1]['fallback_used'])
        self.assertEqual(persist.call_args.args[-1]['librarian_error_code'],'timeout')
        with patch('rolling_context.compaction.time.monotonic',return_value=701):
            with self.assertRaises(CompactionFailure):request_timeout(self.engine.settings,100,'baseline')
        with patch('rolling_context.compaction.time.monotonic',return_value=550):
            with self.assertRaises(CompactionFailure):request_timeout(self.engine.settings,100,'librarian')
            self.assertEqual(request_timeout(self.engine.settings,100,'baseline'),150)


class ValidationTests(unittest.TestCase):
    def test_strict_status_conflict_supersession_and_raw_linkage_preserved(self):
        obj=block();obj['decisions']=[{'text':'older setting','sources':['s1'],'status':'SUPERSEDED'},
                                    {'text':'unresolved contradiction','sources':['s0','s1'],'status':'CONFLICTING'},
                                    {'text':'interpretation','sources':['s1'],'status':'DERIVED'}]
        self.assertEqual(validate_memory_summary(dumps(obj),{'s0','s1'}),obj)
        with self.assertRaises(ValueError):validate_summary(dumps(obj),{'s0','s1'})
        obj['decisions'][0].pop('status')
        with self.assertRaises(ValueError):validate_memory_summary(dumps(obj),{'s0','s1'})
        for source in [[],{},None,'unknown']:
            bad=block();bad['task_state'][0]['sources']=[source]
            with self.assertRaises(ValueError):validate_librarian(dumps(bad),{'s0'})

    def test_new_large_config_matches_experiment_and_baseline_auxiliary(self):
        from pathlib import Path
        args=types.SimpleNamespace(profile='large',state_dir=Path('/tmp/test-state'))
        cfg=generated_config({},args)
        self.assertEqual(cfg['context']['local_rolling']['compactor_mode'],'librarian')
        self.assertEqual(cfg['agent']['reasoning_effort'],'medium')
        aux=cfg['auxiliary']['compression']
        self.assertEqual(aux['reasoning_effort'],'none')
        self.assertFalse(aux['extra_body']['chat_template_kwargs']['enable_thinking'])
        policy=cfg['context']['local_rolling']
        self.assertEqual((policy['summary_max_tokens'],policy['summary_memory_max_tokens'],policy['librarian_max_tokens']),(1536,4096,4096))
        self.assertEqual((policy['target_tokens'],policy['trigger_tokens'],policy['generation_reserve_tokens']),(32000,36000,8192))

    def test_sse_discards_hidden_delta_while_retaining_presence_and_final_json(self):
        lines=[{'choices':[{'delta':{'reasoning_content':SECRET}}]},
               {'choices':[{'delta':{'content':dumps(block())}}]},
               {'choices':[{'delta':{},'finish_reason':'stop'}],'usage':{'completion_tokens':50}}]
        class Response(io.BytesIO):
            def __init__(self):
                super().__init__(b''.join(b'data: '+dumps(x).encode()+b'\n\n' for x in lines)+b'data: [DONE]\n')
                self.fp=types.SimpleNamespace(raw=types.SimpleNamespace(_sock=types.SimpleNamespace(settimeout=lambda x:None)))
        opener=types.SimpleNamespace(open=lambda *a,**kw:Response())
        with patch('rolling_context.common.urllib.request.build_opener',return_value=opener):
            result=stream_chat('http://localhost',{},discard_reasoning=True)
        self.assertNotIn(SECRET,dumps(result))
        message=result['choices'][0]['message']
        self.assertTrue(message['reasoning_present'])
        self.assertEqual(json.loads(message['content']),block())


def load_tests(loader,tests,pattern):
    suite=unittest.TestSuite(LibrarianTests(n) for n in LibrarianTests.__dict__ if n.startswith('test_'))
    suite.addTests(loader.loadTestsFromTestCase(ValidationTests))
    return suite
