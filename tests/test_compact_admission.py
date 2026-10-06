"""Regression: a ready compaction result must be admitted into the next
selection's warm prefix, not left unconsumed while its sources are archived.

Production symptom (Hermes stress test, job 86):
  compaction_ready_jobs: 1, compact_pages: 0, applied: false
  Source pages ARCHIVED while the compact replacement was never visible
  in selection. Context kept growing to saturation.
"""
import json, unittest
from test_engine import EngineTests


class CompactAdmissionTests(EngineTests):
    def setUp(self):
        super().setUp()
        self.engine.settings.update(
            selection_policy='coherent', target_tokens=1600, trigger_tokens=1800,
            tail_tokens=500, minimum_tail_tokens=200, recall_budget_tokens=500,
            warm_budget_tokens=500, segment_max_tokens=400,
            semantic_policy='cold_chunks', segmentation_policy='coherent',
            compact_source_aliases=True, summary_memory_max_tokens=2048,
            chunk_min=200, chunk_target=1000, chunk_max=2000)

    def _body(self):
        # Large enough to trigger cold-chunk scheduling (chunk_min=60).
        # The compacted content mentions E_LOCUS_419; the follow-up query
        # does NOT share any words with it.
        return ([{'role': 'system', 'content': 'Keep invariants'},
                 {'role': 'user', 'content': 'Implement src/genetics.py with E_LOCUS_419'},
                 {'role': 'assistant', 'content': 'Created src/genetics.py defining E_LOCUS_419'}]
                + [{'role': 'assistant', 'content': str(i) + 'x' * 180} for i in range(20)]
                + [{'role': 'user', 'content': 'Continue with the next step'}])

    def test_ready_compact_block_is_admitted_on_next_selection(self):
        body = self._body()
        self.select(body)

        # A worker must have been spawned by the first selection.
        self.assertIsNotNone(self.engine._worker,
                             "First selection must spawn a compaction worker")

        # Run the worker: produces a ready job, attaches COMPACT, archives cold.
        self.finish()

        with self.engine.store.connect() as db:
            status = db.execute("SELECT status FROM jobs ORDER BY id LIMIT 1").fetchone()[0]
            self.assertEqual(status, 'ready', "job must be ready after worker")
            compact = db.execute(
                "SELECT count(*) FROM page_representations WHERE kind='COMPACT'").fetchone()[0]
            self.assertGreater(compact, 0, "COMPACT representation must exist")
            archived = db.execute(
                "SELECT count(*) FROM pages WHERE state='ARCHIVED'").fetchone()[0]
            self.assertGreater(archived, 0, "cold sources must be archived")

        # The fixture counter counts characters: the actual summary is 238,
        # so the fixture allowance must fit it (production budgets are untouched).
        with self.engine.store.connect() as db:
            size = db.execute('SELECT tokens FROM summaries ORDER BY id LIMIT 1').fetchone()[0]
        self.assertLessEqual(size, self.engine.settings['warm_budget_tokens'])

        # Next selection with a query that does NOT lexically match the
        # compacted chunk. The compact block must still be admitted.
        body.append({'role': 'user', 'content': 'Please run the full test suite now'})
        second = self.select(body)

        self.assertIn('IMMUTABLE BLOCK', json.dumps(second),
                      "Ready compact block must be admitted into the next "
                      "selection's warm prefix, even when the current query "
                      "does not lexically match the compacted content")


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(
        CompactAdmissionTests(n) for n in CompactAdmissionTests.__dict__
        if n.startswith('test_'))
