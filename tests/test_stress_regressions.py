import json,unittest
from test_engine import EngineTests
from rolling_context.common import digest,wire_message
from rolling_context.working_set import ElasticWorkingSet, segment_metrics

class StressRegressions(EngineTests):
    def test_relevance_search_does_not_return_its_own_memory_queries(self):
        source={'role':'assistant','content':'GENOME_CONSTRAINT multiplier 7 bias 19 modulus 97'}
        query={'role':'assistant','tool_calls':[{'id':'search','function':{'name':'rolling_history_search','arguments':'GENOME_CONSTRAINT multiplier bias modulus'}}]}
        result={'role':'tool','tool_call_id':'search','content':'GENOME_CONSTRAINT copy of previous search'}
        body=[source,query,result]
        self.engine.store.snapshot('session-a','wire',body);self.engine.pages.ingest('session-a',body,[100,100,100])
        ids=self.engine.pages.search_ids('session-a','GENOME_CONSTRAINT',5,relevance_first=True)
        self.assertEqual(ids,[digest(wire_message(source))])
    def test_stop_prevents_recursive_background_draining(self):
        self.engine._stop.set();self.select()
        with self.engine.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0],0)
    def test_queue_reports_age_and_does_not_duplicate_pending_source(self):
        self.select();self.select()
        status=self.engine.store.job_status('session-a')
        self.assertEqual(status['compaction_queue'],1);self.assertGreaterEqual(status['oldest_queue_age_seconds'],0)
    def test_segment_metrics_ignore_old_identical_occurrences(self):
        groups=[{'segment_id':str(i),'source_ids':['same'],'start':i,'end':i+1} for i in range(20)]
        ledger=[{'page_id':'same','source_id':'same','representation':'RAW','reason':'coherent_recent'}]
        metrics,_=segment_metrics(groups,ledger,{'same':100},raw_start=19)
        self.assertEqual(metrics['raw_segment_count'],1)
        self.assertEqual(metrics['selected_segments'],1)
        self.assertEqual(metrics['segment_mean_tokens'],100)
    def test_elastic_tiers_cover_full_range_without_oscillation(self):
        p=ElasticWorkingSet({'floor_tokens':12000,'normal_tokens':28000,'wide_tokens':48000,'ceiling_tokens':64000,'grow_turns':2,'shrink_turns':6,'cooldown_turns':4})
        target=28000;changes=[]
        for i in range(20):
            demand=60000 if i<2 else 8000
            target,change=p.observe(str(i),target,demand,1000)
            if change:changes.append(change)
        self.assertEqual([c['to_tokens'] for c in changes],[64000,48000,28000,12000])
        self.assertTrue(all(b['turn']-a['turn']>=4 for a,b in zip(changes,changes[1:])))
        self.assertEqual(p.observe('19',target,65000,1000)[0],12000)

def load_tests(loader,tests,pattern):
    return unittest.TestSuite(StressRegressions(n) for n in StressRegressions.__dict__ if n.startswith('test_'))
