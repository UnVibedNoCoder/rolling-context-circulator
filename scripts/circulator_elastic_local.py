"""Late local-task shrink: same archived history, both policies, real final answer."""
import sys,json,copy,uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from circulator_gauntlet import *
fixture=json.loads((OUT/'gauntlet/fixture.json').read_text())['messages']
results=[]
for label,conf in [('elastic-shrink',configuration(64000,elastic=True)),('fixed-local',{})]:
    root=OUT/label;assert not root.exists();e=RollingContextEngine(settings={**conf,'state_dir':str(root)},counter=COUNTER);e.on_session_start(label);e._stop.set()
    req=copy.deepcopy(fixture);req[0]['content']+='\nLocal comparison '+uuid.uuid4().hex
    seed=[req[0],{'role':'user','content':'calibration'}];over=len(wire_tokens(seed,e.get_tool_schemas()))-COUNTER.messages(seed);(root/'large-overhead.json').write_text(dumps({'overhead_tokens':over}))
    rows=[];previous=None
    for i in range(20):
        req += [{'role':'user','content':f'Local task LOCAL_VALUE = {i}; earlier local values are superseded.'},{'role':'assistant','content':'Local value acknowledged.'},{'role':'user','content':'Continue the local city fixture.'}]
        sel=e.select_context(req,conversation_messages=req,incoming_message=req[-1]);ev=event(e);tokens=wire_tokens(sel,e.get_tool_schemas());shared=0
        if previous:
            for a,b in zip(previous,tokens):
                if a!=b:break
                shared+=1
        rows.append({'turn':i,'target':ev['target_tokens'],'active':ev['active_tokens'],'prompt_tokens':len(tokens),'common_prefix_tokens':shared,'potential_prefill_tokens':len(tokens)-shared,'group_errors':check_groups(sel)})
        previous=tokens
    req.append({'role':'user','content':'What is the most recent LOCAL_VALUE? Reply with the integer only.'})
    sel=e.select_context(req,conversation_messages=req,incoming_message=req[-1]);server=relay(root)
    try:g=generate(sel,root,tools=e.get_tool_schemas(),thinking=False,limit=32)
    finally:server.shutdown();server.server_close()
    r={'label':label,'rows':rows,'generation':g,'correct':g['content'].strip()=='19','selection':event(e)};results.append(r);write('elastic-local-results.json',results);print(label,r['correct'],'targets',[x['target'] for x in rows],'seconds',g['elapsed_s'],flush=True)
