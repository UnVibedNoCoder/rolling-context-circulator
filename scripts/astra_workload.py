"""Sequential real Hermes coding/packaging trials with isolated artifacts and monitoring."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import threading
import time
import sys
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
ORIGINAL=ROOT/'work/astra-original/Rolling Context'

PROMPT='''Complete this coding and packaging task in the current workspace. Work continuously until all requirements have been implemented and verified. Use HIGH reasoning where useful and real terminal/file tools. Do not stop with a plan.

The authoritative input is ./source, a frozen copy of Context Circulator. Create the finished standalone package in ./deliverable (path may contain spaces). Inspect every Python module, all tests, installation scripts, and the operational docs before completing the work; retain their existing architecture and behavior.

Implement a useful offline diagnostic: `hermes-circulator history-audit --state-dir DIR --session SESSION --json`. It must run without a live model, without PyYAML, and without importing Hermes. It must never create DIR or a database when missing; return a nonzero exit with a concise error in that case. Open an existing database read-only. Do not use immutable=1 on a live WAL database: recent committed WAL rows must be visible. Leave all stored data intact. For the requested session return exactly these required JSON keys (extra metadata is fine): session, records, pages, generations, jobs, summaries, integrity. records counts DISTINCT record hashes referenced by that session's snapshots, excluding other sessions. pages/generations/summaries are counts belonging to that session. jobs maps every present status to its count. integrity is the SQLite quick_check result ('ok' on a healthy DB). A nonexistent session returns zero counts and empty jobs, not unrelated history. Handle older valid databases with missing page tables by returning pages=0. Return a clear nonzero error for corrupt/unreadable databases. Never emit raw message bodies or credentials in the report. Human-readable output without --json is also required.

Preserve all RAW data, asynchronous semantic compaction, final admission guard, and current FAST/LARGE routing. Preserve dependency-light common helpers and CLI lazy YAML imports. Preserve tool argument forwarding and owned-process cleanup. Do not alter model/server settings. Never use pkill or killall.

Package requirements: copy clean source only; omit runtime DB/WAL/SHM, telemetry, caches, secrets and model files. Update README, START_HERE, VALIDATION and an accurate PACKAGE_REPORT.md to document the new command, architecture, exact quoted local startup and shutdown commands, dependency requirements, read-only behavior, known limitations, and test evidence. Update install-manifest.json SHA-256 values to match all shipped files except itself. Make verify.sh actually detect a changed manifested file, report failure if tests fail, and run its suite only once. Preserve its offline use. Add regression tests for history-audit, cross-session isolation, WAL visibility, missing and corrupt databases, optional older schema, no-YAML/no-Hermes imports, and manifest validation. Do not weaken existing tests. Run the entire original suite and your new tests from deliverable, test the command in a subprocess on real temporary SQLite fixtures, and verify the manifest. Test the audit's no-write guarantee with before/after data hashes or queries.

Keep ./source unchanged. You may only write inside this current workspace and ordinary temporary files. Do not install globally, change symlinks outside this workspace, modify the live context engine, or start/stop model servers. Do not call the main model endpoint from a tool: your own session is already using its only slot. A separate harness will perform live inference validation. No remote publishing, network downloads or new LLM agents. Read local code as evidence. Finish with the completed deliverable and a concise factual report; distinguish tests that ran from tests not performed.
'''


def monitor(path, stop):
    with path.open('w') as out:
        while not stop.is_set():
            sample={'ts':time.time(),'loadavg':Path('/proc/loadavg').read_text().strip(),
                    'meminfo':Path('/proc/meminfo').read_text(),
                    'vmstat':Path('/proc/vmstat').read_text(),
                    'cpu_stat':Path('/proc/stat').read_text().splitlines()[0]}
            result=subprocess.run(['nvidia-smi','--query-gpu=index,utilization.gpu,utilization.memory,memory.used,temperature.gpu,power.draw',
                                   '--format=csv,noheader,nounits'],capture_output=True,text=True,timeout=8)
            sample['gpus']=result.stdout.strip();sample['gpu_error']=result.stderr.strip()
            out.write(json.dumps(sample)+'\n');out.flush();stop.wait(5)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path,default=ORIGINAL,help='Context engine under test, not coding input')
    p.add_argument('--label',required=True);p.add_argument('--targets',type=int,nargs='+',default=[32000,38000,44000])
    p.add_argument('--budget',type=int,default=3600)
    p.add_argument('--profile',choices=['fast','large'],default='fast')
    p.add_argument('--settings',type=Path,help='Explicit experimental engine overrides; defaults are unchanged')
    p.add_argument('--reset-fast',action='store_true',help='Restart only the exact harness-owned FAST before each trial')
    p.add_argument('--reset-server',action='store_true',help='Restart only the exact harness-owned selected main server')
    a=p.parse_args();runs=[]
    overrides=json.loads(a.settings.read_text()) if a.settings else {}
    if not isinstance(overrides,dict):raise ValueError('settings must be a JSON object')
    for target in a.targets:
        if a.reset_fast and a.profile!='fast':raise ValueError('--reset-fast requires FAST')
        if a.reset_fast or a.reset_server:
            subprocess.run(['/usr/bin/python3',str(ROOT/'scripts/astra_servers.py'),'stop',a.profile],check=True)
            subprocess.run(['/usr/bin/python3',str(ROOT/'scripts/astra_servers.py'),'start',a.profile],check=True)
        port=8082 if a.profile=='fast' else 8084
        health_url=f'http://127.0.0.1:{port}/health'
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(health_url,timeout=3) as health:
            if json.load(health).get('status')!='ok':raise RuntimeError('FAST not healthy; trial not started')
        run=ROOT/f'work/astra-trials/{a.label}-{target}'
        run.mkdir(parents=True,exist_ok=False);workspace=run/'workspace';workspace.mkdir()
        shutil.copytree(ORIGINAL,workspace/'source',ignore=shutil.ignore_patterns('__pycache__'))
        state=run/'state';prompt=run/'prompt.txt';prompt.write_text(PROMPT)
        prep=[str(a.source/'hermes-circulator'),'prepare','--profile',a.profile,'--state-dir',str(state)]
        subprocess.run(prep,check=True,stdout=(run/'prepare.log').open('w'),stderr=subprocess.STDOUT)
        import yaml
        conf=state/f'profiles/{a.profile}/config.yaml';config=yaml.safe_load(conf.read_text())
        config['context']['local_rolling'].update(target_tokens=target,tail_tokens=target-8000)
        config['context']['local_rolling'].update(overrides)
        config['context']['local_rolling']['target_tokens']=target
        # Freeze model sampling and task capabilities; no auxiliary title inference during trials.
        config.setdefault('agent',{})['max_turns']=120
        config.setdefault('terminal',{})['cwd']=str(workspace)
        conf.write_text(yaml.safe_dump(config,sort_keys=False))
        cmd=[str(a.source/'hermes-circulator'),'run','--profile',a.profile,'--state-dir',str(state),'--',
             '--reasoning','high','--toolsets','terminal,file,todo','--max-turns','120','--run-budget',str(a.budget),
             '--oneshot','--format','stream-json','--query-file',str(prompt)]
        stop=threading.Event();thread=threading.Thread(target=monitor,args=(run/'monitor.jsonl',stop),daemon=True)
        thread.start();start=time.time()
        record={'label':a.label,'target':target,'tail':target-8000,'started':start,'command':cmd,
                'engine_source':str(a.source),'coding_source':str(ORIGINAL),
                'prompt_sha256':hashlib.sha256(PROMPT.encode()).hexdigest(),'workspace':str(workspace)}
        record['overrides']=overrides
        record['profile']=a.profile
        record['fast_reset_before_trial']=a.reset_fast
        record['server_records']={name:json.loads((ROOT/f'work/astra-servers/{name}.json').read_text()) for name in (a.profile,'compactor')}
        record['engine_files']={str(f.relative_to(a.source)):hashlib.sha256(f.read_bytes()).hexdigest()
                               for f in a.source.rglob('*') if f.is_file() and '__pycache__' not in f.parts
                               and f.suffix=='.py' and 'work' not in f.relative_to(a.source).parts}
        (run/'run.json').write_text(json.dumps(record,indent=2)+'\n')
        print('START',a.label,target,run,flush=True)
        try:
            with (run/'transcript.jsonl').open('w') as out:
                child=subprocess.Popen(cmd,cwd=workspace,stdout=out,stderr=subprocess.STDOUT)
                record['launcher_pid']=child.pid
                (run/'run.json').write_text(json.dumps(record,indent=2)+'\n')
                failures=0
                try:
                    while child.poll() is None:
                        if time.time()-start>a.budget+180:
                            record['harness_timeout']=True;break
                        try:
                            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(health_url,timeout=3) as health:
                                healthy=json.load(health).get('status')=='ok'
                            failures=0 if healthy else failures+1
                        except (OSError,ValueError):failures+=1
                        if failures>=3:
                            record['outcome']='infrastructure_interrupted';break
                        time.sleep(1)
                finally:
                    if child.poll() is None:
                        child.terminate()
                        try:child.wait(timeout=20)
                        except subprocess.TimeoutExpired:child.kill();child.wait()
                    record['exit_code']=child.returncode
        finally:
            record['wall_seconds']=time.time()-start;stop.set();thread.join(10)
        package=workspace/'deliverable'
        if package.is_dir():
            with (run/'final-tests.log').open('w') as out:
                test=subprocess.run(['/usr/bin/python3','-m','unittest','discover','-s','tests','-v'],
                                    cwd=package,stdout=out,stderr=subprocess.STDOUT,timeout=120)
                record['test_exit_code']=test.returncode
            record['final_tree']={str(f.relative_to(package)):hashlib.sha256(f.read_bytes()).hexdigest()
                                  for f in package.rglob('*') if f.is_file() and '__pycache__' not in f.parts}
        (run/'run.json').write_text(json.dumps(record,indent=2)+'\n')
        runs.append(record)
        (ROOT/f'benchmarks/astra-workload-{a.label}.json').write_text(json.dumps({'runs':runs},indent=2)+'\n')
        print('END',a.label,target,record['wall_seconds'],record['exit_code'],flush=True)
        subprocess.run(['/usr/bin/python3',str(ROOT/'scripts/astra_score.py'),str(run)],check=True,
                       stdout=(run/'independent-score.log').open('w'),stderr=subprocess.STDOUT)
        if record.get('outcome')=='infrastructure_interrupted':
            print('Remaining trials not started: stable FAST unavailable',flush=True);break


if __name__=='__main__':main()
