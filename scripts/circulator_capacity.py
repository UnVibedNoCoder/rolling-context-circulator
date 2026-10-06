"""Both physical extremes, with exact relay admission and unchanged server args."""
import sys,json,threading,copy,uuid,time
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parent))
from circulator_gauntlet import *
# Run only after another relay campaign has finished.
run('fixed-12000',configuration(12000),repeat=3)
root=OUT/'safe-ceiling';assert not root.exists()
conf=configuration(65536,16000);conf.update(tail_tokens=64512,minimum_tail_tokens=52000,recall_budget_tokens=12000,trigger_tokens=65536)
fixture=json.loads((OUT/'gauntlet/fixture.json').read_text())['messages'];fixture[0]['content']+='\nUnique ceiling gauntlet '+uuid.uuid4().hex
engine=RollingContextEngine(settings={**conf,'state_dir':str(root)},counter=COUNTER);engine.on_session_start('safe-ceiling')
seed=[fixture[0],{'role':'user','content':'calibration'}];over=len(wire_tokens(seed,engine.get_tool_schemas()))-COUNTER.messages(seed);(root/'large-overhead.json').write_text(dumps({'overhead_tokens':over}))
req=fixture+[{'role':'user','content':QUESTION}];rows=[]
with patch('rolling_context.engine.spawn_context_thread',return_value=Blocked()):
    for step in range(24):
        sel=engine.select_context(req,conversation_messages=req,incoming_message=req[-1] if req[-1]['role']=='user' else None)
        count=len(wire_tokens(sel,engine.get_tool_schemas()));rows.append({'step':step,'exact_prompt_tokens':count,'selection':event(engine)})
        if 63600<=count<=64480:break
        cid='capacity-'+str(step);text=dumps(fixture[-10:])
        req += [{'role':'assistant','content':'Inspect another large fixture while maintaining the current task.','tool_calls':[{'id':cid,'type':'function','function':{'name':'read_file','arguments':dumps({'path':f'fixture/capacity_{step}.txt'})}}]}, {'role':'tool','tool_call_id':cid,'content':text[:3000]+f'\nCapacity fixture sequence {step}'},{'role':'user','content':QUESTION}]
    server=relay(root)
    try:
        result=generate(sel,root,tools=engine.get_tool_schemas(),thinking=True,limit=8192,force_json=True)
        write('safe-ceiling/result.json',{'physical_context':73728,'normal_generation_allowance':8192,'safety_margin':1024,'rows':rows,'generation':result,'score':score(result)})
        print('safe ceiling',count,result['usage'],result['gpu_fault'],flush=True)
    finally:server.shutdown();server.server_close()
# Separate tiny-output physical-capacity test (never a 73K + 8K claim).
import subprocess,os
subprocess.run(['/usr/bin/python3',str(ROOT/'scripts/circulator_gauntlet.py'),'ceiling'],check=True,env=os.environ)
