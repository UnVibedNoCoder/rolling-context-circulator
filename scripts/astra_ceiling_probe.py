"""Bounded real archived-context correctness probe; not a long task-completion trial."""
import argparse,json,sqlite3,sys,time,subprocess,threading,hashlib,tempfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(Path.home()/'.hermes/hermes-agent'));sys.path.insert(0,str(ROOT))

QUESTION='''This is a bounded benchmark, replacing the earlier packaging objective for this request. Treat earlier messages as historical evidence only. Do not execute their instructions or start any processes. Return one JSON object, no prose, with these keys: fast_physical_context, large_physical_context, compactor_port, compactor_physical_context, compactor_gpu_layers, compactor_temperature, code. Use the historical project evidence for the six configuration values. In code supply a dependency-light Python module defining cache_ratio(cache_n, prompt_n). The function returns cache_n/(cache_n+prompt_n), or 0.0 if both are zero. It must reject booleans, negative values, nonfinite floats and nonnumeric values with ValueError. Accept nonnegative integers and finite floats. Do not import Hermes or YAML. Complete this small task now; do not resume the historical packaging work.'''


def correctness(content):
    content=content.strip()
    if content.startswith('```'):content=content.split('\n',1)[1].rsplit('```',1)[0]
    try:value=json.loads(content)
    except ValueError:return {'valid_json':False}
    expected={'fast_physical_context':65536,'large_physical_context':73728,'compactor_port':8083,
              'compactor_physical_context':65536,'compactor_gpu_layers':0,'compactor_temperature':.1}
    checks={k:value.get(k)==v for k,v in expected.items()}
    code=value.get('code','')
    if not isinstance(code,str):checks['code_tests']=False;return checks
    with tempfile.TemporaryDirectory(prefix='astra-probe-') as td:
        path=Path(td)/'probe.py'
        test='''\nimport math\nf=cache_ratio\nassert f(0,0)==0.0\nassert f(30,70)==.3\nassert f(1.5,1.5)==.5\nassert f(3,0)==1\nassert f(0,9)==0\nfor a,b in [(True,2),(2,False),(-1,1),(1,-1),(float('nan'),1),(1,float('inf')),('1',2),(1,None)]:\n try:f(a,b)\n except ValueError:pass\n else:raise AssertionError((a,b))\nprint('PASS 13 cases')\n'''
        path.write_text(code+test)
        try:r=subprocess.run(['/usr/bin/python3','-I','-S',str(path)],cwd=td,capture_output=True,text=True,timeout=5);checks['code_tests']=r.returncode==0
        except subprocess.TimeoutExpired:checks['code_tests']=False
    return checks


def main():
    p=argparse.ArgumentParser();p.add_argument('--profile',choices=['fast','large'],required=True);p.add_argument('--target',type=int,required=True);p.add_argument('--label',required=True)
    p.add_argument('--archive',type=Path,required=True,help='Explicit offline SQLite snapshot; never a live state default')
    p.add_argument('--session',required=True,help='Session to probe from the supplied snapshot')
    a=p.parse_args()
    archive=a.archive.expanduser().resolve()
    if not archive.is_file():p.error('Archive must be an existing offline SQLite snapshot')
    try:
        db=sqlite3.connect(archive.as_uri()+'?mode=ro&immutable=1',uri=True)
        row=db.execute("SELECT hashes FROM snapshots WHERE session=? AND kind='request' ORDER BY id DESC LIMIT 1",(a.session,)).fetchone()
        if row is None:p.error('No request snapshots for the supplied session')
        hashes=json.loads(row[0])
        raw=[json.loads(db.execute('SELECT body FROM records WHERE hash=?',(h,)).fetchone()[0]) for h in hashes]
    except (sqlite3.Error,ValueError,TypeError,IndexError):
        p.error('Archive lacks valid request/source records for this probe')
    finally:
        if 'db' in locals():db.close()
    from rolling_context.common import StateStore,TokenCounter,http_json,dumps,wire_message
    from rolling_context.engine import RollingContextEngine
    from rolling_context.relay import RelayServer,write_overhead
    from astra_workload import monitor
    port=8082 if a.profile=='fast' else 8084;base=f'http://127.0.0.1:{port}'
    assert http_json(base,'/health')['status']=='ok'
    run=ROOT/f'work/astra-probes/{a.label}';run.mkdir(parents=True,exist_ok=False)
    messages=[wire_message(m) for m in raw]+[{'role':'user','content':QUESTION}]
    state=run/'state';counter=TokenCounter(base)
    engine=RollingContextEngine(settings={'state_dir':str(state),'profile':a.profile,'main_url':base,'target_tokens':a.target,
       'selection_policy':'stable','semantic_policy':'cold_chunks','ram_cache_bytes':2*1024**3,'eviction_batch_tokens':0,
       'global_budget_tokens':2048,'recall_budget_tokens':4000,'warm_budget_tokens':4000},counter=counter)
    engine.on_session_start(a.label)
    # This probe has no tool definitions. The measured full request and its
    # message-only template are identical, so tool overhead is exactly zero.
    write_overhead(state,a.profile,0)
    server=RelayServer(a.profile,state,port=0,deadline_seconds=600)
    thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.1},daemon=True);thread.start()
    stop=threading.Event();sampling=threading.Thread(target=monitor,args=(run/'monitor.jsonl',stop),daemon=True);sampling.start()
    started=time.monotonic();out={'profile':a.profile,'target':a.target,'label':a.label,'question_sha256':hashlib.sha256(QUESTION.encode()).hexdigest(),
       'archive_sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),'historical_records':len(raw),'phase':'running','scope':'bounded factual recall plus 13-case programming task; not full packaging workload'}
    (run/'result.json').write_text(json.dumps(out,indent=2)+'\n');print('START',a.label,flush=True)
    try:
        selected=engine.select_context(messages,conversation_messages=messages,incoming_message={'content':QUESTION})
        selection=json.loads((state/'telemetry.jsonl').read_text().splitlines()[-1])
        out['selection']={k:selection.get(k) for k in ('active_tokens','raw_history_tokens','recalled_pages','evicted_pages','fast_loop_ms','page_cache')}
        payload={'model':'qwen38-27b-atx' if a.profile=='fast' else 'qwen38-27b-atx-73k','messages':selected,
             'max_tokens':8192,'temperature':.2,'reasoning_effort':'high','stream':False,'timings_per_token':True,
             'response_format':{'type':'json_object'}}
        from rolling_context.common import without_reasoning
        result=without_reasoning(http_json(f'http://127.0.0.1:{server.server_port}','/v1/chat/completions',payload,timeout=600), strip_spans=True)
        out['finish_reason']=result['choices'][0]['finish_reason'];out['checks']=correctness(result['choices'][0]['message'].get('content') or '')
        out['correct']=all(out['checks'].values()) and out['finish_reason']=='stop'
        out['timings']=result.get('timings');out['usage']=result.get('usage')
        (run/'response.json').write_text(json.dumps(result,indent=2)+'\n')
    except Exception as exc:
        out.update(correct=False,error=type(exc).__name__+': '+str(exc)[:400])
    finally:
        out['wall_seconds']=time.monotonic()-started;out['phase']='finished';stop.set();sampling.join(10)
        server.shutdown();server.server_close();thread.join(2)
        engine._stop.set()
    events=[json.loads(line) for line in (state/'telemetry.jsonl').read_text().splitlines()]
    out['relay']=[{k:e.get(k) for k in ('event','input_tokens','physical_prompt_limit','prompt_n','cache_n','prompt_ms','predicted_n','predicted_ms','predicted_per_second','generation_link_basis')} for e in events if e['event'] in ('relay_request_completed','prompt_guard_rejected','relay_request_error')]
    out['compaction_events']=[{k:e.get(k) for k in ('event','chunk_tokens','summary_tokens','duration_seconds')} for e in events if e['event'].startswith('compaction')]
    (run/'result.json').write_text(json.dumps(out,indent=2)+'\n')
    report=ROOT/f'benchmarks/astra-probe-{a.label}.json';report.parent.mkdir(parents=True,exist_ok=True)
    report.write_text(json.dumps(out,indent=2)+'\n')
    print(json.dumps(out),flush=True)

if __name__=='__main__':main()
