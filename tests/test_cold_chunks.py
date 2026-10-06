import json
from test_engine import EngineTests
from rolling_context.common import digest, wire_message


class ColdChunkTests(EngineTests):
    def setUp(self):
        super().setUp()
        self.engine.settings.update(semantic_policy='cold_chunks',chunk_target=96)

    def test_oversized_first_group_does_not_starve_later_cold_work(self):
        body=[{'role':'user','content':'large objective '+('a'*200)}]+[
            {'role':'assistant','content':('b'+str(i))*16} for i in range(6)]
        hashes=[digest(wire_message(m)) for m in body]
        self.engine.store.snapshot('session-a','wire',body)
        self.engine.pages.ingest('session-a',body,self.engine.counter.weights(body))
        self.engine._schedule(body,hashes,self.engine.counter.weights(body),None,0,2000,max_end=5)
        with self.engine.store.connect() as db:
            job=dict(db.execute('SELECT * FROM jobs').fetchone())
        self.assertEqual(job['coverage_kind'],'source_set')
        covered=json.loads(job['coverage'])
        self.assertNotIn(hashes[0],covered)
        self.assertEqual(covered,hashes[1:4])
        self.finish()
        self.assertEqual(self.engine.store.compatible_summary('session-a',hashes),(None,0))
        blocks=self.engine.store.compatible_page_blocks('session-a',hashes)
        self.assertEqual(len(blocks),1)
        self.assertEqual(json.loads(blocks[0]['covered']),covered)
        self.assertEqual(self.engine.store.record('session-a',hashes[0]),body[0])
        self.assertEqual(self.engine.store.compatible_page_blocks('other',hashes),[])
        self.assertEqual(self.engine.store.compatible_page_blocks('session-a',[hashes[0]]),[])

    def test_current_tool_group_is_never_scheduled_as_cold(self):
        body=[{'role':'user','content':'a'*80},
              {'role':'assistant','content':'','tool_calls':[{'id':'a'},{'id':'b'}]},
              {'role':'tool','tool_call_id':'a','content':'a'*32},
              {'role':'tool','tool_call_id':'b','content':'b'*32},
              {'role':'assistant','content':'continue'}]
        hashes=[digest(wire_message(m)) for m in body]
        self.engine._schedule(body,hashes,self.engine.counter.weights(body),None,0,2000,max_end=3)
        with self.engine.store.connect() as db:
            job=dict(db.execute('SELECT * FROM jobs').fetchone())
        self.assertEqual(json.loads(job['coverage']),hashes[:1])
        self.assertEqual(json.loads(job['chunk']),body[:1])


def load_tests(loader, tests, pattern):
    import unittest
    return unittest.TestSuite(ColdChunkTests(name) for name in ColdChunkTests.__dict__ if name.startswith('test_'))
