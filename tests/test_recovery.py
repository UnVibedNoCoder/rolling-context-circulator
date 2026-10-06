"""Corrupt optional semantic records must never break durable RAW admission."""
import json
import unittest

from test_engine import EngineTests
from rolling_context.common import digest, wire_message


class RecoveryTests(EngineTests):
    # Reuse fixtures, not inherited test cases (the load_tests hook below selects
    # only the focused methods defined on this class).
    def test_corrupt_summary_text_is_ignored_without_losing_raw(self):
        self.select();self.finish()
        with self.engine.store.connect() as db:
            db.execute("UPDATE summaries SET text='{incomplete'")
        output=self.select()
        self.assertNotIn('IMMUTABLE BLOCK',json.dumps(output))
        self.assertEqual(output[-2:],self.messages[-2:])
        self.assertEqual(self.engine.store.record('session-a',digest(wire_message(self.messages[1]))),self.messages[1])

    def test_corrupt_coverage_does_not_fail_open_to_full_history(self):
        self.select();self.finish()
        with self.engine.store.connect() as db:db.execute("UPDATE summaries SET covered='not-json'")
        output=self.select()
        self.assertLess(len(output),len(self.messages))
        self.assertEqual(output[-2:],self.messages[-2:])

    def test_summary_parent_cycle_is_rejected(self):
        self.select();self.finish()
        with self.engine.store.connect() as db:
            db.execute('UPDATE summaries SET parent_id=id')
            row=dict(db.execute('SELECT * FROM summaries').fetchone())
        self.assertEqual(self.engine.store.summary_chain(row),[])
        self.assertEqual(self.select()[-2:],self.messages[-2:])

    def test_summary_parent_from_another_session_is_rejected(self):
        self.select();self.finish()
        with self.engine.store.connect() as db:
            original=dict(db.execute('SELECT * FROM summaries').fetchone())
            foreign=db.execute("INSERT INTO summaries(session,created,covered,text,tokens,job_id) VALUES ('other',0,'[]','{}',1,1)").lastrowid
            db.execute('UPDATE summaries SET parent_id=? WHERE id=?',(foreign,original['id']))
            row=dict(db.execute('SELECT * FROM summaries WHERE id=?',(original['id'],)).fetchone())
        self.assertEqual(self.engine.store.summary_chain(row),[])


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(RecoveryTests(name) for name in RecoveryTests.__dict__ if name.startswith('test_'))
