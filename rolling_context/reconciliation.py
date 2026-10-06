"""Conservative visible-evidence checks and explicit task resolution keys."""
import json
import re
from .common import persistent_message

_VERIFY_COMMAND = re.compile(r'\b(?:pytest|unittest|ctest)\b|\b(?:npm|pnpm|yarn)\s+(?:run\s+)?test\b|(?:^|[/ ])verify\.sh\b')
_PASS = re.compile(r'\b[1-9]\d*\s+(?:tests?\s+)?passed\b|(?:^|\n)OK\s*(?:$|\n)|\b(?:all tests passed|tests passed)\b', re.I)
_COMMIT = re.compile(r'\bgit\s+commit\b')
_COMMIT_RESULT = re.compile(r'^\[[^\]\n]+\s+[0-9a-f]{7,40}\]', re.M)
_COMMIT_ACTION = re.compile(r'^(?:git\s+)?commit\b|\bcommit\s+(?:is\s+)?(?:required|needed|pending)\b|\b(?:must|need(?:s)? to)\s+commit\b', re.I)
_STRENGTH = {
    'verification': re.compile(r'\b(?:verified|validated|tested|tests? passed|passing tests)\b', re.I),
    'commit': re.compile(r'\bcommitted\b', re.I),
    'change': re.compile(r'\b(?:implemented|patched|fixed|resolved|completed)\b', re.I),
}
_NEGATED = re.compile(r'\b(?:not(?: yet)?|never|unverified|pending|needs? to be|must be|should be|to be|cannot|could not|without)\s+(?:been\s+)?$', re.I)


def resolution_key(text):
    return re.sub(r'\s+', ' ', text.replace('**', '').strip().rstrip('.!;')).casefold()


def commit_action(text):
    return bool(_COMMIT_ACTION.search(text))


def tool_fact(message, call=None):
    """A read/command string alone never proves execution or success."""
    if message.get('role') != 'tool':
        return None
    try:
        result = json.loads(message.get('content') or '')
    except (ValueError, TypeError):
        return None
    if not isinstance(result, dict) or result.get('error') or result.get('success') is False:
        return None
    fn = (call or {}).get('function') or {}
    try:
        args = json.loads(fn.get('arguments') or '{}')
    except (ValueError, TypeError):
        args = {}
    name = fn.get('name') or message.get('name')
    command = args.get('command', '') if isinstance(args, dict) else ''
    output = result.get('output', '')
    if name in ('terminal', 'process') and type(result.get('exit_code')) is int and result['exit_code'] == 0 and isinstance(output, str):
        if _COMMIT.search(command) and '--dry-run' not in command and _COMMIT_RESULT.search(output):
            return 'commit'
        if _VERIFY_COMMAND.search(command) and _PASS.search(output):
            return 'verification'
    if name in ('write_file', 'patch', 'apply_patch', 'edit_file') and not result.get('no_change'):
        if any(isinstance(result.get(k), list) and result[k] for k in ('files_modified', 'files_created', 'files_deleted')):
            return 'change'
    return None


def evidence_index(messages, hashes):
    calls = {}; facts = {}; records = {}
    for message, source in zip(messages, hashes):
        visible = persistent_message(message); records[source] = visible
        for call in visible.get('tool_calls') or []:
            if isinstance(call, dict) and isinstance(call.get('id'), str):
                calls[call['id']] = call
        fact = tool_fact(visible, calls.get(visible.get('tool_call_id')))
        if fact:
            facts[source] = fact
    return records, facts


def unsupported_claim(item, facts):
    text = item['text']
    support = {facts.get(source) for source in item['sources']}
    for category, pattern in _STRENGTH.items():
        asserted = any(not _NEGATED.search(text[max(0, m.start()-40):m.start()]) for m in pattern.finditer(text))
        required = {'change', 'verification', 'commit'} if category == 'change' else {category}
        if asserted and not support.intersection(required):
            return category
    return None


def validate_claims(summary, facts):
    for items in summary.values():
        for item in items:
            if item.get('status') not in ('INFERENCE', 'CONFLICTING') and unsupported_claim(item, facts):
                from .compaction import CompactionFailure
                raise CompactionFailure('evidence_overstated', 'preflight')
