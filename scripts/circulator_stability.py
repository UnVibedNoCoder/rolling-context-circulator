"""Matched small append turns under >500K RAW; measured token-prefix stability."""
import sys,copy,json
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from circulator_gauntlet import *
from rolling_context.defaults import VALIDATED_POLICY
fixture=json.loads((OUT/'gauntlet/fixture.json').read_text())['messages']
for label,conf in [('legacy',{**json.loads((OUT/'baseline/defaults.json').read_text()),'selection_policy':'circulator','semantic_policy':'prefix','segmentation_policy':'tool_safe','compact_source_aliases':False}),('winner',{})]:
    root=OUT/'stability'/label;assert not root.exists();e=RollingContextEngine(settings={**conf,'state_dir':str(root)},counter=COUNTER);e.on_session_start(label);e._stop.set()
    seed=[fixture[0],{'role':'user','content':'calibration'}];over=len(wire_tokens(seed,e.get_tool_schemas()))-COUNTER.messages(seed);(root/'large-overhead.json').write_text(dumps({'overhead_tokens':over}))
    req=copy.deepcopy(fixture)+[{'role':'user','content':QUESTION}];rows=[];prev=None;ids=None
    for n in range(13):
        sel=e.select_context(req,conversation_messages=req,incoming_message=req[-1] if req[-1]['role']=='user' else None);ev=event(e);tok=wire_tokens(sel,e.get_tool_schemas());common=0
        if prev:
            for a,b in zip(prev,tok):
                if a!=b:break
                common+=1
        current={p['source_id'] for p in ev['page_ids'] if p.get('source_id')}
        rows.append({'turn':n,'exact_prompt_tokens':len(tok),'common_prefix_tokens':common,'potential_prefill_tokens':len(tok)-common,'source_jaccard':len(current&ids)/max(1,len(current|ids)) if ids else None,'selection':ev,'groups':check_groups(sel)})
        prev=tok;ids=current;cid='steady-'+str(n)
        req += [{'role':'assistant','content':'Inspect the next local fixture.','tool_calls':[{'id':cid,'type':'function','function':{'name':'read_file','arguments':dumps({'path':f'fixture/steady_{n}.txt'})}}]}, {'role':'tool','tool_call_id':cid,'content':'\n'.join(f'local fixture {n} line {i}: case_{i} verified stable boundary and no changed project invariant' for i in range(35))},{'role':'assistant','content':'Local check complete; earlier requirements remain unchanged.'}]
    write(f'stability/{label}/result.json',{'rows':rows,'summary':{'potential_prefill':percentiles([r['potential_prefill_tokens'] for r in rows[1:]]),'selection_ms':percentiles([r['selection']['fast_loop_ms'] for r in rows[1:]]),'jaccard':percentiles([r['source_jaccard'] for r in rows[1:]])}})
    print(label,'prefill',percentiles([r['potential_prefill_tokens'] for r in rows[1:]]),'rebuilds',sum(r['selection'].get('prefix_rebuilt',True) for r in rows),flush=True)
