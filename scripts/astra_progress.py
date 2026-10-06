"""Compact live trial status, without dumping transcripts or private reasoning."""
import collections
import json
from pathlib import Path
import time

ROOT=Path(__file__).resolve().parents[1]


def progress(run):
    record=json.loads((run/'run.json').read_text());events=[];actions=[]
    telemetry=run/'state/telemetry.jsonl'
    if telemetry.exists():
        for line in telemetry.read_text().splitlines():
            try:events.append(json.loads(line))
            except ValueError:pass
    transcript=run/'transcript.jsonl'
    if transcript.exists():
        for line in transcript.read_text().splitlines():
            try:
                e=json.loads(line)
                if e.get('type')=='tool_use':
                    args=e.get('input',{})
                    detail=args.get('path') or (args.get('command') or '')[:110]
                    actions.append({'name':e['name'],'detail':detail})
            except ValueError:pass
    selections=[e for e in events if e['event']=='selection'];done=[e for e in events if e['event']=='relay_request_completed']
    fresh=sum(e.get('prompt_n',0) for e in done);cache=sum(e.get('cache_n',0) for e in done)
    print(json.dumps({'trial':run.name,'elapsed_s':round(record.get('wall_seconds',time.time()-record['started']),1),
          'finished':'exit_code' in record,'exit_code':record.get('exit_code'),'requests':len(done),
          'active':selections[-1]['active_tokens'] if selections else None,
          'raw':selections[-1]['raw_history_tokens'] if selections else None,
          'cache_ratio':round(cache/(fresh+cache),3) if fresh+cache else None,
          'prefill_s':round(sum(e.get('prompt_ms',0) for e in done)/1000,1),
          'actions':len(actions),'recent_actions':actions[-3:],
          'compactor_events':dict(collections.Counter(e['event'] for e in events if e['event'].startswith('compaction'))),
          'relay_errors':sum(e['event']=='relay_request_error' for e in events)}))


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('label');a=p.parse_args()
    for run in sorted((ROOT/'work/astra-trials').glob(a.label+'-*')):progress(run)
