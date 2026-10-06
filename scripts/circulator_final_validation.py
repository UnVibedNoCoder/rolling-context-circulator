"""Fresh real LARGE + CPU worker + a queued unavailable worker, >500K RAW."""
import sys,time,json,uuid,copy,threading
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from circulator_gauntlet import *
from rolling_context.defaults import VALIDATED_POLICY
root=OUT/'validation-winning';assert not root.exists()
fixture=json.loads((OUT/'gauntlet/fixture.json').read_text())['messages']
def make(sid,bad=False):
    history=copy.deepcopy(fixture);history[0]['content']+='\nValidation '+sid+' '+uuid.uuid4().hex
    settings={'state_dir':str(root),'profile':'large','main_url':URL}
    if bad:settings['compactor_url']='http://127.0.0.1:65533'
    e=RollingContextEngine(settings=settings,counter=COUNTER);e.on_session_start(sid)
    seed=[history[0],{'role':'user','content':'calibration'}];over=len(wire_tokens(seed,e.get_tool_schemas()))-COUNTER.messages(seed);(root/'large-overhead.json').write_text(dumps({'overhead_tokens':over}))
    req=history+[{'role':'user','content':'Continue the local city fixture.'}]
    initial=e.select_context(req,conversation_messages=req,incoming_message=req[-1]);e._latest_plan=None
    return e,req,initial
A,qa,initial=make('validation-real-cpu');frozen=copy.deepcopy(initial)
B,qb,initial_b=make('validation-unavailable-cpu',True)
B._latest_plan=None
server=relay(root);result={'defaults':copy.deepcopy(VALIDATED_POLICY),'raw_history_tokens':sum(COUNTER.weights(fixture[1:])),'initial_selection':event(A),'initial_bad_selection':event(B),'samples':[],'models':[]}
start=time.monotonic()
def probe(e,req,label):
    req.append({'role':'user','content':QUESTION});selected=e.select_context(req,conversation_messages=req,incoming_message=req[-1]);e._latest_plan=None
    ev=event(e);g=generate(selected,root,tools=e.get_tool_schemas(),thinking=True,limit=8192)
    s=score(g);generations=[g]
    for n in range(3):
        if s['all_correct']:break
        calls=g['tool_calls'];req.append({'role':'assistant','content':g['content'],'tool_calls':calls} if calls else {'role':'assistant','content':g['content']})
        for call in calls:
            args=json.loads(call['function']['arguments']);req.append({'role':'tool','tool_call_id':call['id'],'content':e.handle_tool_call(call['function']['name'],args)})
        if not calls:req.append({'role':'user','content':'Produce the required JSON now; the port is the compactor port. Recover missing evidence through the archive if necessary.'})
        selected=e.select_context(req,conversation_messages=req,incoming_message=req[-1] if req[-1]['role']=='user' else None);e._latest_plan=None
        g=generate(selected,root,tools=e.get_tool_schemas(),thinking=True,limit=8192,force_json=n==2);generations.append(g);s=score(g)
    return {'label':label,'selection':ev,'final_selection':event(e),'generations':generations,'score':s,'malformed_groups':check_groups(selected),'main_requests':len(generations)}
try:
    result['models'].append(probe(A,qa,'working-cpu'));write('validation-winning/result.json',result)
    result['models'].append(probe(B,qb,'unavailable-cpu'));write('validation-winning/result.json',result)
    # Let the real single CPU job and queued refused endpoint finish. Repeated
    # selection observes queue pressure without waiting on the worker.
    deadline=time.monotonic()+600
    n=0
    while (A._worker and A._worker.is_alive() or B._worker and B._worker.is_alive()) and time.monotonic()<deadline:
        qa.append({'role':'user','content':'Continue the local city fixture.'});t=time.monotonic();selected=A.select_context(qa,conversation_messages=qa,incoming_message=qa[-1]);A._latest_plan=None
        result['samples'].append({'elapsed_s':time.monotonic()-start,'selection_s':time.monotonic()-t,'a_status':A.store.job_status(A.session_id),'b_status':B.store.job_status(B.session_id),'selection':event(A),'malformed_groups':check_groups(selected)})
        write('validation-winning/result.json',result);time.sleep(5);n+=1
    A._stop.set();B._stop.set()
    with A.store.connect() as db:result['jobs']=[dict(r) for r in db.execute('SELECT * FROM jobs')];result['summaries']=[dict(r) for r in db.execute('SELECT * FROM summaries')]
    result['frozen_snapshot_unchanged']=initial==frozen
    old=next(m for m in fixture if m.get('role')=='tool');h=digest(wire_message(old));parts=[];offset=0
    while True:
        page=json.loads(A.handle_tool_call('rolling_history_read',{'source_id':h,'offset':offset,'max_chars':12000}));parts.append(page['untrusted_source'])
        if page['next_offset'] is None:break
        offset=page['next_offset']
    result['raw_recovery']={'source_id':h,'pages_read':len(parts),'chars':sum(map(len,parts)),'exact':' '.join([])=='' and ''.join(parts)==dumps(wire_message(old)),'canonical_exact':A.store.record(A.session_id,h)==wire_message(old),'source_outside_hot_tail':h not in {p['source_id'] for p in event(A)['page_ids'] if p.get('reason')=='coherent_recent'}}
    # Semantic blocks enter only at a subsequent epoch. Explicitly start a new
    # selection epoch in this same durable session; no inference is underway.
    A._stable_frame=None
    sel=A.select_context(qa,conversation_messages=qa,incoming_message=qa[-1]);result['post_summary_selection']=event(A);result['post_summary_group_errors']=check_groups(sel)
    result['elapsed_s']=time.monotonic()-start
    result['passed']=all(x['score']['all_correct'] and not x['malformed_groups'] and not any(g['gpu_fault'] for g in x['generations']) for x in result['models']) and result['frozen_snapshot_unchanged'] and result['raw_recovery']['exact'] and result['raw_recovery']['source_outside_hot_tail'] and any(j['status']=='ready' and j['session']==A.session_id for j in result['jobs']) and any(j['status']=='failed' and j['session']==B.session_id for j in result['jobs'])
    write('validation-winning/result.json',result);print('FINAL E2E',result['passed'],'scores',[x['score']['correct'] for x in result['models']],'jobs',[(j['session'],j['status'],j['chunk_tokens']) for j in result['jobs']],'RAW',result['raw_recovery'],flush=True)
finally:
    A._stop.set();B._stop.set();A._latest_plan=None;B._latest_plan=None;server.shutdown();server.server_close()
