"""Archived evidence identity, echo exclusion, and eligible candidate ranking."""
import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path.home()/'.hermes/hermes-agent'))
from rolling_context.common import StateStore, digest, dumps, wire_message
from rolling_context.pages import PageManager
from rolling_context.visibility import ReadVisibility, read_key


class Counter:
    def text(self, text):
        return len(text)


def call(cid, name, **args):
    return {'role': 'assistant', 'tool_calls': [{'id': cid, 'type': 'function',
        'function': {'name': name, 'arguments': dumps(args)}}]}


def result(cid, value):
    return {'role': 'tool', 'tool_call_id': cid, 'content': dumps(value)}


class RetrievalCorrectnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = StateStore(self.temp.name)
        self.pages = PageManager(self.store, counter=Counter())

    def tearDown(self):
        self.temp.cleanup()

    def archive(self, body, session='a'):
        self.store.snapshot(session, 'wire', body)
        ids = [digest(wire_message(m)) for m in body]
        self.pages.ingest(session, body, [100] * len(body))
        return ids

    def test_mixed_fallback_preserves_bytes_regions_sessions_in_both_orders(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                session = 'reverse' if reverse else 'forward'
                engine = SimpleNamespace(store=self.store, session_id=session, counter=Counter())
                visibility = ReadVisibility(engine)
                args_a = {'path': '/missing/a.py', 'offset': 3, 'limit': 2}
                args_b = {'path': '/missing/b.py', 'offset': 9, 'limit': 1}
                old = [call('old-a', 'read_file', **args_a), result('old-a', {'content': 'EXACT_A\r\nα\r\n'})]
                wrong = [call('wrong', 'read_file', **{**args_a, 'offset': 4}), result('wrong', {'content': 'WRONG_REGION'})]
                foreign = [call('foreign', 'read_file', **args_a), result('foreign', {'content': 'WRONG_SESSION'})]
                self.archive(foreign, session+'-other')
                selected = [call('dedup', 'read_file', **args_a), result('dedup',
                    {'status': 'unchanged', 'dedup': True, 'content_returned': False}),
                    call('success', 'read_file', **args_b), result('success', {'content': 'EXACT_B\r\nβ\r\n'})]
                if reverse:
                    selected = selected[2:] + selected[:2]
                selected += [call('error', 'read_file', path='/missing/error.py'), result('error', {'error': 'permission denied'})]
                body = old + wrong + selected
                ids = self.archive(body, session)
                visibility.observe(body, ids)
                before = copy.deepcopy(selected)
                with patch('rolling_context.file_reads.ordinary_read', side_effect=FileNotFoundError('offline')):
                    projected, recovered = visibility.reconcile(selected)
                self.assertEqual(selected, before)
                self.assertEqual({r['tool_call_id'] for r in recovered}, {'dedup', 'success'})
                for cid, expected, region, source in [('dedup', 'EXACT_A\r\nα\r\n', args_a, ids[1]),
                        ('success', 'EXACT_B\r\nβ\r\n', args_b, digest(next(m for m in selected if m.get('tool_call_id')=='success')))]:
                    payload = json.loads(next(m for m in projected if m.get('tool_call_id')==cid)['content'])
                    self.assertEqual(payload['content'].encode('utf-8'), expected.encode('utf-8'))
                    self.assertEqual(payload['archived_source_id'], source)
                    self.assertEqual(payload['authority'], 'archived_raw')
                    self.assertFalse(payload['disk_rechecked'])
                    info = next(r for r in recovered if r['tool_call_id']==cid)
                    self.assertEqual(info['region'], read_key(call(cid, 'read_file', **region)['tool_calls'][0]))
                    self.assertEqual(self.store.record(session, source)['content'], dumps({'content': expected}))
                self.assertEqual(projected[-1], selected[-1])

    def test_automatic_seeds_and_macro_rendering_exclude_archive_tools(self):
        for tool in ('rolling_history_search', 'rolling_history_read', 'rolling_snapshot_read', 'rolling_raw_read'):
            with self.subTest(tool=tool):
                source = {'role': 'assistant', 'content': 'GENOME_CONSTRAINT original literal'}
                body = [source, call('echo', tool, query='GENOME_CONSTRAINT'), result('echo', {'content': 'GENOME_CONSTRAINT retrieval echo'})]
                ids = self.archive(body, tool)
                hits = self.pages.recall(tool, 'GENOME_CONSTRAINT', set(ids), 6000, relevance_first=True)
                self.assertEqual([h['source_id'] for h in hits], [ids[0]])
                macro = self.pages.recall_segments(tool, 'GENOME_CONSTRAINT', body, ids, [100]*3, set(ids), 6000)
                self.assertTrue(macro)
                self.assertEqual(macro[0]['source_ids'], [ids[0]])
                self.assertEqual([e['source_id'] for e in macro[0]['data']], [ids[0]])
                self.assertEqual(self.pages.search_ids(tool, 'GENOME_CONSTRAINT'), [ids[0]])
                # Explicit source reads and hash lookups still expose exact RAW.
                self.assertEqual(self.store.record(tool, ids[2]), body[2])
                self.assertEqual(self.pages.search_ids(tool, ids[2]), [ids[2]])
                self.assertEqual(self.pages.recall(tool, ids[2], set(ids), 6000), [])

    def test_existing_index_classification_upgrades_without_touching_raw(self):
        body = [{'role':'assistant','content':'original GENOME_CONSTRAINT'},
                call('old', 'rolling_raw_read', source_id='literal'),
                result('old', {'content':'GENOME_CONSTRAINT echo'})]
        ids = self.archive(body)
        with self.store.connect() as db:
            db.execute("UPDATE pages SET tags='[]'")
            db.execute("DELETE FROM page_index_versions WHERE name='archive_tools'")
            raw_before = [tuple(r) for r in db.execute('SELECT * FROM records ORDER BY hash')]
        restarted = PageManager(self.store, counter=Counter())
        # Even if the latest transcript has dropped the old tool call/result.
        restarted.ingest('a', [body[0]], [100])
        self.assertEqual([h['source_id'] for h in restarted.recall('a','GENOME_CONSTRAINT',set(ids),6000)], [ids[0]])
        with self.store.connect() as db:
            self.assertEqual(raw_before, [tuple(r) for r in db.execute('SELECT * FROM records ORDER BY hash')])
        self.assertEqual(self.store.record('a', ids[2]), body[2])

    def test_current_file_capture_remains_eligible_original_evidence(self):
        body = [call('disk','rolling_file_snapshot',path='/source.py'),
                result('disk',{'content':'GENOME_CONSTRAINT current capture'})]
        ids = self.archive(body)
        self.assertIn(ids[1], self.pages.automatic_ids('a', set(ids)))
        self.assertEqual(self.pages.recall('a', 'GENOME_CONSTRAINT', set(ids), 6000)[0]['source_id'], ids[1])

    def test_eligible_cold_hit_survives_more_than_100_ineligible_hot_hits(self):
        cold = {'role': 'assistant', 'content': 'needle ' + 'background ' * 5000}
        hot = [{'role': 'assistant', 'content': f'needle needle needle hot_record_{i}'} for i in range(110)]
        ids = self.archive([cold] + hot)
        hits = self.pages.recall('a', 'needle', {ids[0]}, 6000, relevance_first=True)
        self.assertEqual([h['source_id'] for h in hits], [ids[0]])
        self.assertEqual(hits[0]['message'], cold)

    def test_exact_identifier_candidates_apply_eligibility(self):
        body = [{'role':'assistant', 'content':f'port 8084 hot_{i}'} for i in range(110)]
        cold = {'role':'assistant', 'content':'port 8084 cold'}
        ids = self.archive(body + [cold])
        hits = self.pages.recall('a', '8084', {ids[-1]}, 6000)
        self.assertEqual([h['source_id'] for h in hits], [ids[-1]])
        self.assertEqual(self.pages.recall('other', '8084', set(ids), 6000), [])


if __name__ == '__main__':
    unittest.main()
