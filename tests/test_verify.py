"""Exercise the shipped shell verifier's exit status and actual invocation count."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]


class VerifyTests(unittest.TestCase):
    def fixture(self, root, failing=False):
        shutil.copy2(ROOT/'verify.sh',root/'verify.sh')
        (root/'hermes-circulator').write_text('#!/usr/bin/python3\n')
        (root/'hermes-circulator').chmod(0o755)
        package=root/'rolling_context';package.mkdir()
        for name in ['__init__','common','pages','engine','relay','cli']:(package/f'{name}.py').write_text('')
        tests=root/'tests';tests.mkdir()
        (tests/'test_fixture.py').write_text(
            'import unittest\nfrom pathlib import Path\nclass Check(unittest.TestCase):\n'
            ' def test_result(self):\n'
            '  with Path("invocations.txt").open("a") as handle: handle.write("run\\n")\n'
            f'  self.assertTrue({not failing!r})\n')
        files={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}
        (root/'install-manifest.json').write_text(json.dumps({'files':files}))

    def test_failed_suite_returns_failure_and_runs_once(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);self.fixture(root,failing=True)
            result=subprocess.run(['bash',str(root/'verify.sh')],capture_output=True,text=True,timeout=20)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('FAIL  test suite failed',result.stdout)
            self.assertEqual((root/'invocations.txt').read_text(),'run\n')

    def test_passing_suite_runs_once(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);self.fixture(root)
            result=subprocess.run(['bash',str(root/'verify.sh')],capture_output=True,text=True,timeout=20)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertEqual((root/'invocations.txt').read_text(),'run\n')

    def test_manifest_rejects_a_changed_file(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);self.fixture(root)
            (root/'rolling_context/common.py').write_text('# changed\n')
            result=subprocess.run(['bash',str(root/'verify.sh')],capture_output=True,text=True,timeout=20)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('file hash mismatch',result.stderr)


if __name__=='__main__':unittest.main()
