import copy
import json
import unittest
from rolling_context.governor import GenerationGovernor, validated_policy, retry_body, safe_metadata

PRIVATE='PRIVATE_REASONING_MUST_NEVER_PERSIST'

class GovernorTests(unittest.TestCase):
    def make(self, **kwargs):
        self.now=0
        return GenerationGovernor({'enabled':True, **kwargs}, clock=lambda:self.now)
    def feed(self, g, delta=None, count=4000, finish=None):
        g.consume({'choices':[{'delta':delta or {'reasoning_content':PRIVATE},'finish_reason':finish}],
                   'timings':{'predicted_n':count}})
    def test_numeric_threshold_no_text_or_chunk_token_estimation(self):
        g=self.make();self.feed(g,count=2816)
        self.assertTrue(g.soft());self.assertFalse(g.soft());self.assertIsNone(g.verdict())
        self.feed(g,count=3840);self.assertEqual(g.verdict(),'hard_no_action_tokens')
        self.assertNotIn(PRIVATE,json.dumps(g.__dict__,default=str))
    def test_long_productive_generation(self):
        g=self.make();self.feed(g,{'content':'visible code '*5000},8192,'length')
        self.assertIsNone(g.verdict())
    def test_visible_and_tool_emergence_suppress_hard_trip(self):
        for delta in [{'content':'OK'}, {'tool_calls':[{'index':0,'function':{'arguments':'{'}}]}, {'function_call':{'arguments':'{'}}]:
            g=self.make();self.feed(g,delta);self.assertIsNone(g.verdict())
    def test_near_zero_visible_length_trips(self):
        g=self.make();self.feed(g,{'content':'...'},8192,'length')
        self.assertEqual(g.verdict(),'length_without_useful_output')
    def test_elapsed_requires_reasoning_progress_not_prefill(self):
        g=self.make();self.now=150;self.assertIsNone(g.verdict())
        self.feed(g,count=2816);self.now=239;self.assertIsNone(g.verdict())
        self.now=240;self.assertEqual(g.verdict(),'elapsed_without_action')
    def test_missing_numeric_counts_are_not_invented(self):
        g=self.make()
        for _ in range(64): g.consume({'choices':[{'delta':{'reasoning_content':PRIVATE}}]})
        self.assertIsNone(g.generated_tokens);self.assertIsNone(g.verdict())
        self.now=91;self.assertEqual(g.verdict(),'elapsed_without_action')
    def test_disabled_and_invalid_policy(self):
        self.assertFalse(validated_policy({})['enabled'])
        for bad in [{'max_retries':2},{'hard_tokens':12},{'enabled':'true'},{'soft_tokens':4000,'hard_tokens':3500}]:
            with self.assertRaises(ValueError): validated_policy(bad)
    def test_retry_local_and_physical_limits_unchanged(self):
        original={'messages':[{'role':'system','content':'stable'},{'role':'user','content':'Build'}],
                  'max_tokens':8192,'max_completion_tokens':8192,'n_predict':8192,'reasoning_effort':'medium'}
        before=copy.deepcopy(original);retry=retry_body(original,validated_policy({}))
        self.assertEqual(original,before);self.assertEqual(retry['messages'][1]['role'],'system')
        self.assertEqual(retry['reasoning_effort'],'none');self.assertFalse(retry['chat_template_kwargs']['enable_thinking'])
        for key in ('max_tokens','max_completion_tokens','n_predict'):self.assertEqual(retry[key],8192)
    def test_metadata_is_numeric_allowlist(self):
        t,u=safe_metadata({'timings':{'predicted_n':4000,'secret':PRIVATE,'prompt_ms':PRIVATE},
                          'usage':{'completion_tokens':4096,'secret':PRIVATE,'completion_tokens_details':{'reasoning_tokens':4000,'secret':PRIVATE}}})
        self.assertNotIn(PRIVATE,json.dumps([t,u]));self.assertEqual(u['completion_tokens_details'],{'reasoning_tokens':4000})
