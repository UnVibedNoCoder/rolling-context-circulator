import copy,json,unittest
from test_engine import EngineTests
from rolling_context.coherent import coherent_boundaries
from rolling_context.common import digest,wire_message

class CoherentTests(EngineTests):
    def setUp(self):
        super().setUp()
        self.engine.settings.update(selection_policy='coherent',target_tokens=1600,trigger_tokens=1800,tail_tokens=500,minimum_tail_tokens=200,recall_budget_tokens=500,warm_budget_tokens=200,segment_max_tokens=400,semantic_policy='cold_chunks',segmentation_policy='coherent',compact_source_aliases=True,summary_memory_max_tokens=2048)
    def test_new_query_recalls_old_evidence_without_mutating_prior_snapshot(self):
        body=[{'role':'system','content':'Keep invariants'},{'role':'user','content':'Original task pure src/genetics.py'},{'role':'assistant','content':'ALPHA_SENTINEL old_error E_LOCUS_419'}]+[{'role':'assistant','content':str(i)+'x'*180} for i in range(20)]+[{'role':'user','content':'Continue'}]
        first=self.select(body);frozen=copy.deepcopy(first)
        body.append({'role':'user','content':'exact ALPHA_SENTINEL old_error E_LOCUS_419'})
        second=self.select(body)
        self.assertIn('E_LOCUS_419',json.dumps(second));self.assertEqual(first,frozen)
    def test_complete_tool_result_and_interpretation_share_segment(self):
        body=[{'role':'user','content':'requirement'},{'role':'assistant','tool_calls':[{'id':'one'}]},{'role':'tool','tool_call_id':'one','content':'failed E_ERROR'},{'role':'assistant','content':'fix then test passed'},{'role':'user','content':'new unrelated task'}]
        cuts=coherent_boundaries(body,[20,20,20,20,20],200)
        self.assertEqual(cuts,[4])
    def test_large_ancient_tool_does_not_pin_all_later_history(self):
        body=[{'role':'system','content':'keep'},{'role':'user','content':'old'},{'role':'assistant','tool_calls':[{'id':'old'}]},{'role':'tool','tool_call_id':'old','content':'x'*4000},{'role':'assistant','content':'old result explained'}]
        for i in range(20):body += [{'role':'user','content':'unrelated '+str(i)},{'role':'assistant','content':'z'*300}]
        selected=self.select(body)
        self.assertLess(self.engine.counter.messages(selected),1800);self.assertNotIn(body[3],selected)
    def test_macro_recall_keeps_raw_sources_and_groups_as_one_unit(self):
        body=[{'role':'user','content':'requirement ALPHA_SENTINEL'},{'role':'assistant','tool_calls':[{'id':'one'}]},{'role':'tool','tool_call_id':'one','content':'error E_LOCUS_419'},{'role':'assistant','content':'fix E_LOCUS_419 and tests passed'},{'role':'user','content':'unrelated'}]
        hashes=[digest(wire_message(m)) for m in body];weights=self.engine.counter.weights(body)
        self.engine.store.snapshot('session-a','wire',body);self.engine.pages.ingest('session-a',body,weights)
        result=self.engine.pages.recall_segments('session-a','exact E_LOCUS_419',body,hashes,weights,set(hashes[:4]),3000,400)
        self.assertEqual(len(result),1);self.assertEqual(result[0]['source_ids'],hashes[:4])
        self.assertEqual(self.engine.store.record('session-a',hashes[2]),body[2])
    def test_aliases_are_expanded_to_exact_provenance(self):
        self.engine.settings.update(segmentation_policy='tool_safe',tail_tokens=60,minimum_tail_tokens=40,target_tokens=150,trigger_tokens=180)
        self.select();self.finish()
        with self.engine.store.connect() as db:
            row=db.execute('SELECT * FROM summaries').fetchone()
        self.assertIsNotNone(row)
        obj=json.loads(row['text']);self.assertTrue(all(len(s)==64 for v in obj.values() for x in v for s in x['sources']))

def load_tests(loader,tests,pattern):
    return unittest.TestSuite(CoherentTests(n) for n in CoherentTests.__dict__ if n.startswith('test_'))
