"""Fixed-target diagnostic on real archived requests. NOT a task-completion benchmark."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
import time
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(Path.home()/'.hermes/hermes-agent'))


class BlockedWorker:
    def start(self):pass
    def is_alive(self):return True


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,default=ROOT)
    parser.add_argument('--label',default='original')
    parser.add_argument('--archive',type=Path,required=True,help='Explicit offline SQLite snapshot; never a live state default')
    parser.add_argument('--session',required=True,help='Session to replay from the supplied snapshot')
    args=parser.parse_args();sys.path.insert(0,str(args.source))
    archive=args.archive.expanduser().resolve()
    if not archive.is_file():parser.error('Archive must be an existing offline SQLite snapshot')
    try:
        db=sqlite3.connect(archive.as_uri()+'?mode=ro&immutable=1',uri=True);db.row_factory=sqlite3.Row
        snapshots=list(db.execute("SELECT * FROM snapshots WHERE session=? AND kind='request' ORDER BY id",(args.session,)))
        if not snapshots:parser.error('No request snapshots for the supplied session')
        chosen=snapshots[-4:]
        messages=[[json.loads(db.execute('SELECT body FROM records WHERE hash=?',(h,)).fetchone()[0])
                   for h in json.loads(s['hashes'])] for s in chosen]
    except (sqlite3.Error,ValueError,TypeError,IndexError):
        parser.error('Archive lacks valid request/source records for this replay')
    finally:
        if 'db' in locals():db.close()
    from rolling_context.common import StateStore,TokenCounter,dumps,http_json
    from rolling_context.engine import RollingContextEngine
    counter=TokenCounter('http://127.0.0.1:8082');results=[]
    output=ROOT/f'benchmarks/astra-fixed-replay-{args.label}.json'
    output.parent.mkdir(parents=True,exist_ok=True)
    for sizing in ['target_only','scaled_tail']:
        for target in [32000,36000,38000,40000,42000,44000]:
            state=ROOT/f'work/astra-replay-{args.label}/{sizing}-{target}'
            if state.exists():raise RuntimeError('Fresh output directory required')
            state.mkdir(parents=True)
            tail=24000 if sizing=='target_only' else target-8000
            engine=RollingContextEngine(settings={'state_dir':str(state),'profile':'fast',
                'main_url':counter.base_url,'target_tokens':target,'tail_tokens':tail,
                'minimum_tail_tokens':18000,'overhead_reserve':5725},counter=counter)
            engine.on_session_start('replay');previous=None;rows=[]
            with patch('rolling_context.engine.spawn_context_thread',return_value=BlockedWorker()):
                for snapshot,request in zip(chosen,messages):
                    started=time.monotonic()
                    selected=engine.select_context(request,conversation_messages=request)
                    elapsed=time.monotonic()-started
                    event=json.loads((state/'telemetry.jsonl').read_text().splitlines()[-1])
                    prompt=http_json(counter.base_url,'/apply-template',{'messages':selected})['prompt']
                    tokens=http_json(counter.base_url,'/tokenize',{'content':prompt,'add_special':True,'parse_special':True})['tokens']
                    shared=0
                    if previous:
                        for a,b in zip(previous,tokens):
                            if a!=b:break
                            shared+=1
                    previous=tokens
                    first_user=next(m for m in request if m['role']=='user')
                    rows.append({k:event.get(k) for k in ['active_tokens','raw_history_tokens','recalled_pages',
                        'evicted_pages','deterministic_shed_pages','tail_tokens','tail_goal','fast_loop_ms']} |
                        {'snapshot_id':snapshot['id'],'elapsed_s':elapsed,'common_prefix_tokens':shared,
                         'original_task_exactly_live':first_user in selected,'selected_messages':len(selected)})
            record={'sizing':sizing,'target':target,'tail_limit':tail,'boundaries':rows}
            results.append(record)
            output.write_text(json.dumps({'kind':'archived-request selector screening; zero inference requests',
                'compactor':'blocked deliberately; no semantic gate','results':results},indent=2)+'\n')
            print(sizing,target,'active',rows[-1]['active_tokens'],'tail',rows[-1]['tail_tokens'],
                  'prefix',rows[-1]['common_prefix_tokens'],'objective',rows[-1]['original_task_exactly_live'],flush=True)


if __name__=='__main__':main()
