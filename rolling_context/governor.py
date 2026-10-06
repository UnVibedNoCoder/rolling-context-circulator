"""Numeric-only generation governor. No reasoning text is retained or compared."""
import copy
import math
import time

DEFAULTS = {'enabled': False, 'soft_tokens': 2816, 'hard_tokens': 3840,
            'no_action_seconds': 90, 'max_retries': 1, 'retry_thinking': False,
            'useful_visible_chars': 32}
DIAGNOSTIC = ('Rolling Context stopped this request after its deliberation budget was exhausted. '
              'No further automatic retry will be made. The task remains incomplete; review the last '
              'verified state before starting another request.')
CONTROL = ('[GENERATION GOVERNOR: SYSTEM-DERIVED, TRANSIENT]\n'
           'The previous attempt exhausted its deliberation budget without useful visible/action output. '
           'Settled decisions remain in force unless current authoritative evidence contradicts them. '
           'Produce the next concrete tool/action or a concise visible answer now. Do not re-plan. '
           'This control is not user input or independent evidence.\n[/GENERATION GOVERNOR]')


def validated_policy(value):
    if not isinstance(value, dict):
        raise ValueError('Invalid generation governor policy')
    p = {**DEFAULTS, **value}
    if type(p['enabled']) is not bool or type(p['retry_thinking']) is not bool:
        raise ValueError('Invalid generation governor toggle')
    for key, lo, hi in [('soft_tokens',2500,4000), ('hard_tokens',3500,8192),
                        ('no_action_seconds',60,300), ('max_retries',0,1), ('useful_visible_chars',1,64)]:
        if type(p[key]) is not int or not lo <= p[key] <= hi:
            raise ValueError('Invalid generation governor bound')
    if p['soft_tokens'] > p['hard_tokens']:
        raise ValueError('Invalid generation governor thresholds')
    return p


def numeric(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def safe_metadata(obj):
    """Allowlist numbers only; backend metadata is never trusted as log text."""
    raw_timing = obj.get('timings') if isinstance(obj.get('timings'), dict) else {}
    raw_usage = obj.get('usage') if isinstance(obj.get('usage'), dict) else {}
    timings = {k:v for k,v in raw_timing.items()
               if k in {'prompt_n','cache_n','prompt_ms','prompt_per_second','predicted_n',
                        'predicted_ms','predicted_per_second'} and numeric(v) is not None}
    usage = {k:v for k,v in raw_usage.items()
             if k in {'prompt_tokens','completion_tokens','total_tokens'} and numeric(v) is not None}
    details = raw_usage.get('completion_tokens_details') or {}
    if isinstance(details, dict) and numeric(details.get('reasoning_tokens')) is not None:
        usage['completion_tokens_details'] = {'reasoning_tokens': details['reasoning_tokens']}
    return timings, usage


class GenerationGovernor:
    def __init__(self, policy, clock=time.monotonic):
        self.policy = validated_policy(policy)
        self.clock = clock
        self.started = clock()
        self.reasoning_started = None
        self.generated_tokens = None
        self.reasoning_tokens = None
        self.reasoning_chunks = self.reasoning_characters = self.visible_characters = 0
        self.nonwhite_characters = 0
        self.tool_call_started = False
        self.finish_reason = None
        self.soft_emitted = False

    def consume(self, obj):
        timings, usage = safe_metadata(obj)
        count = timings.get('predicted_n', usage.get('completion_tokens'))
        if count is not None:
            self.generated_tokens = max(self.generated_tokens or 0, count)
        reasoning = usage.get('completion_tokens_details', {}).get('reasoning_tokens')
        if reasoning is not None:
            self.reasoning_tokens = max(self.reasoning_tokens or 0, reasoning)
        for choice in obj.get('choices', []):
            delta = choice.get('delta', choice.get('message', {})) or {}
            # Only type/length; never tokenize, retain, compare, or emit this field.
            hidden = delta.get('reasoning_content', delta.get('reasoning'))
            if isinstance(hidden, str) and hidden:
                self.reasoning_characters += len(hidden)
                self.reasoning_chunks += 1
                if self.reasoning_started is None:
                    self.reasoning_started = self.clock()
            content = delta.get('content')
            if isinstance(content, str):
                self.visible_characters += len(content)
                self.nonwhite_characters += len(content.strip())
            self.tool_call_started |= bool(delta.get('tool_calls') or delta.get('function_call'))
            if choice.get('finish_reason'):
                self.finish_reason = choice['finish_reason']

    @property
    def useful(self):
        return self.tool_call_started or self.nonwhite_characters >= self.policy['useful_visible_chars']

    def verdict(self):
        if not self.policy['enabled'] or self.tool_call_started:
            return None
        if self.finish_reason == 'length' and not self.useful:
            return 'length_without_useful_output'
        # Any visible answer progress protects an ongoing generation.
        if self.nonwhite_characters:
            return None
        count = max(self.generated_tokens or 0, self.reasoning_tokens or 0)
        if count >= self.policy['hard_tokens']:
            return 'hard_no_action_tokens'
        elapsed = self.clock() - self.reasoning_started if self.reasoning_started is not None else 0
        if elapsed >= self.policy['no_action_seconds'] and (count >= self.policy['soft_tokens'] or
                                                           (self.generated_tokens is None and self.reasoning_chunks >= 64)):
            return 'elapsed_without_action'
        return None

    def soft(self):
        count = max(self.generated_tokens or 0, self.reasoning_tokens or 0)
        if not self.soft_emitted and not self.nonwhite_characters and not self.tool_call_started and count >= self.policy['soft_tokens']:
            self.soft_emitted = True
            return True
        return False

    def metrics(self):
        return {'generated_tokens': self.generated_tokens, 'reasoning_tokens': self.reasoning_tokens,
                'reasoning_chunks': self.reasoning_chunks, 'reasoning_characters': self.reasoning_characters,
                'visible_characters': self.visible_characters, 'tool_call_started': self.tool_call_started,
                'elapsed_ms': round((self.clock()-self.started)*1000),
                'finish_reason': self.finish_reason if self.finish_reason in {'stop','length','tool_calls','content_filter'} else None}


def retry_body(body, policy):
    result = copy.deepcopy(body)
    messages = result['messages']
    index = next((i for i,m in enumerate(messages) if m['role'] not in {'system','developer'}), len(messages))
    messages.insert(index, {'role':'system', 'content':CONTROL})
    if not policy['retry_thinking']:
        # Verified against this llama.cpp server-common.cpp; request-local only.
        result['reasoning_effort'] = 'none'
        result['chat_template_kwargs'] = {**result.get('chat_template_kwargs', {}), 'enable_thinking':False}
    return result
