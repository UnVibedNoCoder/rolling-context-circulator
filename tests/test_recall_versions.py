import json
import unittest

from test_engine import EngineTests
from rolling_context.common import digest, wire_message


class RecallVersionTests(EngineTests):
    def observations(self):
        old={'role':'assistant','content':'Decision: /srv/project/config.yaml uses port 8084. Error EADDRINUSE. Hash abcdef123456.'}
        new={'role':'user','content':'Correction: /srv/project/config.yaml now uses port 8082. This supersedes the previous decision. Hash fedcba654321.'}
        pages=self.engine.pages
        self.engine.store.snapshot('session-a','wire',[old,new])
        pages.ingest('session-a',[old,new],[100,100])
        return old,new,[digest(wire_message(m)) for m in [old,new]]

    def test_access_popularity_does_not_override_a_current_correction(self):
        old,new,ids=self.observations()
        for _ in range(8):self.engine.pages.state('session-a',[ids[0]],'RECALL')
        hits=self.engine.pages.recall('session-a','current port /srv/project/config.yaml',set(ids),2000)
        self.assertEqual(hits[0]['source_id'],ids[1])
        self.assertEqual(hits[0]['truth_status'],'quoted_observation')
        self.assertEqual(hits[0]['evidence_position'],1)

    def test_superseded_record_remains_exact_but_is_labelled_historical(self):
        old,new,ids=self.observations()
        self.engine.store.snapshot('session-a','wire',[new])
        self.engine.pages.ingest('session-a',[new],[100])
        hits=self.engine.pages.recall('session-a','exact old EADDRINUSE',set(ids),2000)
        self.assertEqual(hits[0]['source_id'],ids[0])
        self.assertEqual(hits[0]['source_status'],'historical_revision')
        self.assertEqual(self.engine.store.record('session-a',ids[0]),old)

    def test_ingest_invalidates_cached_recall_after_source_edit(self):
        old,new,ids=self.observations()
        first=self.engine.pages.recall('session-a','current config.yaml',set(ids),2000)
        self.assertEqual(first[0]['source_id'],ids[1])
        self.engine.pages.ingest('session-a',[new,old],[100,100])
        second=self.engine.pages.recall('session-a','current config.yaml',set(ids),2000)
        self.assertEqual(second[0]['source_id'],ids[0])

    def test_search_prioritizes_latest_evidence_and_exact_hash_still_reads_old(self):
        old,new,ids=self.observations()
        self.assertEqual(self.engine.pages.search_ids('session-a','config.yaml')[0],ids[1])
        self.assertEqual(self.engine.pages.search_ids('session-a',ids[0])[0],ids[0])


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(RecallVersionTests(name) for name in RecallVersionTests.__dict__ if name.startswith('test_'))
