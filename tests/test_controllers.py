import copy
import json
from pathlib import Path
import tempfile
import unittest

from rolling_context.controllers import BoundaryController


class ControllerTests(unittest.TestCase):
    def history(self, step, command='cat same.py', result='same evidence'):
        return [{'role':'user','content':str(step)},
                {'role':'assistant','tool_calls':[{'function':{'name':'terminal','arguments':json.dumps({'command':command})}}]},
                {'role':'tool','tool_call_id':str(step),'content':result}]

    def feedback(self, generation, cache=30000, fresh=5000):
        return {'session_id':'one','context_generation':generation,'cache_n':cache,'prompt_n':fresh,
                'prompt_ms':15000,'reasoning_observation':{'first_reasoning_ms':0,'last_reasoning_ms':40000}}

    def settings(self):
        return {'target_tokens':38000,'elastic':{'enabled':True,'min_tokens':36000,'max_tokens':42000,
                 'grow_turns':3,'shrink_turns':6,'cooldown_turns':8,'step_tokens':2000}}

    def test_growth_requires_persistent_pressure_and_cooldown(self):
        controller=BoundaryController();settings=self.settings();changes=[]
        for i in range(12):
            change,_=controller.observe(self.history(i),self.feedback(i+1),settings,'one')
            if change:changes.append(change)
        self.assertEqual([c['turn'] for c in changes],[4,12])
        self.assertEqual(settings['target_tokens'],42000)
        for i in range(12,25):controller.observe(self.history(i),self.feedback(i+1),settings,'one')
        self.assertEqual(settings['target_tokens'],42000)

    def test_low_cache_vetoes_growth_and_sustained_quiet_shrinks(self):
        controller=BoundaryController();settings=self.settings()
        for i in range(8):controller.observe(self.history(i),self.feedback(i+1,5000,30000),settings,'one')
        self.assertEqual(settings['target_tokens'],38000)
        for i in range(8,14):controller.observe(self.history(i,result=str(i)),self.feedback(i+1,5000,30000),settings,'one')
        self.assertEqual(settings['target_tokens'],36000)

    def test_unrelated_stale_feedback_and_duplicate_boundary_do_not_act(self):
        controller=BoundaryController();settings=self.settings();original=copy.deepcopy(settings)
        for i in range(20):controller.observe(self.history(i),self.feedback(1),settings,'other')
        self.assertEqual(settings,original)
        body=self.history(21);controller.observe(body,self.feedback(2),settings,'one')
        turn=controller.turn
        for _ in range(5):controller.observe(body,self.feedback(3),settings,'one')
        self.assertEqual(controller.turn,turn)

    def test_checkpoint_preserves_retry_snapshot_and_requires_no_progress(self):
        controller=BoundaryController();settings={'target_tokens':38000,'governor':{'enabled':True}}
        for i in range(5):_,reminder=controller.observe(self.history(i),self.feedback(i+1),settings,'one')
        self.assertIsNotNone(reminder)
        self.assertEqual(controller.observe(self.history(4),self.feedback(5),settings,'one')[1],reminder)
        self.assertIsNone(controller.observe(self.history(5,'python3 -m unittest'),self.feedback(6),settings,'one')[1])

