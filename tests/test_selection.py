import copy
import json
import unittest

from test_engine import EngineTests


class StableSelectionTests(EngineTests):
    def setUp(self):
        super().setUp()
        self.engine.settings.update(selection_policy='stable', target_tokens=1500,
                                    eviction_batch_tokens=300, recall_budget_tokens=0,
                                    chunk_min=10000, chunk_target=11000, chunk_max=12000)
        # This legacy policy's target includes overhead. Keep its original
        # 1490-token message allowance with the new conservative cold start.
        self.engine.settings['target_tokens'] += self.engine._reserve()-10

    def history(self):
        return [{'role':'system','content':'Keep the system fixed'},
                {'role':'user','content':'Original objective: finish all project modules.'}] + [
                {'role':'assistant','content':f'record {i}: '+('x'*180)} for i in range(20)] + [
                {'role':'user','content':'Continue without restarting.'}]

    def test_original_objective_survives_a_later_continuation(self):
        messages=self.history();selected=self.select(messages)
        self.assertIn(messages[1],selected)
        self.assertIn(messages[-1],selected)
        self.assertLess(self.engine.counter.messages(selected),1500)

    def test_append_preserves_existing_prefix_until_eviction_boundary(self):
        messages=self.history();first=self.select(messages);frozen=copy.deepcopy(first)
        more=messages+[{'role':'assistant','content':'new small observation'}]
        second=self.select(more)
        self.assertEqual(second[:len(first)],first)
        self.assertEqual(first,frozen)
        self.assertEqual(second[-1],more[-1])
        self.assertEqual(second,self.select(more))

    def test_edited_source_invalidates_frame_without_mutating_prior_snapshot(self):
        messages=self.history();first=self.select(messages);frozen=copy.deepcopy(first)
        changed=copy.deepcopy(messages);changed[-3]['content']='changed exact evidence'
        second=self.select(changed)
        self.assertEqual(first,frozen)
        self.assertIn(changed[-3],second)

    def test_current_large_tool_result_is_kept_exact_and_group_stays_atomic(self):
        messages=self.history()+[{'role':'assistant','content':'','tool_calls':[{'id':'one'}]},
                                 {'role':'tool','tool_call_id':'one','content':'current exact result '*100}]
        selected=self.select(messages)
        self.assertEqual(selected[-1],messages[-1])
        self.assertIn(messages[-2],selected)

    def test_growing_history_stays_bounded_without_waiting_for_a_summary(self):
        messages=self.history()
        for i in range(10):
            messages.append({'role':'assistant','content':f'next {i}: '+'y'*180})
            selected=self.select(messages)
            self.assertLess(self.engine.counter.messages(selected),1500)
            self.assertEqual(selected[-1],messages[-1])
        self.assertEqual(self.engine.store.record('session-a',self.engine._stable_frame['seen'][0]),messages[1])

    def test_large_objective_does_not_template_an_isolated_assistant_excerpt(self):
        messages=self.history();messages[1]['content']='original exact objective '*120
        self.engine.settings['global_budget_tokens']=300
        original=self.engine.counter.messages
        def count(value):
            if value and all(m['role']=='assistant' for m in value):
                raise ValueError('Qwen rejects isolated assistant message')
            return original(value)
        self.engine.counter.messages=count
        selected=self.select(messages)
        self.assertLess(original(selected),1500)
        self.assertEqual(selected[-1],messages[-1])
        self.assertEqual(self.engine.store.record('session-a',self.engine._stable_frame['seen'][0]),messages[1])


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(StableSelectionTests(name) for name in StableSelectionTests.__dict__ if name.startswith('test_'))
