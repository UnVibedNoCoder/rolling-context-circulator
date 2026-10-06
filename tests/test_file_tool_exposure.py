"""Exercise installed Hermes discovery, default toolset export and dispatch set."""
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest

from rolling_context.cli import prepare, runtime_info, profile_home


class HermesFileExposureTests(unittest.TestCase):
    def test_generated_profile_exports_callable_tools_through_real_hermes_boundary(self):
        launcher = Path.home()/'.local/bin/hermes'
        if not launcher.is_file():
            self.skipTest('Installed Hermes runtime unavailable')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/'source'; source.mkdir()
            # Full default CLI selection, never a --toolsets override.
            (source/'config.yaml').write_text('platform_toolsets:\n  cli: [hermes-cli]\n')
            for name in ('installs', 'tools', 'runtime'):
                dependency = Path.home()/'.hermes'/name
                if dependency.exists(): (source/name).symlink_to(dependency, target_is_directory=True)
            path = root/'source.py'; path.write_text('CURRENT_MARKER = 731\n')
            args = SimpleNamespace(profile='large', state_dir=root/'state', source_home=source,
                refresh=False, copy_secrets=False, hermes=launcher)
            with contextlib.redirect_stdout(io.StringIO()): prepare(args)
            _, python, checkout = runtime_info(args)
            code = '''import sys, json, os
from types import SimpleNamespace
sys.path.insert(0, sys.argv[1])
os.chdir(os.path.dirname(sys.argv[2]))
import hermes_bootstrap
from plugins.context_engine import load_context_engine
from hermes_cli.config import load_config
from hermes_cli.tools_config import _get_platform_tools
from agent.agent_init import _inject_context_engine_tools
from model_tools import get_tool_definitions
engine = load_context_engine('local_rolling')
assert engine is not None, 'Generated profile shim was not discovered'
enabled = _get_platform_tools(load_config(), 'cli')
assert 'context_engine' in enabled, enabled
agent = SimpleNamespace(context_compressor=engine, enabled_toolsets=enabled,
    tools=get_tool_definitions(enabled_toolsets=list(enabled), quiet_mode=True),
    valid_tool_names=set(), session_id='exposure', platform='cli', model='qwen38-27b-atx-73k')
_inject_context_engine_tools(agent)
names = {s['function']['name'] for s in agent.tools}
required = {'rolling_file_snapshot', 'rolling_snapshot_read', 'rolling_raw_read'}
assert required <= names, names
assert required <= agent._context_engine_tool_names
snap = json.loads(engine.handle_tool_call('rolling_file_snapshot', {'path': sys.argv[2]}))
assert 'error' not in snap, snap
page = json.loads(engine.handle_tool_call('rolling_snapshot_read', {'snapshot_id': snap['snapshot_id']}))
raw = json.loads(engine.handle_tool_call('rolling_raw_read', {'raw_id': snap['raw_id']}))
assert 'CURRENT_MARKER = 731' in page['excerpt']['content']
assert raw['excerpt']['content'] == page['excerpt']['content']
# Real built-in file handler, real task path resolver, then native selection.
from tools.file_tools import read_file_tool
class Counter:
    def text(self, text): return len(text)
    def weights(self, messages): return [len(m.get('content') or '') for m in messages]
    def messages(self, messages): return sum(self.weights(messages))
engine.counter = Counter()
engine.pages.counter = engine.counter
engine._stop.set()
call = {'role': 'assistant', 'tool_calls': [{'id': 'ordinary', 'type': 'function',
    'function': {'name': 'read_file', 'arguments': json.dumps({'path': 'source.py'})}}]}
result = read_file_tool('source.py', task_id=agent.session_id)
messages = [{'role': 'user', 'content': 'Inspect current source'}, call,
    {'role': 'tool', 'tool_call_id': 'ordinary', 'content': result}]
selected = engine.select_context(messages, conversation_messages=messages)
value = json.loads(next(m['content'] for m in selected if m.get('tool_call_id') == 'ordinary'))
assert value['authority'] == 'current_disk_capture', value
assert 'CURRENT_MARKER = 731' in value['content']
assert value['read']['tool'] == 'rolling_snapshot_read'
print(json.dumps({'native_tools': sorted(required), 'total_tools': len(names), 'callable': True}))
'''
            env = dict(os.environ, HERMES_HOME=str(profile_home(args)), HERMES_DISABLE_LAZY_INSTALLS='1',
                       PYTHONDONTWRITEBYTECODE='1', TMPDIR=str(root))
            result = subprocess.run([python, '-I', '-c', code, str(checkout), str(path)],
                env=env, capture_output=True, text=True, timeout=90)
            self.assertEqual(result.returncode, 0, result.stderr[-5000:]+result.stdout[-2000:])
            data = json.loads(result.stdout.splitlines()[-1])
            self.assertTrue(data['callable']); self.assertGreater(data['total_tools'], 5)


if __name__ == '__main__': unittest.main()
