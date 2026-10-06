"""Independent acceptance checks and telemetry accounting for the fixed coding workload."""
import argparse
import collections
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import re
import statistics
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from rolling_context.common import StateStore,dumps,digest


def check_package(package):
    checks={}
    if not package.is_dir():return {'deliverable_exists':False}
    with tempfile.TemporaryDirectory(prefix='astra-acceptance-') as td:
        temp=Path(td);state=temp/'state';store=StateStore(state)
        first={'role':'user','content':'PRIVATE_SENTINEL_8d7c'};other={'role':'user','content':'other'}
        store.snapshot('wanted','canonical',[first,first]);store.snapshot('other','canonical',[other])
        with store.connect() as db:
            db.execute('CREATE TABLE pages (session TEXT,source_id TEXT)')
            db.execute('INSERT INTO pages VALUES (?,?)',('wanted',digest(first)))
            db.execute('INSERT INTO pages VALUES (?,?)',('other',digest(other)))
            db.execute("INSERT INTO jobs(session,created,updated,status,owner,coverage,chunk,trigger_tokens,chunk_tokens) VALUES ('wanted',0,0,'failed',1,'[]','[]',0,0)")
            db.execute("INSERT INTO summaries(session,created,covered,text,tokens,job_id) VALUES ('wanted',0,'[]','{}',1,1)")
        store.generation('wanted','hash',[],0)
        def invoke(session, directory=state):
            return subprocess.run(['/usr/bin/python3','-I','-S',str(package/'hermes-circulator'),
                'history-audit','--state-dir',str(directory),'--session',session,'--json'],
                capture_output=True,text=True,timeout=20)
        before=hashlib.sha256(store.path.read_bytes()).hexdigest()
        r=invoke('wanted')
        try:obj=json.loads(r.stdout)
        except ValueError:obj={}
        checks['offline_no_yaml_no_hermes']=r.returncode==0
        checks['exact_session_counts']=all(obj.get(k)==v for k,v in {'session':'wanted','records':1,
            'pages':1,'generations':1,'summaries':1,'jobs':{'failed':1},'integrity':'ok'}.items())
        checks['no_raw_body_leak']='PRIVATE_SENTINEL' not in r.stdout+r.stderr
        checks['database_bytes_unchanged']=hashlib.sha256(store.path.read_bytes()).hexdigest()==before
        r=invoke('absent')
        try:obj=json.loads(r.stdout)
        except ValueError:obj={}
        checks['absent_session_isolated']=r.returncode==0 and all(obj.get(k)==v for k,v in
            {'records':0,'pages':0,'generations':0,'summaries':0,'jobs':{}}.items())
        missing=temp/'missing';r=invoke('wanted',missing)
        checks['missing_no_creation']=r.returncode!=0 and not missing.exists()
        broken=temp/'broken';broken.mkdir();(broken/'memory.sqlite3').write_bytes(b'invalid sqlite')
        r=invoke('wanted',broken)
        checks['corrupt_clean_error']=r.returncode!=0 and 'Traceback' not in r.stderr
        # Keep a WAL writer connected so the committed newest page remains in WAL.
        with store.connect() as db:
            db.execute('PRAGMA wal_autocheckpoint=0')
            db.execute('INSERT INTO pages VALUES (?,?)',('wanted','wal-new-page'));db.commit()
            r=invoke('wanted')
            try:obj=json.loads(r.stdout)
            except ValueError:obj={}
            checks['committed_wal_visible']=r.returncode==0 and obj.get('pages')==2
        with store.connect() as db:db.execute('DROP TABLE pages')
        r=invoke('wanted')
        try:obj=json.loads(r.stdout)
        except ValueError:obj={}
        checks['legacy_missing_pages']=r.returncode==0 and obj.get('pages')==0
        try:
            manifest=json.loads((package/'install-manifest.json').read_text())['files']
            checks['manifest_hashes']=bool(manifest) and all((package/f).is_file() and hashlib.sha256((package/f).read_bytes()).hexdigest()==h
                                                          for f,h in manifest.items() if f!='install-manifest.json')
        except (OSError,ValueError,KeyError):checks['manifest_hashes']=False
        checks['required_docs']=all((package/f).is_file() for f in ['README.md','START_HERE.md','VALIDATION.md','PACKAGE_REPORT.md'])
        clone=temp/'tampered';shutil.copytree(package,clone,ignore=shutil.ignore_patterns('__pycache__'))
        with (clone/'rolling_context/common.py').open('a') as out:out.write('\n# benchmark tamper detection\n')
        r=subprocess.run(['bash',str(clone/'verify.sh')],cwd=clone,capture_output=True,text=True,timeout=120)
        checks['verify_rejects_tamper']=r.returncode!=0
    return checks


def score(run):
    record=json.loads((run/'run.json').read_text());events=[];messages=[]
    for path,dest in [(run/'state/telemetry.jsonl',events),(run/'transcript.jsonl',messages)]:
        for line in path.read_text().splitlines():
            try:dest.append(json.loads(line))
            except ValueError:pass
    sels=[e for e in events if e['event']=='selection'];completed=[e for e in events if e['event']=='relay_request_completed']
    tools=[e for e in messages if e.get('type')=='tool_use'];calls=collections.Counter(dumps(e.get('input',{}))+'|'+e['name'] for e in tools)
    reads=collections.Counter(e.get('input',{}).get('path') for e in tools if e['name']=='read_file')
    verification_started=time.monotonic()
    checks=check_package(run/'workspace/deliverable')
    verification_seconds=time.monotonic()-verification_started
    fresh=sum(e.get('prompt_n',0) for e in completed);cache=sum(e.get('cache_n',0) for e in completed)
    data={'run':record,'acceptance':checks,'correct':all(checks.values()) and record.get('test_exit_code')==0,
        'counts':dict(collections.Counter(e['event'] for e in events)),
        'stream_event_types':dict(collections.Counter(e.get('type') for e in messages)),
        'provider':{'requests':len(completed),'fresh_tokens':fresh,'cached_tokens':cache,
                    'cache_ratio':cache/(fresh+cache) if fresh+cache else None,
                    'prefill_s':sum(e.get('prompt_ms',0) for e in completed)/1000,
                    'decode_s':sum(e.get('predicted_ms',0) for e in completed)/1000,
                    'output_tokens':sum(e.get('predicted_n',0) for e in completed),
                    'decode_tokens_per_second_weighted':sum(e.get('predicted_n',0) for e in completed)/max(.001,sum(e.get('predicted_ms',0) for e in completed)/1000),
                    'decode_tokens_per_second_median':statistics.median(e['predicted_per_second'] for e in completed if e.get('predicted_per_second')) if completed else None},
        'context':{'max_raw':max((s['raw_history_tokens'] for s in sels),default=0),
                   'max_active':max((s['active_tokens'] for s in sels),default=0),
                   'selection_s':sum(s['fast_loop_ms'] for s in sels)/1000,
                   'evictions':sum(s['evicted_pages'] for s in sels),'recalled_page_selections':sum(s['recalled_pages'] for s in sels)},
        'tools':{'total':len(tools),'identical_repetitions':sum(v-1 for v in calls.values()),
                 'file_read_calls':sum(reads.values()),'repeated_file_paths':sum(v-1 for v in reads.values()),
                 'execution_s':sum(e.get('duration_ms',0) for e in messages if e.get('type')=='tool_result')/1000},
        'limits':'Tool duration sums can overlap. Repeated paths include legitimate chunked reads; not necessarily waste. Reasoning without progress requires additional observation.'}
    data['independent_verification_seconds']=verification_seconds
    data['total_wall_including_independent_verification']=record.get('wall_seconds',0)+verification_seconds
    recalled=collections.Counter(h['source_id'] for s in sels for h in s.get('page_ids',[]) if h.get('reason')=='recall')
    data['context']['repeated_recall_page_selections']=sum(n-1 for n in recalled.values())
    data['context']['deterministic_shed_page_selections']=sum(s.get('deterministic_shed_pages',0) for s in sels)
    data['context']['recall_tool_calls']=sum(e['name'] in ['rolling_history_search','rolling_history_read'] for e in tools)
    data['background']={
        'completed_jobs':[{k:e.get(k) for k in ['chunk_tokens','summary_tokens','duration_seconds','input_tokens','output_tokens']}
                          for e in events if e['event']=='compaction_ready'],
        'failed_jobs':sum(e['event']=='compaction_failed' for e in events),
        'unavailable_chunks':sum(e['event']=='chunk_unavailable' for e in events),
        'max_serialized_page_cache_bytes':max((s.get('page_cache',{}).get('ram_bytes',0) for s in sels),default=0)}
    source=ROOT/'work/astra-original/Rolling Context';copied=run/'workspace/source'
    data['acceptance']['frozen_coding_input_unchanged']=all((copied/p.relative_to(source)).is_file() and p.read_bytes()==(copied/p.relative_to(source)).read_bytes()
                    for p in source.rglob('*') if p.is_file() and '__pycache__' not in p.parts)
    data['correct']=all(data['acceptance'].values()) and record.get('test_exit_code')==0
    samples=[]
    for line in (run/'monitor.jsonl').read_text().splitlines():
        try:samples.append(json.loads(line))
        except ValueError:pass
    def mem(sample,key):
        hit=re.search(r'^'+re.escape(key)+r':\s+(\d+)',sample['meminfo'],re.M)
        return int(hit.group(1))*1024 if hit else 0
    def vm(sample,key):
        hit=re.search(r'^'+re.escape(key)+r'\s+(\d+)',sample['vmstat'],re.M)
        return int(hit.group(1)) if hit else 0
    cpu=[];gpus=collections.defaultdict(list)
    for previous,current in zip(samples,samples[1:]):
        before=list(map(int,previous['cpu_stat'].split()[1:]));after=list(map(int,current['cpu_stat'].split()[1:]))
        delta=[b-a for a,b in zip(before,after)];total=sum(delta[:8])
        if total>0:cpu.append(100*(total-delta[3]-delta[4])/total)
    for sample in samples:
        for line in sample.get('gpus','').splitlines():
            try:
                index,util,memutil,vram,temp,power=[float(value.strip()) for value in line.split(',')]
                gpus[int(index)].append({'util':util,'vram_mib':vram,'temperature_c':temp,'power_w':power})
            except ValueError:pass
    data['system']={'samples':len(samples),'cpu_util_mean_pct':statistics.mean(cpu) if cpu else None,
                    'minimum_available_ram_bytes':min((mem(s,'MemAvailable') for s in samples),default=None),
                    'maximum_swap_used_bytes':max((mem(s,'SwapTotal')-mem(s,'SwapFree') for s in samples),default=None),
                    'swap_in_pages':vm(samples[-1],'pswpin')-vm(samples[0],'pswpin') if samples else None,
                    'swap_out_pages':vm(samples[-1],'pswpout')-vm(samples[0],'pswpout') if samples else None,
                    'gpus':{index:{'mean_util_pct':statistics.mean(s['util'] for s in rows),
                                   'peak_vram_mib':max(s['vram_mib'] for s in rows),
                                   'peak_temperature_c':max(s['temperature_c'] for s in rows)} for index,rows in gpus.items()}}
    dest=ROOT/f'benchmarks/astra-score-{run.name}.json';dest.write_text(json.dumps(data,indent=2)+'\n')
    print(json.dumps({k:v for k,v in data.items() if k not in ['run']},indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('run',type=Path);score(p.parse_args().run)
