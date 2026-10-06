"""Native current source retrieval, visibility, RAW identity and admission."""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path.home()/'.hermes/hermes-agent'))
from rolling_context.common import StateStore, digest, dumps, wire_message
from rolling_context.engine import RollingContextEngine
from rolling_context.file_reads import FileSnapshots
from rolling_context.admission import excerpt
from rolling_context.visibility import ReadVisibility
from test_continuity import Counter, coding_history


class FileReadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root/'mutable.py'
        self.path.write_bytes(b'first = 1\r\nneedle = 2\r\nlast = 3\r\n')
        self.engine = RollingContextEngine(settings={'state_dir': str(self.root/'state'),
            'file_workspace': str(self.root)}, counter=Counter())
        self.engine.on_session_start('files')
        self.engine._stop.set()
        (self.engine.store.root/'large-overhead.json').write_text(dumps({'overhead_tokens': 10845}))

    def tearDown(self):
        self.temp.cleanup()

    def tool(self, name, **args):
        return json.loads(self.engine.handle_tool_call(name, args))

    def snapshot(self):
        return self.tool('rolling_file_snapshot', path=str(self.path))

    def read_call(self, cid, path=None, offset=1, limit=2000):
        return {'role': 'assistant', 'tool_calls': [{'id': cid, 'type': 'function',
            'function': {'name': 'read_file', 'arguments': dumps({'path': path or str(self.path),
                'offset': offset, 'limit': limit})}}]}

    def select(self, messages):
        return self.engine.select_context(messages, conversation_messages=messages)

    def ordinary(self, cid='read', content=None, path=None, offset=1, limit=2000):
        messages = [{'role': 'user', 'content': 'Inspect current mutable source.'},
            self.read_call(cid, path, offset, limit), {'role': 'tool', 'tool_call_id': cid,
                'content': content or dumps({'content': self.path.read_bytes().decode(), 'truncated': False})}]
        before = copy.deepcopy(messages)
        selected = self.select(messages)
        self.assertEqual(before, messages)
        return json.loads(next(m['content'] for m in selected if m.get('tool_call_id') == cid))

    def test_native_tools_exported_and_callable(self):
        names = {s['function']['name'] for s in self.engine.get_tool_schemas()}
        self.assertTrue({'rolling_file_snapshot', 'rolling_snapshot_read', 'rolling_raw_read'} <= names)
        snap = self.snapshot()
        self.assertNotIn('error', self.tool('rolling_snapshot_read', snapshot_id=snap['snapshot_id']))
        self.assertNotIn('error', self.tool('rolling_raw_read', raw_id=snap['raw_id']))

    def test_current_never_substitutes_stale_archive_even_same_mtime_and_size(self):
        original = self.snapshot()
        import os
        previous = self.path.stat()
        self.path.write_bytes(self.path.read_bytes().replace(b'needle = 2', b'needle = 9'))
        os.utime(self.path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        current = self.snapshot()
        self.assertNotEqual(original['sha256'], current['sha256'])
        self.assertNotEqual(original['snapshot_id'], current['snapshot_id'])
        self.assertIn('needle = 9', current['excerpt']['content'])
        archived = self.tool('rolling_snapshot_read', snapshot_id=original['snapshot_id'])
        self.assertIn('needle = 2', archived['excerpt']['content'])
        self.assertFalse(archived['disk_rechecked'])

    def test_unchanged_reuses_snapshot_and_survives_restart(self):
        one, two = self.snapshot(), self.snapshot()
        self.assertEqual(one['snapshot_id'], two['snapshot_id'])
        self.assertEqual(two['fingerprint_status'], 'unchanged')
        restarted = FileSnapshots(StateStore(self.engine.store.root), 'files')
        self.assertEqual(restarted.read_snapshot({'snapshot_id': one['snapshot_id']})['excerpt']['content'], self.path.read_bytes().decode())
        other = FileSnapshots(self.engine.store, 'other')
        with self.assertRaises(ValueError): other.read_snapshot({'snapshot_id': one['snapshot_id']})

    def test_large_current_and_snapshot_reads_bounded_with_lossless_pagination(self):
        self.path.write_text('x'*20000+'\n'+'needle = 42\n'*2000)
        snap = self.snapshot()
        self.assertLessEqual(len(snap['excerpt']['content']), 2000)
        parts = []; offset = 0
        while True:
            page = self.tool('rolling_snapshot_read', snapshot_id=snap['snapshot_id'],
                start_line=1, end_line=snap['lines'], offset=offset, max_chars=12000)['excerpt']
            parts.append(page['content'])
            if page['next_offset'] is None: break
            offset = page['next_offset']
        self.assertEqual(''.join(parts).encode(), self.path.read_bytes())

    def test_range_and_literal_search_keep_crlf_and_coordinates(self):
        snap = self.snapshot()
        result = self.tool('rolling_snapshot_read', snapshot_id=snap['snapshot_id'], start_line=2, end_line=2)
        self.assertEqual(result['excerpt']['content'], 'needle = 2\r\n')
        result = self.tool('rolling_snapshot_read', snapshot_id=snap['snapshot_id'], query='needle')
        self.assertEqual(result['excerpt']['matches'][0]['line'], 2)
        self.assertEqual(result['excerpt']['matches'][0]['text'], 'needle = 2\r\n')

    def test_raw_uses_archive_after_file_deleted(self):
        data = self.path.read_bytes(); snap = self.snapshot(); self.path.unlink()
        result = self.tool('rolling_raw_read', raw_id=snap['raw_id'])
        self.assertEqual(result['excerpt']['content'].encode(), data)
        self.assertEqual(result['authority'], 'archived_raw')
        self.assertIn('error', self.snapshot())

    def test_empty_search_and_long_line_search_are_bounded(self):
        self.path.write_text('')
        snap = self.snapshot()
        self.assertEqual(self.tool('rolling_snapshot_read', snapshot_id=snap['snapshot_id'], query='x')['excerpt']['matches'], [])
        self.path.write_text('a'*20000+'NEEDLE'+'b'*20000)
        snap = self.snapshot()
        result = self.tool('rolling_snapshot_read', snapshot_id=snap['snapshot_id'], query='NEEDLE', max_chars=1000)
        match = result['excerpt']['matches'][0]
        self.assertIn('NEEDLE', match['text']); self.assertTrue(match['partial_line'])
        self.assertLessEqual(len(match['text']), 1000)

    def test_snapshot_identity_survives_admission(self):
        self.path.write_text('def useful_symbol():\n    pass\n'*1200)
        snap = self.snapshot()
        message = {'role': 'tool', 'tool_call_id': 'x', 'content': dumps(snap)}
        self.engine.store.snapshot('files', 'test', [message])
        reduced = excerpt(self.engine, message, digest(message), 1800, 'useful_symbol')
        self.assertLessEqual(len(reduced['content']), 1800)
        self.assertIn(snap['snapshot_id'], reduced['content'])
        self.assertIn('rolling_snapshot_read', reduced['content'])
        self.assertEqual(self.engine.store.record('files', digest(message)), message)

    def test_small_ordinary_read_is_usable_current_source(self):
        result = self.ordinary()
        self.assertEqual(result['content'].encode(), self.path.read_bytes())
        self.assertEqual(result['authority'], 'current_disk_capture')
        self.assertFalse(result['truncated'])
        self.assertEqual(result['read']['tool'], 'rolling_snapshot_read')

    def test_relative_ordinary_source_uses_workspace(self):
        result = self.ordinary(path='mutable.py')
        self.assertEqual(result['path'], str(self.path))
        self.assertTrue(result['disk_rechecked'])

    def test_stale_dedup_result_is_upgraded_to_current_disk(self):
        self.ordinary()
        self.path.write_text('FRESH_SOURCE = 731\n')
        result = self.ordinary('dedup', dumps({'status': 'unchanged', 'dedup': True, 'content_returned': False}))
        self.assertIn('FRESH_SOURCE', result['content'])
        self.assertEqual(result['authority'], 'current_disk_capture')

    def test_unknown_disk_is_explicitly_archived_not_current(self):
        value = dumps({'content': 'old evidence', 'truncated': False})
        result = self.ordinary(content=value, path=str(self.root/'missing.py'))
        self.assertEqual(result['authority'], 'archived_raw')
        self.assertFalse(result['disk_rechecked'])
        self.assertEqual(result['content'], 'old evidence')

    def test_read_projection_stable_until_new_tool_call(self):
        call = self.read_call('one'); result = {'role': 'tool', 'tool_call_id': 'one', 'content': dumps({'content': 'initial'})}
        messages = [{'role': 'user', 'content': 'Read source'}, call, result]
        first = self.select(messages)
        self.path.write_text('changed\n')
        self.assertEqual(self.select(messages), first)
        current = self.ordinary('two')
        self.assertEqual(current['content'], 'changed\n')

    def test_selector_failure_recovers_without_fail_open(self):
        messages = coding_history()+[self.read_call('fault'), {'role': 'tool',
            'tool_call_id': 'fault', 'content': dumps({'content': self.path.read_text()})}]
        with patch('rolling_context.coherent.select_coherent', side_effect=ValueError('damaged frame')):
            selected = self.select(messages)
        self.assertIsNotNone(selected)
        self.assertLessEqual(self.engine.counter.messages(selected)+10845, 64512)
        self.assertNotEqual(selected, messages)

    def test_file_read_hint_enters_normal_selection(self):
        messages = [{'role': 'user', 'content': 'Read source'}, self.read_call('hint'),
            {'role': 'tool', 'tool_call_id': 'hint', 'content': dumps({'content': 'source'})}]
        selected = self.select(messages)
        self.assertTrue(any(m.get('role') == 'system' and 'FILE SOURCE RETRIEVAL' in m.get('content', '') for m in selected))

    def test_large_read_with_overhead_long_history_and_auxiliary_stays_admissible(self):
        self.path.write_text('ACTIVE_SOURCE = 731\n'*15000)
        messages = coding_history()+[self.read_call('large'), {'role': 'tool', 'tool_call_id': 'large',
            'content': dumps({'content': self.path.read_text(), 'truncated': False})}]
        original = copy.deepcopy(messages)
        from rolling_context.relay import write_overhead
        write_overhead(self.engine.store.root, 'large', 10845, session_id='files', has_tools=True, request_class='tool_bearing', schema_fingerprint='full')
        write_overhead(self.engine.store.root, 'large', 0, request_class='no_tools', has_tools=False)
        selected = self.select(messages)
        self.assertLessEqual(self.engine.counter.messages(selected)+10845, 64512)
        self.assertEqual(messages, original)
        value = json.loads(next(m['content'] for m in selected if m.get('tool_call_id') == 'large'))
        self.assertIn('ACTIVE_SOURCE', value['content'])
        self.assertLessEqual(len(value['content']), 6000)
        self.assertEqual(self.tool('rolling_raw_read', raw_id=value['raw_id'], start_line=15000)['excerpt']['content'], 'ACTIVE_SOURCE = 731\n')


if __name__ == '__main__': unittest.main()
