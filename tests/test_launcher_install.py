"""Installed-launcher provenance and the real installer/update mechanism."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from rolling_context.provenance import profile_provenance, SOURCE_ROOT


class LauncherInstallTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.installed=self.root/'.local/bin/hermes-circulator';self.installed.parent.mkdir(parents=True)
        self.home=self.root/'profiles/large';shim=self.home/'plugins/local_rolling/__init__.py';shim.parent.mkdir(parents=True)
        shim.write_text('import sys\nsys.path.insert(0, '+repr(str(SOURCE_ROOT))+')\n')
        self.marker={'source_root':str(SOURCE_ROOT)}
        self.config={'context':{'local_rolling':{'profile':'large'}}}

    def tearDown(self):self.temp.cleanup()

    def report(self):
        with patch('rolling_context.provenance.Path.home',return_value=self.root):
            return profile_provenance(self.home,self.marker,self.config)

    def install(self):
        folder=self.root/'installer';folder.mkdir(exist_ok=True)
        launcher=folder/'hermes-circulator'
        if not launcher.exists():launcher.symlink_to(SOURCE_ROOT/'hermes-circulator')
        # Change only the fixture destination, not HOME or the install logic.
        script=(SOURCE_ROOT/'install.sh').read_text()
        line='LAUNCHER_DEST="$HOME/.local/bin/hermes-circulator"'
        self.assertIn(line,script)
        script=script.replace(line,'LAUNCHER_DEST='+repr(str(self.installed)))
        installer=folder/'install.sh';installer.write_text(script)
        env=os.environ.copy();env.update(PYTHONDONTWRITEBYTECODE='1',HERMES_DISABLE_LAZY_INSTALLS='1')
        result=subprocess.run(['bash',str(installer),'--no-prepare'],capture_output=True,text=True,env=env,timeout=15)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def test_correct_installed_wrapper_passes_provenance(self):
        self.installed.symlink_to(SOURCE_ROOT/'hermes-circulator')
        report=self.report();self.assertTrue(report['installed_wrapper_matches_source'])
        self.assertEqual(report['provenance_status'],'ok');self.assertFalse(report['provenance_warnings'])

    def test_stale_wrapper_is_detected(self):
        stale=self.root/'stale-launcher';stale.touch();self.installed.symlink_to(stale)
        report=self.report();self.assertFalse(report['installed_wrapper_matches_source'])
        self.assertTrue(any('Installed wrapper resolves' in warning for warning in report['provenance_warnings']))

    def test_install_update_repoints_stale_wrapper_to_active_source(self):
        stale=self.root/'stale-launcher';stale.touch();self.installed.symlink_to(stale)
        self.install()
        self.assertEqual(self.installed.resolve(),SOURCE_ROOT/'hermes-circulator')
        self.assertTrue(self.report()['installed_wrapper_matches_source'])
        self.assertFalse(self.report()['provenance_warnings'])

    def test_repeated_install_preserves_runtime_source_identity(self):
        self.install();self.install()
        report=self.report();self.assertEqual(report['source_root'],str(SOURCE_ROOT))
        self.assertEqual(report['engine_defining_file'],str(SOURCE_ROOT/'rolling_context/engine.py'))
        result=subprocess.run([str(self.installed),'--help'],capture_output=True,text=True,timeout=10,
                              env={**os.environ,'PYTHONDONTWRITEBYTECODE':'1'})
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('doctor',result.stdout)
