"""Adversarial deterministic recall probes. No model or external file rereading."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]


def main():
    p=argparse.ArgumentParser();p.add_argument('--source',type=Path,default=ROOT/'work/astra-original/Rolling Context');p.add_argument('--label',default='original')
    a=p.parse_args();sys.path.insert(0,str(a.source))
    from rolling_context.common import StateStore,digest,wire_message,dumps
    from rolling_context.pages import PageManager
    old={'role':'assistant','content':'Decision: deploy /srv/project/config.yaml using port 8084 and hash abcdef123456. Old failure: EADDRINUSE.'}
    new={'role':'user','content':'Correction: /srv/project/config.yaml now uses port 8082 and hash fedcba654321. This supersedes the old deployment decision.'}
    results={}
    with tempfile.TemporaryDirectory() as td:
        store=StateStore(td);pages=PageManager(store)
        ids=[digest(wire_message(m)) for m in [old,new]]
        store.snapshot('test','wire',[old,new]);pages.ingest('test',[old,new],[100,100])
        # A hot obsolete page must not become newer factual evidence due to accesses.
        for _ in range(5):pages.state('test',[ids[0]],'RECALL')
        start=time.perf_counter();hits=pages.recall('test','current port /srv/project/config.yaml',set(ids),1000)
        results['current_query']={'latency_ms':(time.perf_counter()-start)*1000,'ordered_ids':[h['source_id'] for h in hits],
                                  'newest_first':bool(hits and hits[0]['source_id']==ids[1])}
        results['exact_old_error']=bool(pages.recall('test','exact EADDRINUSE',set(ids),1000))
        results['numeric_only']=bool(pages.recall('test','8082',set(ids),1000))
        results['hash_lookup']=pages.search_ids('test',ids[0])==[ids[0]]
        results['session_isolation']=pages.recall('other','config',set(ids),1000)==[]
        store.snapshot('test','wire',[new]);pages.ingest('test',[new],[100])
        hits=pages.recall('test','current port /srv/project/config.yaml',set(ids),1000)
        results['edited_history']={'newest_first':bool(hits and hits[0]['source_id']==ids[1]),'hits':hits}
        results['old_raw_still_exact']=store.record('test',ids[0])==old
        results['no_compact']=True
    (ROOT/f'benchmarks/astra-recall-{a.label}.json').write_text(json.dumps(results,indent=2)+'\n')
    print(json.dumps({k:v for k,v in results.items() if k!='edited_history'},indent=2))


if __name__=='__main__':main()
