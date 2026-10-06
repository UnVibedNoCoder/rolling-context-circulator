"""Real CPU summary size/quality and asynchronous isolation benchmark."""
import json,time,sys,copy,os
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parent))
from circulator_stress import OUT,ROOT,COUNTER,RollingContextEngine,write,percentiles
from rolling_context.common import digest,dumps,wire_message,http_json
from rolling_context.engine import safe_boundaries
history=json.loads((OUT/'fixtures/history.json').read_text()) if (OUT/'fixtures/history.json').exists() else None
if history is None:raise RuntimeError('Run fixture first')
results=[]
for i,(size,cap) in enumerate([(2000,1024),(4000,1024),(8000,1536),(12000,2048),(4000,1536),(8000,1536)]):
    body=history[1:];weights=COUNTER.weights(body);ends=safe_boundaries(body);end=min(ends,key=lambda n:abs(sum(weights[:n])-size));chunk=body[:end];hashes=[digest(wire_message(m)) for m in chunk]
    state=OUT/'cpu'/f'source-{size}-out-{cap}-rep-{i}';assert not state.exists();e=RollingContextEngine(settings={'state_dir':str(state),'summary_max_tokens':cap,'job_timeout':600,'selection_policy':'stable'},counter=COUNTER);e.on_session_start('cpu')
    e.store.snapshot('cpu','wire',chunk);e.pages.ingest('cpu',chunk,weights[:end]);now=time.time()
    with e.store.connect() as db:
        jid=db.execute("INSERT INTO jobs(session,created,updated,status,owner,coverage,parent_id,chunk,trigger_tokens,chunk_tokens,coverage_kind) VALUES (?,?,?,?,?,?,?,?,?,?,?)",('cpu',now,now,'queued',os.getpid(),dumps(hashes),None,dumps(chunk),35000,sum(weights[:end]),'source_set')).lastrowid
    start=time.monotonic();e._compact(jid,'cpu');elapsed=time.monotonic()-start
    with e.store.connect() as db:
        job=dict(db.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone());s=db.execute('SELECT * FROM summaries').fetchone();summary=dict(s) if s else None
    telemetry=[json.loads(x) for x in (state/'telemetry.jsonl').read_text().splitlines()]
    expected=['qwen35-4b-compactor','8083','65536','Random','rng.nextInteger','7','8094','E_LOCUS_MISMATCH_419']
    text=summary['text'] if summary else ''
    result={'source_target':size,'source_tokens':sum(weights[:end]),'max_output':cap,'status':job['status'],'error':job['error'],'elapsed_s':elapsed,'telemetry':telemetry,'summary':summary,'literal_fact_hits':[x for x in expected if x in text],'expected_facts':expected,'sources_cited':len({s for items in json.loads(text).values() for item in items for s in item['sources']}) if text else 0,'source_count':len(hashes)}
    results.append(result);write('cpu-results.json',results);print(size,cap,job['status'],round(elapsed,2),'facts',len(result['literal_fact_hits']),flush=True)
