"""Bounded structural inspection pressure from completed visible tool results."""
from collections import OrderedDict
import json
import re
import shlex
from .common import digest, persistent_message

_READS = {'read_file', 'rolling_file_snapshot', 'rolling_snapshot_read', 'rolling_raw_read',
          'rolling_history_read', 'rolling_history_search', 'search_files', 'search', 'grep'}
_WRITES = {'write_file', 'patch', 'apply_patch', 'edit_file'}
_INSPECT = re.compile(r'(?:^|&&|;|\|)\s*(?:cat|sed|head|tail|rg|grep|find|git\s+(?:show|diff|log|status))\b')
_VERIFY = re.compile(r'\b(?:pytest|unittest|ctest|test|tests|build|compile|check|lint|verify|verification)\b')
def _diagnostic(value):
    value = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', value)
    value = re.sub(r'\b\d+(?:\.\d+)?\s*(?:ms|seconds?|secs?|s)\b', '<duration>', value)
    return re.sub(r'\b\d{4}-\d{2}-\d{2}[T ][0-9:.+-]+Z?\b', '<timestamp>', value)


class InspectionTracker:
    def __init__(self):
        self.session = None; self.count = 0; self.prefix = None
        self.calls = OrderedDict(); self.reads = OrderedDict(); self.results = OrderedDict()
        self.repeats = self.turn = self.write_epoch = 0
        self.progress = False; self.changed = False; self.user = None

    @staticmethod
    def _remember(table, key, value):
        previous = table.get(key)
        table[key] = value; table.move_to_end(key)
        while len(table) > 128:
            table.popitem(last=False)
        return previous != value

    def clear(self):
        self.repeats = 0; self.reads.clear()

    def _result(self, call, message, bootstrap):
        try:
            result = json.loads(message.get('content') or '')
        except (ValueError, TypeError):
            return
        if call['kind'] == 'read' and isinstance(result, list):
            result = {'results': result}
        if not isinstance(result, dict):
            return
        error = result.get('error')
        if error:
            if self._remember(self.results, call['key'], digest(_diagnostic(str(error)))):
                self.clear(); self.progress = not bootstrap
            return
        if result.get('success') is False or result.get('no_change'):
            return
        kind = call['kind']
        if kind == 'read':
            if call['terminal'] and type(result.get('exit_code')) is not int:
                return
            if call['terminal'] and result['exit_code'] != 0:
                if self._remember(self.results, call['key'], digest(result.get('output'))):
                    self.clear(); self.progress = not bootstrap
                return
            key = call['key']
            if result.get('dedup') or result.get('status') == 'unchanged':
                identity = self.reads.get(key, digest('unchanged'))
            else:
                evidence = {k: result[k] for k in ('content', 'sha256', 'fingerprint', 'source_fingerprint',
                    'matches', 'results', 'output', 'excerpt', 'untrusted_source', 'untrusted_history') if k in result}
                if not evidence:
                    return
                if isinstance(evidence.get('output'), str):
                    evidence['output'] = _diagnostic(evidence['output'])
                identity = digest(evidence)
            novel = self._remember(self.reads, key, identity)
            self.turn += int(not bootstrap); self.changed = not bootstrap
            self.repeats = 0 if novel or bootstrap else min(128, self.repeats + 1)
            self.progress = self.progress or novel and not bootstrap
            return
        if kind == 'write':
            paths = result.get('files_modified') or result.get('files_created') or result.get('files_deleted')
            if not isinstance(paths, list) or not paths:
                return
            identity = digest([paths, call['data'], result.get('diff')])
            novel = self._remember(self.results, call['key'], identity)
            if novel:
                self.write_epoch += 1
        elif kind == 'verify' or call['terminal']:
            code = result.get('exit_code'); output = result.get('output')
            if type(code) is not int or not isinstance(output, str):
                return
            if kind != 'verify' and not output.strip():
                return
            key = digest([call['key'], self.write_epoch])
            novel = self._remember(self.results, key, digest([code, _diagnostic(output)]))
        else:
            return
        if novel:
            self.clear(); self.progress = not bootstrap

    def observe(self, session, body):
        messages = [persistent_message(m) for m in body]
        hashes = [digest(m) for m in messages]
        if session != self.session or self.count > len(hashes) or self.prefix != digest(hashes[:self.count]):
            self.__init__(); self.session = session
        bootstrap = self.prefix is None
        self.changed = self.progress = False
        for message in messages[self.count:]:
            if message.get('role') == 'user':
                text = str(message.get('content') or '').strip()
                if text.lower().strip('.!') not in {'continue','go on','proceed','yes','ok','okay'}:
                    key = digest(text)
                    if self.user != key:
                        self.clear(); self.user = key
            for call in message.get('tool_calls') or []:
                if not isinstance(call, dict) or not isinstance(call.get('id'), str):
                    continue
                fn = call.get('function') or {}
                try:
                    args = json.loads(fn.get('arguments') or '{}')
                except (ValueError, TypeError):
                    continue
                if not isinstance(args, dict):
                    continue
                name = fn.get('name'); command = args.get('command', '')
                terminal = name in {'terminal', 'process'}
                read = name in _READS or name == 'terminal' and isinstance(command, str) and bool(_INSPECT.search(command)) and not re.search(r'(?<![<>])>(?![>&])', command)
                kind = 'read' if read else 'write' if name in _WRITES else 'verify' if terminal and isinstance(command, str) and _VERIFY.search(command) else 'other'
                key_args = {k: v for k, v in args.items() if k not in {'content','tool_call_id','request_id'}}
                if isinstance(command, str) and command:
                    try: key_args['command'] = shlex.split(command)
                    except ValueError: pass
                value = {'key': digest([name, key_args]), 'kind': kind, 'terminal': terminal,
                         'data': digest(args.get('content') or args)}
                self._remember(self.calls, call['id'], value)
            if message.get('role') == 'tool':
                call = self.calls.pop(message.get('tool_call_id'), None)
                if call:
                    self._result(call, message, bootstrap)
        self.count = len(messages); self.prefix = digest(hashes)
        return self.repeats


def inspection_control():
    return {'role': 'system', 'content':
        '[REPEATED INSPECTION CONTROL: SYSTEM-DERIVED, TRANSIENT]\n'
        'Repeated inspections returned unchanged evidence without implementation or new verification. '
        'Do one falsifiable test, make an implementation change, or obtain genuinely new evidence next. '
        'Use existing read/search results instead of rereading the same region or function. '
        'A new failing test, changed source, runtime/error result or user requirement permits investigation again. '
        'Preserve the original task and all RAW evidence; do not claim unverified success.\n'
        '[/REPEATED INSPECTION CONTROL]'}
