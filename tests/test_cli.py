import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import subprocess
import json
import os
import signal
import tempfile
import textwrap
import time
import types
import unittest
from unittest.mock import patch
from rolling_context.cli import check_servers,PrototypeError,MARKER

ROOT = Path(__file__).resolve().parents[1]

class OptionalCompactorTests(unittest.TestCase):
    def test_offline_compactor_never_blocks_a_valid_main_profile(self):
        def get(url):
            if ':8083' in url:raise OSError('offline')
            if url.endswith('/health'):return {'status':'ok'}
            if url.endswith('/props'):return {'default_generation_settings':{'n_ctx':73728}}
            return {'data':[{'id':'qwen38-27b-atx-73k'}]}
        with patch('rolling_context.cli.get_json',side_effect=get):
            report=check_servers(types.SimpleNamespace(profile='large'))
        self.assertEqual(report[0]['context_length'],73728)
        self.assertFalse(report[1]['available'])
    def test_invalid_main_profile_is_rejected_without_process_management(self):
        with patch('rolling_context.cli.get_json',side_effect=OSError('offline')):
            with self.assertRaises(PrototypeError):check_servers(types.SimpleNamespace(profile='fast'))


class IsolatedRuntimeImportTests(unittest.TestCase):
    """Reproduce the isolated Hermes runtime: no PyYAML installed.

    The background compactor worker imports the engine, and the engine's
    verify_compactor() must not pull in CLI-only code (cli.py) that requires
    yaml. This runs a subprocess that blocks `import yaml` so the failure mode
    of the real isolated runtime is reproduced, not just the dev shell.
    """
    def test_engine_and_worker_import_without_yaml(self):
        root = Path(__file__).resolve().parents[1]
        hermes = Path.home()/".hermes/hermes-agent"
        code = (
            "import sys, types\n"
            f"sys.path.insert(0, {str(hermes)!r})\n"
            f"sys.path.insert(0, {str(root)!r})\n"
            "sys.modules['yaml'] = None  # force ModuleNotFoundError on import yaml\n"
            "import rolling_context.engine as engine\n"
            "import inspect\n"
            "src = inspect.getsource(engine.verify_compactor)\n"
            "assert 'from .common import compactor_process_facts' in src, 'engine still imports cli'\n"
            "from rolling_context.common import compactor_process_facts, option_value\n"
            "import rolling_context.cli as cli\n"
            "assert cli.compactor_process_facts is compactor_process_facts\n"
            "assert cli.option_value is option_value\n"
            "print('OK')\n"
        )
        result = subprocess.run([sys.executable, "-c", code],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0,
                         f"import failed without yaml:\n{result.stdout}\n{result.stderr}")
        self.assertIn("OK", result.stdout)


class LauncherInterpreterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        launcher = Path.home()/".local/bin/hermes"
        if not launcher.is_file():
            raise unittest.SkipTest("Installed Hermes launcher is unavailable")
        result = subprocess.run([str(launcher), "--print-runtime-command"],
                                capture_output=True, text=True, timeout=30, check=True)
        cls.isolated_python = json.loads(result.stdout)[0]
        result = subprocess.run([cls.isolated_python, "-I", "-c",
                                 "import importlib.util; print(importlib.util.find_spec('yaml'))"],
                                capture_output=True, text=True, timeout=10, check=True)
        if result.stdout.strip() != "None":
            raise unittest.SkipTest("Hermes runtime already has PyYAML")

    def profile(self, directory):
        state = Path(directory)/"state"
        home = state/"profiles/large"
        home.mkdir(parents=True)
        (home/"config.yaml").write_text("context:\n  engine: local_rolling\n")
        (home/MARKER).write_text(json.dumps({"prototype": "local-rolling-context",
                                             "profile": "large", "enabled": True}))
        return state

    def test_executable_status_uses_system_python_despite_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = self.profile(temporary)
            directory = Path(temporary)/"bin"
            directory.mkdir()
            (directory/"python3").symlink_to(self.isolated_python)
            env = dict(os.environ, PATH=str(directory))
            result = subprocess.run([str(ROOT/"hermes-circulator"), "status", "--profile", "large",
                                     "--state-dir", str(state)], env=env,
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["context_engine"], "local_rolling")

    def test_explicit_no_yaml_interpreter_fails_with_actionable_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = self.profile(temporary)
            result = subprocess.run([self.isolated_python, "-I", str(ROOT/"hermes-circulator"),
                                     "status", "--profile", "large", "--state-dir", str(state)],
                                    capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 1)
            self.assertIn("PyYAML is unavailable", result.stderr)
            self.assertIn("Run ~/.local/bin/hermes-circulator directly", result.stderr)
            self.assertNotIn("Traceback", result.stderr)


class OwnedProcessShutdownTests(unittest.TestCase):
    """Exercise actual signals and child reaping with no model/relay port use."""
    def wait_for(self, predicate, process, message):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if predicate():
                return
            if process.poll() is not None:
                self.fail(f"Fixture exited before {message}: {process.communicate()}")
            time.sleep(0.02)
        self.fail(f"Timed out waiting for {message}")

    def shutdown_fixture(self, during_startup):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            child = directory/"child.py"
            child.write_text(textwrap.dedent("""\
                import os, sys, time
                from pathlib import Path
                Path(sys.argv[1]).write_text(str(os.getpid()))
                while True:
                    time.sleep(1)
                """))
            helper = directory/"wrapper.py"
            helper.write_text(textwrap.dedent("""\
                import os, signal, subprocess, sys
                from pathlib import Path
                from types import SimpleNamespace
                sys.path.insert(0, sys.argv[1])
                from rolling_context import cli
                directory = Path(sys.argv[2])
                home = directory/'profile'
                home.mkdir()
                cli.require_profile = lambda args: (home, {'enabled': True})
                cli.read_yaml = lambda path: {'context': {'engine': 'local_rolling'},
                                              'compression': {'enabled': False}}
                from rolling_context.provenance import source_provenance
                provenance = dict(source_provenance({'profile':'large'}), provenance_errors=[])
                cli.profile_provenance = lambda *args: provenance
                def check_plugin(args):
                    args._provenance = provenance
                    return ('fixture-hermes', None, None)
                cli.check_plugin = check_plugin
                cli.check_servers = lambda args: []
                class Probe:
                    def __enter__(self): return self
                    def __exit__(self, *args): pass
                    def connect_ex(self, address): return 111
                cli.socket.socket = Probe
                cli.get_json = lambda *args, **kwargs: {
                    'profile': 'large' if (directory/'ready').exists() else 'starting',
                    'upstream_port': 8084, **provenance,
                }
                actual_popen = subprocess.Popen
                def fake_popen(argv, **kwargs):
                    kind = 'relay' if 'rolling_context.relay' in argv else 'hermes'
                    return actual_popen([sys.executable, str(directory/'child.py'),
                                         str(directory/(kind+'.pid'))], **kwargs)
                cli.subprocess.Popen = fake_popen
                args = SimpleNamespace(profile='large', state_dir=directory/'state',
                                       hermes_args=[], hermes=Path('fixture-hermes'))
                raise SystemExit(cli.run(args))
                """))
            if not during_startup:
                (directory/"ready").touch()
            process = subprocess.Popen([sys.executable, str(helper), str(ROOT), str(directory)],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                       start_new_session=True)
            child_pids = []
            try:
                relay_pid = directory/"relay.pid"
                self.wait_for(lambda: relay_pid.exists() and relay_pid.read_text().strip(),
                              process, "relay startup")
                child_pids.append(int(relay_pid.read_text()))
                hermes_pid = directory/"hermes.pid"
                if not during_startup:
                    self.wait_for(lambda: hermes_pid.exists() and hermes_pid.read_text().strip(),
                                  process, "Hermes startup")
                    child_pids.append(int(hermes_pid.read_text()))
                process.send_signal(signal.SIGHUP)
                stdout, stderr = process.communicate(timeout=8)
                self.assertEqual(process.returncode, 128 + signal.SIGHUP, stderr)
                if during_startup:
                    self.assertFalse(hermes_pid.exists(), stdout)
                for pid in child_pids:
                    with self.assertRaises(ProcessLookupError):
                        os.kill(pid, 0)
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=6)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                # A regression may strand a fixture child; target only its
                # recorded PID, keeping this test isolated from real services.
                for path in (directory/"relay.pid", directory/"hermes.pid"):
                    if path.exists() and path.read_text().strip():
                        try:
                            pid = int(path.read_text())
                            argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
                            if os.fsencode(child) in argv:
                                os.kill(pid, signal.SIGKILL)
                        except (OSError, ValueError):
                            pass

    def test_sighup_stops_hermes_and_owned_relay(self):
        self.shutdown_fixture(during_startup=False)

    def test_sighup_during_relay_readiness_cleans_owned_relay(self):
        self.shutdown_fixture(during_startup=True)


if __name__=='__main__':unittest.main()
