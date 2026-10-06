"""Validate the actual ready CPU COMPACT block in a subsequent real generation."""
import sys,json,uuid,copy,sqlite3,time
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from circulator_gauntlet import *
root=OUT/'validation-winning';sid='validation-real-cpu'
store=StateStore(root)
with store.connect() as db:
    row=db.execute("SELECT hashes FROM snapshots WHERE session=? AND kind='canonical' ORDER BY id DESC LIMIT 1",(sid,)).fetchone()
    req=[json.loads(db.execute('SELECT body FROM records WHERE hash=?',(h,)).fetchone()[0]) for h in json.loads(row[0])]
req[0]['content']+='\nReady summary validation '+uuid.uuid4().hex
req.append({'role':'user','content':QUESTION})
e=RollingContextEngine(settings={'state_dir':str(root)},counter=COUNTER);e.on_session_start(sid);e._stop.set()
server=relay(root);start=time.monotonic()
try:
    selected=e.select_context(req,conversation_messages=req,incoming_message=req[-1]);ev=event(e)
    result=generate(selected,root,tools=e.get_tool_schemas(),thinking=True,limit=8192)
    s=score(result);gens=[result]
    for attempt in range(3):
        if s['all_correct']:break
        calls=result['tool_calls'];req.append({'role':'assistant','content':result['content'],'tool_calls':calls} if calls else {'role':'assistant','content':result['content']})
        for call in calls:
            req.append({'role':'tool','tool_call_id':call['id'],'content':e.handle_tool_call(call['function']['name'],json.loads(call['function']['arguments']))})
        if not calls:req.append({'role':'user','content':'Now produce the NEW solve implementation and required JSON from exact evidence. Port belongs to CPU compactor.'})
        selected=e.select_context(req,conversation_messages=req,incoming_message=req[-1] if req[-1]['role']=='user' else None)
        result=generate(selected,root,tools=e.get_tool_schemas(),thinking=True,limit=8192,force_json=attempt==2);s=score(result);gens.append(result)
    record={'selection':ev,'final_selection':event(e),'score':s,'generations':gens,'main_requests':len(gens),'malformed_groups':check_groups(selected),'elapsed_s':time.monotonic()-start,'passed':s['all_correct'] and ev['compact_pages']>0 and not check_groups(selected) and not any(g['gpu_fault'] for g in gens)}
    write('validation-winning/summary-generation.json',record);print('READY SUMMARY',record['passed'],s['correct'],'compact',ev['compact_pages'],'active',event(e)['active_tokens'],flush=True)
finally:server.shutdown();server.server_close()
