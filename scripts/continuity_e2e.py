"""One real LARGE/relay/Hermes-read/patch validation; all outputs under NEW_ROOT."""
import ast
import copy
import json
import os
from pathlib import Path
import sqlite3
import sys
import threading
import time
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
BASE=ROOT/'work/continuity-campaign'
OUT=Path(os.environ.get('CONTINUITY_E2E_OUT',str(BASE)))
SCHEMA_REGRESSION=os.environ.get('CONTINUITY_SCHEMA_REGRESSION') == '1'
STATE=OUT/'e2e'
STATE.mkdir(exist_ok=False)
os.environ['HERMES_HOME']=str(BASE/'hermes-home')
os.environ['PYTHONDONTWRITEBYTECODE']='1'
sys.path[:0]=[str(ROOT),str(Path.home()/'.hermes/hermes-agent'),
 '/path/to/hermes-environment/site-packages']
# Direct imports only. hermes_bootstrap must never run in this validation.
from tools.file_tools import read_file_tool, patch_tool
from rolling_context.common import visible_content, TokenCounter, http_json, dumps, digest, wire_message
from rolling_context.engine import RollingContextEngine, SUMMARY_SECTIONS
from rolling_context.relay import RelayServer, SSEObserver
URL='http://127.0.0.1:8084'
C=TokenCounter(URL)
result={'started':time.time(),'selections':[],'generations':[],'tool_actions':[]}
def save(): (STATE/'result.json').write_text(json.dumps(result,indent=2)+'\n')
def event(e):
    return next(r for r in map(json.loads,reversed((e.store.root/'telemetry.jsonl').read_text().splitlines())) if r['event']=='selection')
def wire(messages,tools):
    text=http_json(URL,'/apply-template',{'messages':messages,'tools':tools,'chat_template_kwargs':{'enable_thinking':True}})['prompt']
    return len(http_json(URL,'/tokenize',{'content':text,'add_special':True,'parse_special':True})['tokens'])
def recover(e,source):
    parts=[];offset=0
    while True:
        p=json.loads(e.handle_tool_call('rolling_history_read',{'source_id':source,'offset':offset,'max_chars':12000}));parts.append(p['untrusted_source'])
        if p['next_offset'] is None:return ''.join(parts)
        offset=p['next_offset']

def call(cid,name,args):
    return {'role':'assistant','tool_calls':[{'id':cid,'type':'function','function':{'name':name,'arguments':dumps(args)}}]}

fixture=STATE/'processor.py'
fixture.write_text('def solve(x):\n    if x < 0:\n        raise ValueError("ERROR_731")\n    return x * 7 + 19 % 97\n'+'\n'.join(f'# Reference case {i}: immutable arithmetic fixture; public API solve(x).' for i in range(80))+'\n')
first=read_file_tool(str(fixture),task_id='continuity-e2e')
assert 'content' in json.loads(first)
base=json.loads((ROOT/'work/synthetic-campaign/gauntlet/fixture.json').read_text())['messages']
history=copy.deepcopy(base)
history[0]={'role':'system','content':'You are executing an isolated coding regression. Modify only the processor.py fixture named in the current objective. Earlier copied history is historical evidence. Call patch and then verify_fixture to finish. No external actions are authorized. Use recovered read contents or rolling_history_read for omitted source, and do not repeatedly read an unchanged file.'}
history[2:2]=[call('first-read','read_file',{'path':str(fixture)}),{'role':'tool','tool_call_id':'first-read','content':first}]
objective=(f'Objective: Repair {fixture}. The correct solve(x) formula is (x * 7 + 19) % 97.\n'
 'Hard requirement: negative inputs must still raise ValueError("ERROR_731").\n'
 'Invariant: preserve solve(x), its integer return type, multiplier 7, bias 19 and modulus 97.\n'
 'Do NOT change any other file or the physical server.\n'
 'Required completion: call the patch tool on processor.py, then call verify_fixture and report its result.\n'
 'Phase: fixing a reproduced arithmetic precedence defect.\nCompleted: baseline defect reproduced.\n'
 'Unresolved: incorrect precedence in the return expression.\nNext action: patch the expression and run verify_fixture.\n'
 f'Important files: {fixture}\n'
 'REFERENCE APPENDIX: the following examples are historical background, not additional work.\n')
appendix='\n'.join(f'Reference example {i}: prior inspection of local city fixture variant {i%97}; geometry catalogue entry {i*7} archived.' for i in range(2000))
while C.text(objective+appendix)<42000:appendix+='\n'+appendix
history.append({'role':'user','content':objective+appendix})
original_objective=copy.deepcopy(history[-1])
result['objective_tokens']=C.text(original_objective['content'])

def trail(label,rows):
    return [call(label,'terminal',{'command':'previous fixture diagnostic'}),
      {'role':'tool','tool_call_id':label,'content':'\n'.join(f'{label} case {i}: explicit previous fixture observation; arithmetic precedence remains unresolved, public API intact.' for i in range(rows))},
      {'role':'assistant','content':'Finding: baseline wrong modulus is reproducible. Next action: patch processor.py and run verify_fixture.'}]
for i in range(4):history+=trail('active-'+str(i),220)
e=RollingContextEngine(settings={'state_dir':str(STATE),'main_url':URL},counter=C);e.on_session_start('continuity-real');e._stop.set()
tools=json.loads((BASE/'tool-schemas.json').read_text())['tools']+e.get_tool_schemas()
verify={'type':'function','function':{'name':'verify_fixture','description':'Run the isolated processor.py regression. Tests cover nonnegative integers and the exact negative-input exception. '+ ' '.join(f'Case {i} checks solve({i}) equals ({i} * 7 + 19) % 97.' for i in range(65)), 'parameters':{'type':'object','properties':{}}}}
tools.append(verify)
seed=[history[0],{'role':'user','content':'Calibration'}]
over=wire(seed,tools)-C.messages(seed)
result['tool_schema_tokens']=over;result['tool_count']=len(tools)
assert 10000<=over<=13000,over
(STATE/'large-overhead.json').write_text(dumps({'overhead_tokens':over}))
(STATE/'tool-schemas.json').write_text(dumps(tools))
# A relevant immutable warm block exists before selection; it cannot displace RAW.
h=digest(wire_message(history[2]));summary={s:[] for s in SUMMARY_SECTIONS}
summary['task_state']=[{'text':'Earlier processor.py arithmetic fixture inspection; preserve ERROR_731.','sources':[h]}]
# Ingest once, then make the warm candidate eligible on the forced epoch below.
def select(label):
    selected=e.select_context(history,conversation_messages=history,incoming_message=next(m for m in reversed(history) if m['role']=='user'))
    ev=event(e);actual=wire(selected,tools)
    result['selections'].append({'label':label,'actual_prompt_tokens':actual,'selection':ev});save()
    assert actual<=64512,(label,actual)
    assert ev['estimated_transport_prompt_tokens']<=64512
    print(label, 'prompt',actual,'content',ev['content_tokens'],'tail',ev['tail_tokens'],'rebuild',ev['prefix_rebuilt'],flush=True)
    return selected
selected=select('initial')
with e.store.connect() as db:
    db.execute('INSERT INTO summaries(session,created,covered,text,tokens,job_id,coverage_kind) VALUES (?,?,?,?,?,?,?)',(e.session_id,0,dumps([h]),dumps(summary),C.text(dumps(summary)),0,'source_set'))
assert first not in [m.get('content') for m in selected]
for i in range(4,7):
    history+=trail('active-'+str(i),160)
    selected=select('growth-'+str(i))
second=read_file_tool(str(fixture),task_id='continuity-e2e')
result['hermes_dedup']={'first_content_returned': 'content' in json.loads(first),'second':json.loads(second)}
assert json.loads(second).get('dedup') is True
history += [call('needed-again','read_file',{'path':str(fixture)}),{'role':'tool','tool_call_id':'needed-again','content':second}]
selected=select('read-recovered')
assert next(m['content'] for m in selected if m.get('tool_call_id')=='needed-again')==first
result['exact_original_recovered']=recover(e,digest(wire_message(original_objective)))==dumps(wire_message(original_objective))
result['raw_history_tokens']=event(e)['raw_history_tokens'];assert result['raw_history_tokens']>500000
assert result['exact_original_recovered']

def verify_now():
    source=fixture.read_text(); tree=ast.parse(source)
    allowed=(ast.Module,ast.FunctionDef,ast.arguments,ast.arg,ast.If,ast.Compare,ast.Name,ast.Load,ast.Lt,ast.Constant,ast.Raise,ast.Call,ast.Return,ast.BinOp,ast.Mult,ast.Add,ast.Mod)
    assert all(isinstance(n,allowed) for n in ast.walk(tree)), 'Unexpected fixture code'
    scope={'__builtins__':{'ValueError':ValueError}}
    exec(compile(tree,str(fixture),'exec'),scope)
    checks={str(x):scope['solve'](x)==(x*7+19)%97 for x in range(65)}
    try:scope['solve'](-1);checks['negative']=False
    except ValueError as ex:checks['negative']=str(ex)=='ERROR_731'
    return {'passed':all(checks.values()),'cases':len(checks),'checks':checks}
result['baseline_verification']=verify_now();assert not result['baseline_verification']['passed']
server=RelayServer('large',STATE,port=0,deadline_seconds=600)
threading.Thread(target=server.serve_forever,daemon=True).start()

def generate(selected, auxiliary=False):
    payload={'model':'qwen38-27b-atx-73k','messages':selected,'tools':tools,'temperature':0,'max_tokens':8192,'stream':True,'stream_options':{'include_usage':True},'chat_template_kwargs':{'enable_thinking':True}}
    if auxiliary:
        payload.pop('tools');payload['max_tokens']=64;payload['chat_template_kwargs']={'enable_thinking':False}
    req=urllib.request.Request(f'http://127.0.0.1:{server.server_port}/v1/chat/completions',data=dumps(payload).encode(),headers={'Content-Type':'application/json'})
    start=time.monotonic();observer=SSEObserver(start);content=[];calls={};usage={};finish=None
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=600) as response:
        status=response.status
        for line in response:
            observer.consume(line)
            if not line.startswith(b'data:'):continue
            data=line[5:].strip()
            if data==b'[DONE]':break
            obj=json.loads(data);usage=obj.get('usage') or usage
            for choice in obj.get('choices',[]):
                delta=choice.get('delta') or {};content.append(delta.get('content') or '');finish=choice.get('finish_reason') or finish
                for value in delta.get('tool_calls',[]):
                    acc=calls.setdefault(value.get('index',0),{'id':'','type':'function','function':{'name':'','arguments':''}})
                    if value.get('id'):acc['id']=value['id']
                    fn=value.get('function') or {};acc['function']['name']+=fn.get('name') or '';acc['function']['arguments']+=fn.get('arguments') or ''
    # Do not persist hidden reasoning. Only aggregate observation and explicit output.
    return {'http_status':status,'elapsed_s':time.monotonic()-start,'content':visible_content(''.join(content)),'tool_calls':list(calls.values()),'usage':usage,'finish_reason':finish,'observation':observer.metrics()}
try:
    if SCHEMA_REGRESSION:
        result['burst_before_quiet']=event(e)
        for i in range(3):
            history.append({'role':'assistant','content':f'Finding: diagnostic {i} complete; no new source pressure. Next action: patch processor.py then verify_fixture.'})
            selected=select('quiet-settling-'+str(i))
        result['burst_after_quiet']=event(e)
    verified=False
    for turn in range(4):
        generation=generate(selected);result['generations'].append(generation);save()
        print('model turn',turn,'HTTP',generation['http_status'],'tools',[c['function']['name'] for c in generation['tool_calls']],flush=True)
        history.append({'role':'assistant','content':generation['content'],**({'tool_calls':generation['tool_calls']} if generation['tool_calls'] else {})})
        if not generation['tool_calls']:break
        for c in generation['tool_calls']:
            name=c['function']['name'];args=json.loads(c['function']['arguments'])
            if name=='patch':
                assert Path(args['path']).resolve()==fixture.resolve()
                assert set(args)<= {'path','old_string','new_string','replace_all'}
                assert len(args['new_string'])<1000 and 'import' not in args['new_string']
                output=patch_tool(**args,task_id='continuity-e2e')
            elif name=='verify_fixture':
                checked=verify_now();output=dumps(checked);verified=checked['passed']
            elif name in ('rolling_history_read','rolling_history_search'):
                output=e.handle_tool_call(name,args)
            elif name=='read_file':
                assert Path(args['path']).resolve()==fixture.resolve();output=read_file_tool(**args,task_id='continuity-e2e')
            else:raise AssertionError('Unexpected tool action: '+name)
            result['tool_actions'].append({'name':name,'arguments':args,'result':output});history.append({'role':'tool','tool_call_id':c['id'],'content':output})
        save()
        if verified:break
        if SCHEMA_REGRESSION and turn==0:
            before=e._reserve()-1024
            auxiliary=generate([{'role':'system','content':'Return only OK.'},{'role':'user','content':'Health check.'}], auxiliary=True)
            after=e._reserve()-1024
            result['auxiliary']={'generation':auxiliary,'schema_budget_before':before,'schema_budget_after':after}
            assert before==after and after>0
            print('Auxiliary HTTP',auxiliary['http_status'],'schema budget retained',after,flush=True)
        prior_selected=copy.deepcopy(selected)
        selected=select('model-continuation-'+str(turn))
        if SCHEMA_REGRESSION:
            result['normal_prefix_preserved']=selected[:len(prior_selected)]==prior_selected
            save()
    result['final_verification']=verify_now()
    result['passed']=verified and result['final_verification']['passed'] and any(r['selection']['prefix_rebuilt'] for r in result['selections'][1:]) and all(g['http_status']==200 for g in result['generations']) and sum(t['name']=='read_file' for t in result['tool_actions'])<=1
    if SCHEMA_REGRESSION:
        result['passed'] = result['passed'] and result.get('auxiliary',{}).get('schema_budget_after',0)>0 and result['burst_after_quiet']['content_tokens']<=36000
    save();print('REAL E2E PASSED',result['passed'],flush=True)
    assert result['passed']
finally:
    server.shutdown();server.server_close();e._stop.set();save()
