"""Short high-pressure macro source/output campaign, separate from main inference."""
import json,time,sys,os
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from circulator_stress import OUT,ROOT,COUNTER,RollingContextEngine,write
from rolling_context.common import digest,dumps,wire_message
from rolling_context.engine import safe_boundaries
fixture=json.loads((OUT/'gauntlet/fixture.json').read_text());history=fixture['messages'];results=[]
for i,(size,cap) in enumerate([(8000,1536),(12000,1536),(20000,2048)]):
    body=history[1:];weights=COUNTER.weights(body);end=min(safe_boundaries(body),key=lambda n:abs(sum(weights[:n])-size));chunk=body[:end];hashes=[digest(wire_message(m)) for m in chunk]
    state=OUT/'cpu-macro'/f'source-{size}-out-{cap}';assert not state.exists();e=RollingContextEngine(settings={'state_dir':str(state),'summary_max_tokens':cap,'summary_memory_max_tokens':4096,'compact_source_aliases':True,'job_timeout':600},counter=COUNTER);e.on_session_start('cpu-macro')
    e.store.snapshot('cpu-macro','wire',chunk);e.pages.ingest('cpu-macro',chunk,weights[:end]);now=time.time()
    with e.store.connect() as db:
        jid=db.execute("INSERT INTO jobs(session,created,updated,status,owner,coverage,parent_id,chunk,trigger_tokens,chunk_tokens,coverage_kind) VALUES (?,?,?,?,?,?,?,?,?,?,?)",('cpu-macro',now,now,'queued',os.getpid(),dumps(hashes),None,dumps(chunk),32000,sum(weights[:end]),'source_set')).lastrowid
    start=time.monotonic();e._compact(jid,'cpu-macro');elapsed=time.monotonic()-start
    with e.store.connect() as db:
        job=dict(db.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone());s=db.execute('SELECT * FROM summaries').fetchone();summary=dict(s) if s else None
    telemetry=[json.loads(x) for x in (state/'telemetry.jsonl').read_text().splitlines()]
    expected=['qwen35-4b-compactor','8083','65536','Random.new','7','19','97','E_NEGATIVE_INPUT_731']
    text=summary['text'] if summary else ''
    result={'source_target':size,'source_tokens':sum(weights[:end]),'max_output':cap,'status':job['status'],'error':job['error'],'elapsed_s':elapsed,'telemetry':telemetry,'summary':summary,'literal_fact_hits':[x for x in expected if x in text],'expected_facts':expected,'sources_cited':len({s for items in json.loads(text).values() for item in items for s in item['sources']}) if text else 0,'source_count':len(hashes)}
    results.append(result);write('cpu-macro-results.json',results);print(size,cap,job['status'],round(elapsed,2),'facts',len(result['literal_fact_hits']),flush=True)
