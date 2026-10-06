"""One dense >500K history gauntlet per sparse full-range candidate."""
import sys,json,time,copy,ast,statistics,uuid,argparse,subprocess,threading
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parent))
from circulator_stress import *
from circulator_live import generate,relay
from rolling_context.coherent import coherent_boundaries
from rolling_context.working_set import segment_metrics

QUESTION='''GAUNTLET_GENOME GAUNTLET_PALETTE GAUNTLET_PIPELINE E_NEGATIVE_INPUT_731 campaign_epoch_beta campaign_epoch_alpha.
Return to the earlier genetics workstream. Use the CURRENT verified decisions, the old implementation and error fix, plus the recent offset.
Submit ONLY a JSON object with keys: limit, multiplier, bias, modulus, offset, palette (array), alias, port, context, path, error, rejected, code.
code must be a standalone Python function solve(x) returning a tuple (score, current_clutch_limit, palette_label). The old fixed score formula and negative-input guard still apply; add the recent offset AFTER the modulus. Select palette[score % len(palette)]. This is a NEW function to implement, not an existing def solve to find. The port field is the CPU compactor port associated with alias, not the transport port. No imports or filesystem access. Do not guess missing evidence. You can use rolling_history_search/read if necessary.'''
EXPECTED={'limit':11,'multiplier':7,'bias':19,'modulus':97,'offset':13,'palette':['amber','cobalt','jade'],'alias':'qwen35-4b-compactor','port':8083,'context':65536,'path':'src/shared/Example/ExampleModule.py','error':'E_NEGATIVE_INPUT_731','rejected':'Random.new'}

def build_fixture():
    original=json.loads((OUT/'fixtures/history.json').read_text())
    refs=json.loads((OUT/'fixtures/project-reference.json').read_text());src=list(refs.values())
    history=[original[0],{'role':'user','content':'Persistent invariants: example-project is server authoritative. Pure shared module path src/shared/Example/ExampleModule.py; rejected Random.new. CPU compactor alias qwen35-4b-compactor port 8083 context 65536. Synthetic gauntlet decisions are separate from the read-only real project.'}]
    clusters=[]
    def episode(name,requirement,decision):
        start=len(history)
        history.append({'role':'user','content':name+' requirement: '+requirement})
        for j,source in enumerate(src[:2]):
            cid=name+'-'+str(j)
            history.extend([{'role':'assistant','content':name+' inspect related implementation and type dependency.','tool_calls':[{'id':cid,'type':'function','function':{'name':'read_file','arguments':dumps({'path':f'fixtures/{name}/file_{j}.luau'})}}]}, {'role':'tool','tool_call_id':cid,'content':name+' OLD INSPECTION EVIDENCE\n'+source+'\n'+requirement}, {'role':'assistant','content':name+' interpretation: '+decision}])
        cid=name+'-test'
        history.extend([{'role':'assistant','content':name+' apply correction and run tests.','tool_calls':[{'id':cid,'type':'function','function':{'name':'run_tests','arguments':dumps({'fixture':name})}}]}, {'role':'tool','tool_call_id':cid,'content':name+' failure before correction: '+requirement+'\n'+ '\n'.join(f'{name} independent_test_{j}: verified dependency line_{j}, status check_{j}' for j in range(100))}, {'role':'assistant','content':name+' correction VERIFIED: '+decision+' Tests passed after the fix; no pending failure.'}])
        clusters.append({'name':name,'start':start-1,'end':len(history)-1,'requirement':requirement,'decision':decision})
    episode('GAUNTLET_GENOME','Initial campaign_epoch_alpha clutch_limit=7. multiplier=7, bias=19. Old formula BUG: x*7+19%97. Negative inputs failed with E_NEGATIVE_INPUT_731.','Fixed formula: base=(x*7+19)%97. if x<0 raise ValueError("E_NEGATIVE_INPUT_731"). Use caller randomness only. campaign_epoch_alpha clutch_limit remains 7 until explicitly superseded.')
    # Intervening bulk is real-project-style dumps/test output, with topic breaks.
    body=original[12:]
    thirds=[body[:len(body)//3],body[len(body)//3:2*len(body)//3],body[2*len(body)//3:]]
    for i,part in enumerate(thirds):
        while part and part[0].get('role')=='tool':part=part[1:]
        # Ensure each boundary ends after complete tool groups.
        while part and check_groups(part):part=part[:-1]
        history.append({'role':'user','content':f'Distractor workstream CITY_ASSETS_{i}: inspect copied unrelated fixture assets and local tests.'})
        history+=copy.deepcopy(part)
        if i==0:episode('GAUNTLET_PALETTE','Historical palette=[red,green,blue]. Do not mutate input genotype. Cross-file UI palette lookup depends on score.','CURRENT corrected palette=[amber,cobalt,jade]. Earlier red/green/blue was rejected. palette_label=palette[final_score % len(palette)].')
        if i==1:episode('GAUNTLET_PIPELINE','campaign_epoch_beta supersedes campaign_epoch_alpha clutch_limit. Build depends on validation; validation depends on injected RNG.','CURRENT campaign_epoch_beta clutch_limit=11. KEEP multiplier=7,bias=19,modulus=97 and E_NEGATIVE_INPUT_731 guard. Requirement and old formula still authoritative; only clutch_limit changes.')
    # More than 500K; complete tool clusters, distinct large outputs.
    raw=sum(COUNTER.weights(history[1:]));i=0
    while raw<510000:
        cid='pressure-'+str(i);addition=[{'role':'user','content':f'Topic CITY_DISTRACTOR_{i}: unrelated large asset inspection.'},{'role':'assistant','tool_calls':[{'id':cid,'type':'function','function':{'name':'read_file','arguments':dumps({'path':f'fixture/city_{i}.luau'})}}]}, {'role':'tool','tool_call_id':cid,'content':src[i%len(src)]+ '\n'+ '\n'.join(f'cityAsset_{i}_{j}: mesh region {j}; collider_{(j*13+i)%991}; local status PASS_{j}' for j in range(80))},{'role':'assistant','content':f'CITY_DISTRACTOR_{i} complete. This topic does not revise genetics.'}]
        history+=addition;raw+=sum(COUNTER.weights(addition));i+=1
    history += [{'role':'user','content':'Return to genetics. RECENT CHANGE: offset=13, added AFTER the old fixed modulus. Preserve all current verified earlier decisions.'},{'role':'assistant','content':'Acknowledged recent offset 13, pending final synthesis and executable solve(x).'}]
    write('gauntlet/fixture.json',{'messages':history,'clusters':clusters,'expected':EXPECTED,'raw_history_tokens':sum(COUNTER.weights(history[1:]))});return history,clusters

def configuration(target,segment=16000,elastic=False,policy='coherent'):
    conf={'profile':'large','main_url':URL,'selection_policy':policy,'target_tokens':target,'trigger_tokens':min(65536,target+4000),'tail_tokens':int(target*.6),'minimum_tail_tokens':min(8000,int(target*.35)),'recall_budget_tokens':min(28000,int(target*.45)),'warm_budget_tokens':4000,'chunk_min':4000,'chunk_target':12000,'chunk_max':20000,'summary_max_tokens':1536,'summary_memory_max_tokens':4096,'compact_source_aliases':True,'semantic_policy':'cold_chunks','segmentation_policy':'coherent','segment_max_tokens':segment,'recall_policy':'macro','eviction_batch_tokens':4000}
    if elastic:conf['elastic']={'enabled':True,'floor_tokens':12000,'normal_tokens':24000,'wide_tokens':48000,'ceiling_tokens':64000,'grow_turns':2,'shrink_turns':6,'cooldown_turns':4}
    return conf

def score(result):
    text=result['content'].strip()
    if result['tool_calls']:
        try:text=result['tool_calls'][0]['function']['arguments']
        except (KeyError,IndexError):pass
    try:answer=json.loads(text.removeprefix('```json').removesuffix('```').strip())
    except ValueError:answer={}
    checks={k:answer.get(k)==v for k,v in EXPECTED.items()};code=answer.get('code','');code_tests={};error=None
    try:
        parsed=ast.parse(code)
        banned=(ast.Import,ast.ImportFrom,ast.Attribute,ast.With,ast.ClassDef)
        if any(isinstance(n,banned) for n in ast.walk(parsed)):raise ValueError('Code contains disallowed side effects/imports')
        allowed={'ValueError':ValueError,'len':len,'int':int,'tuple':tuple,'list':list}
        scope={'__builtins__':allowed};exec(compile(parsed,'gauntlet-solution','exec'),scope)
        fn=scope['solve']
        for x in [0,1,2,5,13,97,255]:
            expected_score=(x*7+19)%97+13;expected=(expected_score,11,EXPECTED['palette'][expected_score%3]);code_tests[str(x)]=fn(x)==expected
        try:fn(-1);code_tests['negative']=False
        except ValueError as e:code_tests['negative']=str(e)=='E_NEGATIVE_INPUT_731'
    except Exception as e:error=repr(e)
    return {'answer':answer,'fact_checks':checks,'code_checks':code_tests,'code_error':error,'correct':sum(checks.values())+sum(code_tests.values()),'total':len(EXPECTED)+8,'all_correct':all(checks.values()) and len(code_tests)==8 and all(code_tests.values())}

def run(label,conf,live=True,repeat=0):
    fixture=json.loads((OUT/'gauntlet/fixture.json').read_text());history=copy.deepcopy(fixture['messages']);clusters=fixture['clusters']
    history[0]['content']+='\nCache isolation trial '+uuid.uuid4().hex
    root=OUT/'gauntlet/states'/f'{label}-r{repeat}';assert not root.exists(),root
    e=RollingContextEngine(settings={**conf,'state_dir':str(root)},counter=COUNTER);e.on_session_start(label)
    seed=[history[0],{'role':'user','content':'calibration'}];overhead=len(wire_tokens(seed,e.get_tool_schemas()))-COUNTER.messages(seed);(root/'large-overhead.json').write_text(dumps({'overhead_tokens':max(0,overhead)}))
    rows=[];prev=None;prefix=None;req=[];oldend=0
    weights=COUNTER.weights(history[1:]);cum=[];total=0
    for w in weights:total+=w;cum.append(total)
    ends=[next(i+2 for i,n in enumerate(cum) if n>=threshold) for threshold in (100000,250000,500000)]+[len(history)]
    server=relay(root) if live else None
    start=time.monotonic()
    try:
        with patch('rolling_context.engine.spawn_context_thread',return_value=Blocked()):
            for n,end in enumerate(sorted(set(ends))):
                while end<len(history) and check_groups(history[:end]):end+=1
                req+=copy.deepcopy(history[oldend:end]);oldend=end
                # Hard recall demand accumulates for consecutive boundaries;
                # only final synthesis incurs inference in the broad sweep.
                req.append({'role':'user','content':('Continue the local city fixture.' if n<2 else QUESTION)})
                sel=e.select_context(req,conversation_messages=req,incoming_message=req[-1]);ev=event(e)
                body=[wire_message(m) for m in req[1:]];h=[digest(m) for m in body];w=COUNTER.weights(body)
                groups=e.pages.macro_groups(label,body,h,w,conf.get('segment_max_tokens',16000))
                if conf.get('selection_policy')!='coherent':
                    # Legacy prompts select independent message pages.
                    groups=[{'segment_id':x,'source_ids':[x],'tokens':y} for x,y in zip(h,w)]
                met,prev=segment_metrics(groups,ev['page_ids'],dict(zip(h,w)),prev)
                tok=wire_tokens(sel,e.get_tool_schemas());shared=0
                if prefix:
                    for a,b in zip(prefix,tok):
                        if a!=b:break
                        shared+=1
                prefix=tok;clusters_complete=[]
                represented={p['source_id'] for p in ev['page_ids'] if p.get('source_id')}
                for c in clusters:
                    ids={digest(wire_message(m)) for m in history[c['start']+1:c['end']+1]};hits=ids&represented
                    clusters_complete.append({'name':c['name'],'some':bool(hits),'complete':hits==ids,'represented':len(hits),'total':len(ids)})
                rows.append({'checkpoint':n,'selection':ev,'metrics':met,'clusters':clusters_complete,'exact_prompt_tokens':len(tok),'common_prefix_tokens':shared,'potential_prefill_tokens':len(tok)-shared,'group_errors':check_groups(sel)})
            generations=[];tool_reads=[];duplicate_reads=0
            generation=generate(sel,root,tools=e.get_tool_schemas(),limit=8192,thinking=True) if live and repeat>=2 else generate(sel,root,tools=e.get_tool_schemas(),limit=1536) if live else None
            scoring=score(generation) if generation else None
            if generation:generations.append(generation)
            # Finalist agent loop: actual archive calls, tool results and code
            # validation feedback. Broad screening remains one request.
            if live and repeat:
                for attempt in range(3 if repeat>=2 else 5):
                    if scoring['all_correct'] or generation['gpu_fault']:break
                    calls=generation['tool_calls']
                    req.append({'role':'assistant','content':generation['content'],'tool_calls':calls} if calls else {'role':'assistant','content':generation['content']})
                    if calls:
                        for call in calls:
                            name=call['function']['name']
                            try:args=json.loads(call['function']['arguments'])
                            except ValueError:args={}
                            sig=dumps([name,args]);duplicate_reads+=sig in tool_reads;tool_reads.append(sig)
                            content=e.handle_tool_call(name,args)
                            req.append({'role':'tool','tool_call_id':call['id'],'content':content})
                    else:
                        failed=[k for k,v in scoring['fact_checks'].items() if not v]+[k for k,v in scoring['code_checks'].items() if not v]
                        req.append({'role':'user','content':'Validation failed for fields/cases: '+dumps(failed)+'. Recover exact archived evidence, correct the solution and submit the required JSON. Do not revise fields that already passed.'})
                    forced=repeat>=2 and attempt==2
                    if forced:req.append({'role':'user','content':'The bounded archive-recovery stage is complete. Now create the NEW solve(x) function and return the required JSON from the evidence already present. Use null for any truly missing fact; do not call another tool. The port field belongs to the compactor alias.'})
                    sel=e.select_context(req,conversation_messages=req,incoming_message=req[-1] if req[-1]['role']=='user' else None)
                    generation=generate(sel,root,tools=e.get_tool_schemas(),limit=8192 if repeat>=2 else 2048,thinking=repeat>=2,force_json=forced)
                    generations.append(generation);scoring=score(generation)
                if scoring['answer'].get('code'):
                    (root/'solution.py').write_text(scoring['answer']['code']+'\n')
            if generations:
                first_useful=next((g['observer'].get('first_tool_call_ms') for g in generations if g['tool_calls']),None)
            else:first_useful=None
            result={'label':label,'repeat':repeat,'settings':conf,'rows':rows,'selected':sel,'generation':generation,'generations':generations,'main_model_request_count':len(generations),'archive_reads':tool_reads,'duplicate_retrievals':duplicate_reads,'first_useful_tool_ms':first_useful,'final_selection':event(e),'malformed_groups':check_groups(sel),'score':scoring,'total_elapsed_s':time.monotonic()-start}
            write(f'gauntlet/{label}-r{repeat}.json',result)
            print(label,repeat,'active',rows[-1]['selection']['active_tokens'],'prompt',rows[-1]['exact_prompt_tokens'],'segments',rows[-1]['metrics']['selected_segments'],'fragment',round(rows[-1]['metrics']['fragmentation_score'],2),'score',scoring['correct'] if scoring else 'offline','time',round(result['total_elapsed_s'],2),flush=True)
            return result
    finally:
        if server:server.shutdown();server.server_close()

def main():
    p=argparse.ArgumentParser();p.add_argument('phase',choices=['fixture','segments','coarse','finalists','recovery','ceiling']);a=p.parse_args()
    if a.phase=='fixture':build_fixture();return
    if a.phase=='segments':
        for seg in (3000,6000,10000,16000,20000):run('segment-'+str(seg),configuration(28000,seg),live=False)
    if a.phase=='coarse':
        run('legacy-current',{**json.loads((OUT/'baseline/defaults.json').read_text()),'selection_policy':'circulator','semantic_policy':'prefix','segmentation_policy':'tool_safe','recall_policy':'fine','compact_source_aliases':False})
        for target in (12000,20000,32000,40000,48000,56000,64000):run('fixed-'+str(target),configuration(target))
        run('elastic-full',configuration(24000,elastic=True))
    if a.phase=='finalists':
        for name,conf in [('fixed-20000',configuration(20000)),('fixed-32000',configuration(32000)),('fixed-56000',configuration(56000)),('elastic-full',configuration(24000,elastic=True))]:run(name,conf,repeat=1)
    if a.phase=='recovery':
        for name,conf in [('fixed-32000',configuration(32000)),('fixed-20000',configuration(20000)),('fixed-64000',configuration(64000)),('elastic-full',configuration(24000,elastic=True))]:run(name,conf,repeat=2)
    if a.phase=='ceiling':
        # Separate physical-capacity request, small generation budget. Construct
        # exact input near 72.5K without changing the server's 73,728 context.
        state=OUT/'physical-ceiling';assert not state.exists();state.mkdir();server=relay(state)
        try:
            source=json.loads((OUT/'gauntlet/fixture.json').read_text())['messages'];messages=[source[0],{'role':'user','content':'Capacity experiment. Read the quoted transcript and respond exactly CEILING_OK.'}]
            text=dumps(source[-120:]);part=text
            while len(wire_tokens(messages+[{'role':'user','content':part}]))<72500:part+='\n'+text[:4000]
            while len(wire_tokens(messages+[{'role':'user','content':part}]))>72500:part=part[:-max(10,int((len(wire_tokens(messages+[{'role':'user','content':part}]))-72500)*2))]
            messages+=[{'role':'user','content':part},{'role':'user','content':'Respond exactly CEILING_OK'}];tokens=len(wire_tokens(messages));result=generate(messages,state,limit=64)
            write('physical-ceiling/result.json',{'physical_context':73728,'exact_input_tokens':tokens,'generation_allowance':64,'margin':1024,'generation':result});print('ceiling',tokens,result['content'],result['timings'],flush=True)
        finally:server.shutdown();server.server_close()
if __name__=='__main__':main()
