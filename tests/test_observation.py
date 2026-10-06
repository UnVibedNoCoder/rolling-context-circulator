import json
import time
import tempfile
from pathlib import Path
import unittest

from rolling_context.relay import SSEObserver, write_feedback


class ObservationTests(unittest.TestCase):
    def test_fragmented_unicode_stream_counts_actions_and_reasoning_without_logging_text(self):
        observer=SSEObserver(time.monotonic())
        items=[{'choices':[{'delta':{'reasoning_content':'private 🦙'}}]},
               {'choices':[{'delta':{'reasoning_content':' continues'}}]},
               {'choices':[{'delta':{'tool_calls':[{'id':'call'}]}}]},
               {'choices':[{'delta':{},'finish_reason':'tool_calls'}]}]
        raw=b''.join(('data: '+json.dumps(i,ensure_ascii=False)+'\n\n').encode() for i in items)+b'data: [DONE]\n\n'
        for byte in raw:observer.consume(bytes([byte]))
        observer.finish();metrics=observer.metrics()
        self.assertEqual(metrics['reasoning_characters'],len('private 🦙 continues'))
        self.assertEqual(metrics['reasoning_chunks'],2)
        self.assertEqual(metrics['longest_reasoning_only_streak'],2)
        self.assertEqual(metrics['tool_call_chunks'],1)
        self.assertIsNotNone(metrics['first_tool_call_ms'])
        self.assertEqual(metrics['finish_reasons'],['tool_calls'])
        self.assertTrue(observer.done)
        self.assertNotIn('private',json.dumps(metrics))

    def test_content_resets_reasoning_only_streak(self):
        observer=SSEObserver(time.monotonic())
        for delta in [{'reasoning_content':'a'},{'content':'b'},{'reasoning_content':'c'}]:
            observer.observe_json({'choices':[{'delta':delta}]})
        self.assertEqual(observer.longest_reasoning_only_streak,1)
        self.assertEqual(observer.content_characters,1)
        self.assertIsNotNone(observer.first_visible_content_ms)

    def test_feedback_requires_provenance_and_contains_only_private_metrics(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'fast-feedback.json'
            write_feedback(Path(root),'fast',{'input_tokens':123})
            self.assertFalse(path.exists())
            write_feedback(Path(root),'fast',{'session_id':'one','context_generation':3,
                'input_tokens':456,'messages':'PRIVATE','Authorization':'SECRET'})
            saved=path.read_bytes()
            self.assertEqual(json.loads(saved)['input_tokens'],456)
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            self.assertNotIn(b'PRIVATE',saved)
            self.assertNotIn(b'SECRET',saved)
            write_feedback(Path(root),'fast',{'context_generation':4,'input_tokens':999})
            self.assertEqual(path.read_bytes(),saved)
            self.assertEqual(list(Path(root).glob('*.tmp')),[])
