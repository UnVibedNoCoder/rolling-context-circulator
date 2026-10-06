"""Fresh-state LARGE/relay quality, warm-prefix timings and coding validation."""
import sys,json,time,copy,threading,urllib.request,subprocess,uuid
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parent))
from circulator_stress import *
from rolling_context.common import visible_content
from rolling_context.relay import SSEObserver

def generate(messages,state,tools=None,thinking=False,limit=512,force_json=False):
    payload={'model':'qwen38-27b-atx-73k','messages':messages,'temperature':0,'max_tokens':limit,'stream':True,'stream_options':{'include_usage':True},'timings_per_token':True,'chat_template_kwargs':{'enable_thinking':thinking}}
    if tools:payload['tools']=tools
    if force_json:
        payload['tool_choice']='none'
        payload['response_format']={'type':'json_object'}
    req=urllib.request.Request('http://127.0.0.1:8094/v1/chat/completions',data=dumps(payload).encode(),headers={'Content-Type':'application/json'})
    started=time.monotonic();observer=SSEObserver(started);text=[];calls={};usage={};timings={};finish=None
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req,timeout=600) as r:
        for line in r:
            observer.consume(line)
            if not line.startswith(b'data:'):continue
            v=line[5:].strip()
            if v==b'[DONE]':break
            x=json.loads(v);usage=x.get('usage') or usage;timings=x.get('timings') or timings
            for c in x.get('choices',[]):
                d=c.get('delta',{});text.append(d.get('content') or '');finish=c.get('finish_reason') or finish
                for t in d.get('tool_calls',[]):
                    n=t.get('index',0);acc=calls.setdefault(n,{'id':'','type':'function','function':{'name':'','arguments':''}})
                    if t.get('id'):acc['id']=t['id']
                    fn=t.get('function',{});acc['function']['name']+=fn.get('name') or '';acc['function']['arguments']+=fn.get('arguments') or ''
    record={'elapsed_s':time.monotonic()-started,'observer':observer.metrics(),'content':visible_content(''.join(text)),'tool_calls':list(calls.values()),'usage':usage,'timings':timings,'finish_reason':finish}
    record['kernel_check']=subprocess.run(['journalctl','-k','--since','@'+str(int(json.loads((OUT/'hardware-before.json').read_text())['ts'])),'--no-pager'],capture_output=True,text=True).stdout
    record['gpu_fault']=bool(__import__('re').search(r'NVRM: Xid',record['kernel_check']))
    return record

def relay(state):
    server=RelayServer('large',state,port=8094,deadline_seconds=600);thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();return server

def quality(label,conf,rep=0):
    history=json.loads((OUT/'fixtures/history.json').read_text());history[0]['content']+=f'\nTrial cache isolation {uuid.uuid4().hex}.'
    state=OUT/'live'/f'{label}-r{rep}';assert not state.exists()
    e=RollingContextEngine(settings={**conf,'state_dir':str(state)},counter=COUNTER);e.on_session_start(f'{label}-{rep}');server=relay(state)
    req=copy.deepcopy(history)+[{'role':'user','content':'Continue the current task.'}];results=[]
    try:
        with patch('rolling_context.engine.spawn_context_thread',return_value=Blocked()):
            e.select_context(req,conversation_messages=req,incoming_message=req[-1])
            probes=[('old','What were campaign_epoch_alpha clutch_limit and exact error sentinel? Respond ONLY JSON with keys old_limit (integer), error (string).',{'old_limit':7,'error':'E_LOCUS_MISMATCH_419'}),('current','What is campaign_epoch_beta current clutch_limit and unchanged transport port? Respond ONLY JSON keys limit (integer), port (integer).',{'limit':11,'port':8094}),('multiple','What exact original compactor alias, port, physical context and rejected random approach? Respond ONLY JSON keys alias (string), port (integer), context (integer), rejected (string).',{'alias':'qwen35-4b-compactor','port':8083,'context':65536,'rejected':'Random'}),('dependency','What must build depend on, and what must validation depend on? Respond ONLY JSON keys build_requires (string), validation_requires (string).',{'build_requires':'validation','validation_requires':'RNG'})]
            for name,q,expect in probes:
                req.append({'role':'user','content':q});sel=e.select_context(req,conversation_messages=req,incoming_message=req[-1]);ev=event(e)
                result=generate(sel,state,limit=256);req.append({'role':'assistant','content':result['content']})
                try:answer=json.loads(result['content'].strip().removeprefix('```json').removesuffix('```').strip())
                except ValueError:answer={}
                hit={k:(answer.get(k)==v if isinstance(v,int) else v.lower() in str(answer.get(k,'')).lower()) for k,v in expect.items()}
                results.append({'probe':name,'expected':expect,'answer':answer,'checks':hit,'selection':ev,'generation':result});write(f'live/{label}-r{rep}/results.json',results)
                print(label,rep,name,hit,round(result['elapsed_s'],2),result['timings'],flush=True)
    finally:server.shutdown();server.server_close()
    return results

def main():
    parser=argparse.ArgumentParser();parser.add_argument('phase',choices=['baseline','coherent']);a=parser.parse_args()
    if a.phase=='baseline':
        for label,conf in [('current',{}),('stable20',settings(20000,'stable'))]:quality(label,conf)
    else:
        for rep in range(2):
            for target in ([16000,24000,20000,28000] if rep==0 else [28000,20000,24000,16000]):quality(f'coherent{target}',settings(target,'coherent'),rep)
if __name__=='__main__':main()
