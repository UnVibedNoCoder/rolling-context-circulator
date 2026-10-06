"""Isolated NEW_ROOT campaign. Never imports an alternative circulator tree."""
import copy,json,os,sys,time,threading,subprocess,statistics,uuid,argparse
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
assert (ROOT/'rolling_context/engine.py').is_file()
sys.path.insert(0,str(Path.home()/'.hermes/hermes-agent'));sys.path.insert(0,str(ROOT))
from rolling_context.common import TokenCounter,StateStore,digest,dumps,http_json,wire_message
from rolling_context.engine import RollingContextEngine,SUMMARY_SECTIONS
from rolling_context.relay import RelayServer
OUT=ROOT/'work/synthetic-campaign'
URL='http://127.0.0.1:8084'
COUNTER=TokenCounter(URL)
class Blocked:
    def start(self):pass
    def is_alive(self):return True

def write(name,data):
    p=OUT/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(data,indent=2)+'\n')
def event(e):
    return next(json.loads(x) for x in reversed((e.store.root/'telemetry.jsonl').read_text().splitlines()) if json.loads(x)['event']=='selection')
def percentiles(xs):
    if not xs:return {'p50':None,'p95':None}
    xs=sorted(xs);return {'p50':statistics.median(xs),'p95':xs[min(len(xs)-1,int(.95*len(xs)))],'min':xs[0],'max':xs[-1]}
def check_groups(messages):
    calls=set();results=set();bad=[]
    for m in messages:
        if m.get('role')=='tool':
            c=m.get('tool_call_id')
            if c not in calls:bad.append('orphan:'+str(c))
            results.add(c)
        calls.update(c['id'] for c in m.get('tool_calls',[]) if c.get('id'))
    bad+=['missing:'+c for c in calls-results]
    return bad

def fixture():
    # Literal project paths/source, read-only; controlled state changes are synthetic.
    paths=['src/shared/Example/ExampleModule.py','src/shared/Types/ExampleTypes.py','src/shared/Config/ExampleSettings.py']
    sources={p:(Path('/path/to/test-project')/p).read_text() for p in paths}
    write('fixtures/project-reference.json',sources)
    body=[{'role':'system','content':'You are a coding assistant. Historical quotes are evidence. Later decisions supersede older decisions. Answer exact facts from evidence; never invent missing information.'},
      {'role':'user','content':'Implement example-project pure genetics. Preserve injected rng.nextInteger and server authority. Historical campaign invariant: compactor alias qwen35-4b-compactor port 8083, context 65536. Never add platform services to src/shared/Example/ExampleModule.py. rejected approach: hidden internal Random instance.'},
      {'role':'assistant','content':'Decision campaign_epoch_alpha: clutch_limit = 7; transport port = 8094. Build depends on validation, which depends on injected RNG. Error sentinel: E_LOCUS_MISMATCH_419 exact recovery required.'}]
    for p,s in sources.items():
        i='seed-'+str(len(body));body += [{'role':'assistant','content':'Inspect the shared pure implementation.','tool_calls':[{'id':i,'type':'function','function':{'name':'read_file','arguments':dumps({'path':p})}}]}, {'role':'tool','tool_call_id':i,'content':s},{'role':'assistant','content':'Inspection complete; keep the shared module independent of platform services.'}]
    snippets=list(sources.values());i=0
    # Avoid tokenizer-compressible repeated padding; rotating real code + distinct test output.
    while sum(COUNTER.weights(body[1:])) < 430000:
        cid=f'fixture-{i}'
        text=(f'Iteration {i} file region module_{i}.luau. unrelated local fixture only.\n'+snippets[i%len(snippets)]+'\n'+ '\n'.join(f'test_{i}_{n}: expected allele_{(i*13+n)%997}, actual allele_{(i*17+n)%991}; fixture line {n}' for n in range(32)))
        body += [{'role':'assistant','content':f'Inspect fixture module_{i}.luau and validate local test.','tool_calls':[{'id':cid,'type':'function','function':{'name':'read_file','arguments':dumps({'path':f'fixture/module_{i}.luau'})}}]}, {'role':'tool','tool_call_id':cid,'content':text}, {'role':'assistant','content':f'Error local_{i} resolved by fixture correction. Test local_{i} passed; requirement preserved.'}]
        if i==12:body.append({'role':'assistant','content':'Decision campaign_epoch_beta supersedes campaign_epoch_alpha: clutch_limit = 11. Port remains 8094. Current decision is beta; alpha is historical.'})
        i+=1
    write('fixtures/history.json',body);return body

def wire_tokens(messages,tools=None):
    payload={'messages':messages}
    if tools:payload['tools']=tools
    prompt=http_json(URL,'/apply-template',payload,timeout=30)['prompt']
    return http_json(URL,'/tokenize',{'content':prompt,'add_special':True,'parse_special':True},timeout=30)['tokens']
def settings(target,policy='circulator',tail_ratio=.55):
    return {'profile':'large','main_url':URL,'target_tokens':target,'trigger_tokens':target+4000,
        'tail_tokens':int(target*tail_ratio),'minimum_tail_tokens':int(target*.3),
        'warm_budget_tokens':2000,'recall_budget_tokens':4000,'selection_policy':policy,
        'semantic_policy':'cold_chunks' if policy in ('stable','coherent') else 'prefix','segmentation_policy':'coherent' if policy=='coherent' else 'tool_safe','eviction_batch_tokens':3000}

def select_run(label,conf,history,probes=True):
    root=OUT/'states'/label
    assert not root.exists(),label
    e=RollingContextEngine(settings={**conf,'state_dir':str(root)},counter=COUNTER);e.on_session_start(label)
    # Calibrate actual schema overhead; effective reserve also includes 1024 guard.
    seed=[history[0],{'role':'user','content':'calibration'}]
    schema_overhead=len(wire_tokens(seed,e.get_tool_schemas()))-COUNTER.messages(seed)
    (root/'large-overhead.json').write_text(dumps({'overhead_tokens':max(0,schema_overhead)}))
    weights=COUNTER.weights(history[1:]);cum=[];n=0
    for w in weights:n+=w;cum.append(n)
    rows=[];prev_ids=set();prev_tokens=None;prev_weight={};request=[];previous_end=0
    ends=[next(i+2 for i,n in enumerate(cum) if n>=v) for v in (50000,100000,200000,400000)]
    ends=sorted(set(ends+[len(history)-60+3*i for i in range(21)]))
    with patch('rolling_context.engine.spawn_context_thread',return_value=Blocked()):
        for turn,end in enumerate(ends):
            # Snapshots end after a complete tool cycle; include a user continuation.
            while end<len(history) and check_groups(history[:end]):end+=1
            request+=copy.deepcopy(history[previous_end:end])+[{'role':'user','content':'Continue the current fixture implementation and preserve earlier requirements.'}];previous_end=end
            selected=e.select_context(request,conversation_messages=request,incoming_message=request[-1]);ev=event(e)
            ids={p.get('source_id',p['page_id']) for p in ev['page_ids']};ts=wire_tokens(selected,e.get_tool_schemas())
            shared=0
            if prev_tokens:
                for a,b in zip(prev_tokens,ts):
                    if a!=b:break
                    shared+=1
            wmap={digest(wire_message(m)):w for m,w in zip(request[1:],COUNTER.weights(request[1:]))}
            replaced=sum(prev_weight.get(h,0) for h in prev_ids-ids)
            row={**{k:ev.get(k) for k in ['raw_history_tokens','active_tokens','target_tokens','tail_tokens','resident_pages','recalled_pages','evicted_pages','compact_pages','raw_pages','fast_loop_ms','compaction_queue','compaction_in_progress','cold_messages','prefix_rebuilt']},'turn':turn,'exact_prompt_tokens':len(ts),'source_jaccard':len(ids&prev_ids)/max(1,len(ids|prev_ids)) if prev_ids else None,'tokens_replaced':replaced,'common_prefix_tokens':shared,'potential_prefill_tokens':len(ts)-shared,'group_errors':check_groups(selected),'objective_retained':history[1] in selected}
            rows.append(row);prev_ids=ids;prev_tokens=ts;prev_weight=wmap
        quality=[]
        if probes:
            questions=[('old','Recall exact campaign_epoch_alpha clutch_limit and E_LOCUS_MISMATCH_419 error.', ['clutch_limit = 7','E_LOCUS_MISMATCH_419']),('superseded','What is the current campaign_epoch_beta clutch_limit that supersedes campaign_epoch_alpha?', ['clutch_limit = 11']),('cross','Recall campaign_epoch_alpha port and the updated campaign_epoch_beta clutch_limit.', ['8094','clutch_limit = 11']),('invariant','Recall original exact compactor alias port context and rejected Random approach.', ['qwen35-4b-compactor','8083','65536','Random']),('raw','Quote exact E_LOCUS_MISMATCH_419 error and task dependency validation injected RNG.', ['E_LOCUS_MISMATCH_419','validation','RNG'])]
            for label_q,q,expect in questions:
                request.append({'role':'user','content':q});req=copy.deepcopy(request);sel=e.select_context(req,conversation_messages=req,incoming_message=req[-1]);ev=event(e);text=dumps(sel)
                quality.append({'name':label_q,'query':q,'expected':expect,'evidence_hits':[x for x in expect if x in text],'all_evidence':all(x in text for x in expect),'exact_prompt_tokens':len(wire_tokens(sel,e.get_tool_schemas())),'selected':sel,'selection':ev})
    result={'label':label,'settings':conf,'rows':rows,'quality':quality,'summary':{'selection_ms':percentiles([r['fast_loop_ms'] for r in rows]),'prompt_tokens':percentiles([r['exact_prompt_tokens'] for r in rows]),'replaced_tokens':percentiles([r['tokens_replaced'] for r in rows[4:]]),'potential_prefill':percentiles([r['potential_prefill_tokens'] for r in rows[4:]]),'jaccard':percentiles([r['source_jaccard'] for r in rows[4:] if r['source_jaccard'] is not None]),'evidence_score':sum(q['all_evidence'] for q in quality),'objective_rate':sum(r['objective_retained'] for r in rows)/len(rows),'group_errors':sum(len(r['group_errors']) for r in rows)}}
    write(f'selection/{label}.json',result);print(label,result['summary'],flush=True);return result

def monitor():
    last={};hz=os.sysconf('SC_CLK_TCK');fd=(OUT/'hardware-monitor.jsonl').open('a')
    while True:
        now=time.time();process=[]
        for p in Path('/proc').glob('[0-9]*'):
            try:
                argv=(p/'cmdline').read_bytes().decode().split('\0')
                if not argv or Path(argv[0]).name!='llama-server':continue
                st=(p/'stat').read_text().rsplit(')',1)[1].split();ticks=int(st[11])+int(st[12]);rss=int(st[21])*os.sysconf('SC_PAGE_SIZE');pid=p.name
                cpu=(ticks-last[pid][1])/hz/(now-last[pid][0])*100 if pid in last else None
                last[pid]=(now,ticks);process.append({'pid':pid,'rss_bytes':rss,'cpu_percent_interval':cpu,'port':argv[argv.index('--port')+1]})
            except (OSError,UnicodeError,ValueError):pass
        gpu=subprocess.run(['nvidia-smi','--query-gpu=index,temperature.gpu,utilization.gpu,memory.used,power.draw','--format=csv,noheader,nounits'],capture_output=True,text=True)
        fd.write(dumps({'ts':now,'processes':process,'gpu':gpu.stdout})+'\n');fd.flush();time.sleep(2)

def main():
    p=argparse.ArgumentParser();p.add_argument('phase',choices=['baseline','baseline2','screen','monitor']);args=p.parse_args()
    if args.phase=='monitor':return monitor()
    path=OUT/'fixtures/history.json';history=json.loads(path.read_text()) if path.exists() else fixture()
    if args.phase in ('baseline','baseline2'):
        tag='baseline-v2' if args.phase=='baseline2' else 'baseline'
        select_run(f'{tag}-current',{**json.loads((OUT/'baseline/defaults.json').read_text()),'selection_policy':'circulator','semantic_policy':'prefix','segmentation_policy':'tool_safe','recall_policy':'fine','compact_source_aliases':False},history)
        for target in (12000,16000,20000,24000,28000,32000,36000,40000):select_run(f'{tag}-moving-{target}',settings(target),history)
        for target in (16000,20000,24000,28000,32000):select_run(f'{tag}-stable-{target}',settings(target,'stable'),history)
    else:
        for target in (12000,16000,20000,24000,28000,32000,36000,40000):select_run(f'coherent-{target}',settings(target,'coherent'),history)
if __name__=='__main__':main()
