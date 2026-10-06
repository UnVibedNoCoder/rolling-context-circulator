"""Source attribution, policy identity, fail-safe launch and read-only discovery."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path.home()/'.hermes/hermes-agent'))
from rolling_context import cli
from rolling_context.provenance import (SOURCE_ROOT, effective_settings, profile_provenance,
                                        source_fingerprint, source_provenance)

class Counter:
    def text(self, text): return len(text)



class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.args = SimpleNamespace(profile='large', state_dir=self.root/'state',
                                    hermes=Path.home()/'.local/bin/hermes', hermes_args=[])
        self.home = cli.profile_home(self.args)
        self.home.mkdir(parents=True)
        # Reuse installed dependency environments, exactly as prepare does.
        for name in ('installs','tools','runtime'):
            dependency = Path.home()/'.hermes'/name
            if dependency.exists():
                (self.args.state_dir/name).symlink_to(dependency, target_is_directory=True)
        self.config = {'context': {'engine':'local_rolling', 'local_rolling': {
            'profile':'large', 'state_dir':str(self.root/'durable'), 'target_tokens':28000,
            'tail_tokens':12000, 'generation_reserve_tokens':8192}}, 'compression':{'enabled':False}}
        self.marker = {'prototype':'local-rolling-context', 'profile':'large',
                       'source_root':str(SOURCE_ROOT), 'enabled':True}
        (self.home/cli.MARKER).write_text(json.dumps(self.marker))
        (self.home/'config.yaml').write_text(cli.yaml_module().safe_dump(self.config))
        self.shim = self.home/'plugins/local_rolling/__init__.py'
        self.shim.parent.mkdir(parents=True)
        self.write_shim(SOURCE_ROOT)

    def tearDown(self):
        self.temp.cleanup()

    def write_shim(self, source):
        self.shim.write_text(f"import sys\nSOURCE_ROOT = {str(source)!r}\n"
            "if SOURCE_ROOT not in sys.path:\n    sys.path.insert(0, SOURCE_ROOT)\n"
            "from rolling_context.engine import RollingContextEngine as _Engine\n"
            "class RollingContextEngine(_Engine):\n    pass\n")

    def report(self):
        return profile_provenance(self.home, self.marker, self.config)

    def test_explicit_settings_and_effective_defaults_both_visible(self):
        report = self.report()
        self.assertEqual(report['provenance_status'], 'ok')
        self.assertEqual(report['profile_shim_source_root'], str(SOURCE_ROOT))
        self.assertEqual(report['explicit_settings'], self.config['context']['local_rolling'])
        self.assertEqual(report['effective_settings']['target_tokens'], 28000)
        self.assertEqual(report['effective_settings']['summary_max_tokens'], 1536)
        self.assertEqual((report['physical_context'], report['generation_reserve_tokens'], report['safety_margin_tokens']), (73728,8192,1024))
        self.assertEqual(report['engine_defining_file'], str(SOURCE_ROOT/'rolling_context/engine.py'))
        self.assertEqual(report['relay_source_file'], str(SOURCE_ROOT/'rolling_context/relay.py'))

    def test_missing_shim_is_reported_and_blocks_doctor_and_launch_before_discovery(self):
        self.shim.unlink()
        self.assertEqual(self.report()['provenance_status'], 'error')
        with patch('rolling_context.cli.check_plugin') as discovery, patch('rolling_context.cli.check_servers') as servers, patch('rolling_context.cli.subprocess.Popen') as popen:
            for action in (cli.doctor, cli.run):
                with self.assertRaisesRegex(cli.PrototypeError, 'Cannot verify profile shim'):
                    action(self.args)
            discovery.assert_not_called(); servers.assert_not_called(); popen.assert_not_called()

    def test_missing_and_different_source_shims_fail_without_repairing_profiles(self):
        alternate = self.root/'alternate'
        for exists in (False, True):
            with self.subTest(exists=exists):
                if exists:
                    (alternate/'rolling_context').mkdir(parents=True)
                    (alternate/'rolling_context/engine.py').write_text('# a different source')
                self.write_shim(alternate)
                before = self.shim.read_bytes()
                report = self.report()
                self.assertIn('mismatch' if exists else 'missing', ' '.join(report['provenance_errors']))
                with patch('rolling_context.cli.subprocess.Popen') as popen:
                    with self.assertRaises(cli.PrototypeError): cli.run(self.args)
                    popen.assert_not_called()
                self.assertEqual(self.shim.read_bytes(), before)

    def test_resolved_alias_of_same_tree_is_valid_and_stale_marker_is_visible(self):
        alias = self.root/'alias'; alias.symlink_to(SOURCE_ROOT, target_is_directory=True)
        self.write_shim(alias)
        self.marker['source_root'] = str(self.root/'retired')
        report = self.report()
        self.assertEqual(report['provenance_errors'], [])
        self.assertEqual(report['profile_shim_source_root'], str(SOURCE_ROOT))
        self.assertTrue(any('creation marker' in w for w in report['provenance_warnings']))

    def test_policy_fingerprint_changes_with_settings_but_not_state_location(self):
        settings = effective_settings(self.config['context']['local_rolling'])
        a = source_provenance(settings)
        b = source_provenance({**settings,'target_tokens':32000})
        c = source_provenance({**settings,'state_dir':'/another/state'})
        self.assertNotEqual(a['policy_fingerprint'], b['policy_fingerprint'])
        self.assertEqual(a['policy_fingerprint'], c['policy_fingerprint'])
        self.assertEqual(a['source_fingerprint'], b['source_fingerprint'])

    def test_source_fingerprint_tracks_source_bytes_not_reports_or_relocation(self):
        for name in ('one','two'):
            root = self.root/name; (root/'rolling_context').mkdir(parents=True)
            (root/'rolling_context/engine.py').write_text('engine v1')
            (root/'hermes-circulator').write_text('wrapper v1')
        one, two = self.root/'one', self.root/'two'
        self.assertEqual(source_fingerprint(one), source_fingerprint(two))
        (one/'report.txt').write_text('unrelated')
        self.assertEqual(source_fingerprint(one), source_fingerprint(two))
        (one/'rolling_context/engine.py').write_text('engine v2')
        self.assertNotEqual(source_fingerprint(one), source_fingerprint(two))

    def test_real_hermes_discovery_reports_base_file_and_uses_temporary_state(self):
        if not self.args.hermes.is_file(): self.skipTest('Installed Hermes unavailable')
        before = {str(p.relative_to(self.root)):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        cli.check_plugin(self.args)
        self.assertFalse((self.root/'durable').exists())
        after = {str(p.relative_to(self.root)):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(self.args._provenance['runtime_engine_defining_file'], str(SOURCE_ROOT/'rolling_context/engine.py'))
        self.assertEqual(self.args._provenance['discovered_profile_shim'], str(self.shim))

    def test_runtime_defining_file_mismatch_is_rejected(self):
        expected = self.report()
        verified = {**expected, 'engine_defining_file':'/wrong/engine.py', 'discovered_profile_shim':str(self.shim)}
        runtime = (str(self.args.hermes), '/usr/bin/python3', Path.home()/'.hermes/hermes-agent')
        fake = SimpleNamespace(returncode=0, stdout=json.dumps(verified), stderr='')
        with patch('rolling_context.cli.runtime_info', return_value=runtime), patch('rolling_context.cli.subprocess.run', return_value=fake):
            with self.assertRaisesRegex(cli.PrototypeError, 'Runtime engine_defining_file mismatch'):
                cli.check_plugin(self.args)

    def test_session_start_records_source_policy_settings_without_model_requests(self):
        from rolling_context.engine import RollingContextEngine
        engine = RollingContextEngine(settings=self.config['context']['local_rolling'], counter=Counter())
        engine.on_session_start('provenance')
        record = json.loads((engine.store.root/'telemetry.jsonl').read_text().splitlines()[-1])
        self.assertEqual(record['event'], 'session_start')
        for key in ('engine_defining_file','relay_source_file','source_fingerprint','policy_fingerprint','effective_settings','explicit_settings','physical_context','generation_reserve_tokens','safety_margin_tokens'):
            self.assertEqual(record[key], source_provenance(engine.settings, engine.explicit_settings)[key])
        self.assertFalse(engine.should_compress())

    def test_doctor_reports_sources_even_when_main_server_is_unavailable(self):
        def discovery(args):
            args._provenance = self.report()
            return ('fixture-hermes', '/fixture/python', Path('/fixture/hermes'))
        output = io.StringIO()
        with patch('rolling_context.cli.check_plugin', side_effect=discovery), patch('rolling_context.cli.check_servers', side_effect=cli.PrototypeError('offline')), contextlib.redirect_stdout(output):
            with self.assertRaisesRegex(cli.PrototypeError, 'offline'): cli.doctor(self.args)
        report = json.loads(output.getvalue())
        self.assertEqual(report['engine_defining_file'], str(SOURCE_ROOT/'rolling_context/engine.py'))
        self.assertEqual(report['server_check_error'], 'offline')

    def test_relay_health_and_startup_policy_use_verified_launch_settings(self):
        import os
        import threading
        import urllib.request
        from rolling_context.relay import RelayServer
        report = self.report()
        with patch.dict(os.environ, {'ROLLING_CONTEXT_LAUNCH_PROVENANCE':json.dumps(report)}):
            server = RelayServer('large', self.root/'relay', port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(f'http://127.0.0.1:{server.server_port}/rolling-context/health', timeout=3) as response:
                health = json.load(response)
            for key in ('engine_defining_file','relay_source_file','source_fingerprint','policy_fingerprint','effective_settings','explicit_settings','physical_context','generation_reserve_tokens','safety_margin_tokens'):
                self.assertEqual(health[key], report[key])
        finally:
            server.shutdown(); thread.join(); server.server_close()

    def test_relay_rejects_source_or_policy_mismatch_before_binding(self):
        import os
        from rolling_context.relay import RelayServer
        for key in ('relay_source_file','source_fingerprint','policy_fingerprint'):
            with self.subTest(key=key):
                report = self.report(); report[key] = 'mismatch'
                with patch.dict(os.environ, {'ROLLING_CONTEXT_LAUNCH_PROVENANCE':json.dumps(report)}), patch('rolling_context.relay.ThreadingHTTPServer.__init__') as bind:
                    with self.assertRaisesRegex(ValueError, 'provenance mismatch'):
                        RelayServer('large', self.root/'relay', port=0)
                    bind.assert_not_called()


if __name__ == '__main__': unittest.main()
