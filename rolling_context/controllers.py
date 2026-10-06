"""Opt-in conservative boundary controls. No model calls or in-flight mutation."""
import hashlib
import json
from collections import deque


class BoundaryController:
    def __init__(self):
        self.last_body = None
        self.last_feedback = None
        self.turn = 0
        self.last_change = -1000
        self.pressure_turns = 0
        self.quiet_turns = 0
        self.reads = deque(maxlen=12)
        self.no_progress_turns = 0
        self.last_reminder = None
        self.last_result_event = None

    def observe(self, body, feedback, settings, session):
        fingerprint = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        if fingerprint == self.last_body:
            return None, self.last_reminder
        self.last_body = fingerprint
        self.turn += 1
        fresh = bool(feedback and feedback.get('session_id') == session
                     and feedback.get('context_generation') != self.last_feedback)
        if fresh:
            self.last_feedback = feedback['context_generation']
        else:
            feedback = {}
        calls = next((m.get('tool_calls') for m in reversed(body) if m.get('tool_calls')), [])
        repeated_read = False
        progress = False
        result_message = next((m for m in reversed(body) if m.get('role') == 'tool'), {})
        result = result_message.get('content') or ''
        result_hash = hashlib.sha256(result.encode()).hexdigest()
        event = (result_message.get('tool_call_id'), result_hash)
        if event == self.last_result_event or not result_message:
            calls = []
        self.last_result_event = event
        for call in calls:
            function = call.get('function', {})
            name, arguments = function.get('name', ''), function.get('arguments', '')
            try:
                args = json.loads(arguments) if isinstance(arguments, str) else arguments
            except ValueError:
                args = {}
            command = str(args.get('command', '')) if isinstance(args, dict) else ''
            read = name in ('read_file', 'rolling_history_read', 'rolling_history_search') or (
                name == 'terminal' and any(word in command for word in ('cat ', 'sed ', 'head ', 'tail ')))
            progress |= name in ('write_file', 'patch', 'apply_patch', 'edit_file') or (
                name == 'terminal' and any(word in command for word in ('unittest', 'pytest', 'verify.sh', 'python3 -', 'mkdir ', 'cp ', 'cat >', 'cat <<')))
            if read:
                evidence = (name, json.dumps(args, sort_keys=True), result_hash)
                repeated_read |= evidence in self.reads
                self.reads.append(evidence)
        # These are conservative observations, not a semantic judgment of thought.
        self.no_progress_turns = self.no_progress_turns+1 if repeated_read and not progress else 0
        elastic = settings.get('elastic') or {}
        governor = settings.get('governor') or {}
        change = None
        if elastic.get('enabled') and fresh:
            lower, upper = elastic['min_tokens'], elastic['max_tokens']
            if not 28000 <= lower <= settings['target_tokens'] <= upper <= 44000:
                raise ValueError('Elastic bounds must contain the target and stay within 28-44K')
            pressure = repeated_read or bool(feedback.get('recall_pressure'))
            cache = feedback.get('cache_n', 0)/max(1, feedback.get('cache_n', 0)+feedback.get('prompt_n', 0))
            poor_cache = cache < elastic.get('cache_floor', .5) and feedback.get('prompt_ms', 0) >= elastic.get('prefill_floor_ms', 10000)
            self.pressure_turns = self.pressure_turns+1 if pressure else 0
            self.quiet_turns = self.quiet_turns+1 if not pressure and poor_cache else 0
            if self.turn-self.last_change >= elastic.get('cooldown_turns', 8):
                target = settings['target_tokens']
                step = elastic.get('step_tokens', 2000)
                if self.pressure_turns >= elastic.get('grow_turns', 3) and not poor_cache:
                    new_target = min(upper, target+step)
                    reason = 'persistent_repeat_read_pressure'
                elif self.quiet_turns >= elastic.get('shrink_turns', 6):
                    new_target = max(lower, target-step)
                    reason = 'persistent_fresh_prefill_without_repeat_pressure'
                else:
                    new_target = target
                    reason = ''
                if new_target != target:
                    change = {'from_tokens': target, 'to_tokens': new_target, 'reason': reason,
                              'turn': self.turn, 'cache_ratio': cache}
                    settings['target_tokens'] = new_target
                    self.last_change = self.turn
                    self.pressure_turns = self.quiet_turns = 0
        reminder = None
        observed = feedback.get('reasoning_observation') or {}
        if governor.get('enabled') and fresh and (
            feedback.get('governor_cancelled') or
            self.no_progress_turns >= governor.get('repeat_read_turns', 4) and
            (observed.get('last_reasoning_ms') or 0)-(observed.get('first_reasoning_ms') or 0) >= governor.get('soft_reasoning_ms', 30000)
        ):
            reminder = {'role': 'user', 'content':
                '[Execution continuity checkpoint]\n'
                'Continue the original task from the current verified files and tool results. '
                'Recent identical reads returned unchanged evidence. Prefer a concrete edit or focused verification next; '
                'use exact archived history if a needed fact is missing. Do not repeat a completed inspection or claim unverified success.\n'
                '[/Execution continuity checkpoint]'}
            self.no_progress_turns = 0
        self.last_reminder = reminder
        return change, reminder
