"""Public replay tools require explicit inputs before creating outputs or probing models."""
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
HELPERS = {
    'astra_replay.py': [],
    'astra_ceiling_probe.py': ['--profile', 'large', '--target', '32000', '--label', 'synthetic-probe'],
}


class ReplayInputTests(unittest.TestCase):
    def invoke(self, helper, args):
        return subprocess.run(
            [sys.executable, '-I', '-B', str(ROOT / 'scripts' / helper), *args],
            text=True, capture_output=True, timeout=10,
            env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1', 'HERMES_DISABLE_LAZY_INSTALLS': '1'},
        )

    def test_missing_explicit_archive_or_session_fails_before_any_work(self):
        for helper, args in HELPERS.items():
            for extra, missing in [([], '--archive'), (['--archive', '/nonexistent/synthetic.sqlite3'], '--session')]:
                with self.subTest(helper=helper, missing=missing):
                    result = self.invoke(helper, args + extra)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn(missing, result.stderr)
                    self.assertNotIn('Traceback', result.stderr)

    def test_missing_archive_is_not_created(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'missing.sqlite3'
            for helper, args in HELPERS.items():
                with self.subTest(helper=helper):
                    result = self.invoke(helper, args + ['--archive', str(archive), '--session', 'synthetic-session'])
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn('existing offline SQLite snapshot', result.stderr)
                    self.assertFalse(archive.exists())

    def test_unknown_session_leaves_archive_unchanged_and_needs_no_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'synthetic-archive.sqlite3'
            with sqlite3.connect(archive) as db:
                db.execute('CREATE TABLE snapshots(id INTEGER, session TEXT, kind TEXT, hashes TEXT)')
                db.execute('INSERT INTO snapshots VALUES(1, ?, ?, ?)', ('other-synthetic-session', 'request', '[]'))
            before = archive.read_bytes()
            for helper, args in HELPERS.items():
                with self.subTest(helper=helper):
                    result = self.invoke(helper, args + ['--archive', str(archive), '--session', 'synthetic-session'])
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertIn('No request snapshots', result.stderr)
                    self.assertNotIn('Traceback', result.stderr)
                    self.assertEqual(archive.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
