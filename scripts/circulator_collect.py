"""Produce compact audit tables from persisted campaign evidence; no inference."""
import json,csv,hashlib,statistics,subprocess,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from circulator_stress import ROOT,OUT,percentiles

def read(p):return json.loads(p.read_text())
rows=[]
for p in sorted((OUT/'gauntlet').glob('*-r*.json')):
    x=read(p)
    if not x.get('generation'):continue
    gens=x.get('generations') or [x['generation']]
    final=x.get('final_selection') or x['rows'][-1]['selection']
    row={'candidate':x['label'],'repeat':x['repeat'],'quality':x['score']['correct'],'checks':x['score']['total'],'all_correct':x['score']['all_correct'],'seconds':x['total_elapsed_s'],'requests':len(gens),'archive_calls':len(x.get('archive_reads',[])),'duplicate_calls':x.get('duplicate_retrievals',0),'input_tokens_first':gens[0]['usage'].get('prompt_tokens'),'input_tokens_last':gens[-1]['usage'].get('prompt_tokens'),'prefill_tokens':sum(g['timings'].get('prompt_n',0) for g in gens),'prefill_ms':sum(g['timings'].get('prompt_ms',0) for g in gens),'cached_tokens':sum(g['timings'].get('cache_n',0) for g in gens),'decode_tokens':sum(g['timings'].get('predicted_n',0) for g in gens),'decode_ms':sum(g['timings'].get('predicted_ms',0) for g in gens),'active_tokens':final['active_tokens'],'target_tokens':final['target_tokens'],'tail_tokens':final['tail_tokens'],'segment_count_initial':x['rows'][-1]['metrics']['selected_segments'],'fragmentation_initial':x['rows'][-1]['metrics']['fragmentation_score'],'group_errors':len(x.get('malformed_groups',[]))+sum(len(r['group_errors']) for r in x['rows']),'gpu_fault':any(g['gpu_fault'] for g in gens),'evidence':str(p.relative_to(ROOT))}
    rows.append(row)
with (OUT/'candidate-comparison.csv').open('w',newline='') as f:
    writer=csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader();writer.writerows(rows)
# Interval telemetry is sampled from /proc, never lifetime-average ps %CPU.
mon=[]
for line in (OUT/'hardware-monitor.jsonl').read_text().splitlines():
    try:mon.append(json.loads(line))
    except ValueError:pass
hardware={}
for port in ('8083','8084'):
    ps=[p for r in mon for p in r['processes'] if p['port']==port]
    hardware[port]={'cpu_percent_interval':percentiles([p['cpu_percent_interval'] for p in ps if p['cpu_percent_interval'] is not None]),'rss_bytes':percentiles([p['rss_bytes'] for p in ps]),'samples':len(ps)}
    busy=[p['cpu_percent_interval'] for p in ps if p['cpu_percent_interval'] is not None and p['cpu_percent_interval']>100]
    hardware[port]['busy_cpu_percent_interval']=percentiles(busy)
# CPU text-only fact scoring avoids accidental hits in hexadecimal source hashes.
cpu=[]
for name in ['cpu-results.json','cpu-macro-results.json']:
    for x in read(OUT/name):
        texts='\n'.join(item['text'] for v in json.loads(x['summary']['text']).values() for item in v) if x.get('summary') else ''
        ev=next((r for r in x['telemetry'] if r['event']=='compaction_ready'),{})
        cpu.append({'phase':name,'source_tokens':x['source_tokens'],'cap':x['max_output'],'status':x['status'],'seconds':x['elapsed_s'],'input_tokens':ev.get('input_tokens'),'output_tokens':ev.get('output_tokens'),'stored_tokens':ev.get('summary_tokens'),'fact_hits_text_only':[v for v in x['expected_facts'] if v in texts],'expected_fact_count':len(x['expected_facts']),'raw_tokens_per_second':x['source_tokens']/x['elapsed_s'],'timings':ev.get('timings'),'texts':texts})
stability={k:read(OUT/'stability'/k/'result.json') for k in ['legacy','winner']}
for k,x in stability.items():
    seq=x['rows'][1:];x['summary']['total_potential_prefill_tokens']=sum(r['potential_prefill_tokens'] for r in seq);x['summary']['total_input_tokens']=sum(r['exact_prompt_tokens'] for r in seq)
    x['summary']['rebuilds']=sum(r['selection'].get('prefix_rebuilt',True) for r in x['rows'])
    x['summary']['removed_source_tokens']=percentiles([r['selection'].get('removed_source_tokens',0) for r in seq])
    x['summary']['retained_source_fraction']=percentiles([r['selection'].get('source_token_retained_fraction',0) for r in seq])
changed=[]
for folder in ['rolling_context','tests']:
    before=OUT/'baseline'/folder
    for p in sorted((ROOT/folder).glob('*.py')):
        old=before/p.name
        if not old.exists() or p.read_bytes()!=old.read_bytes():changed.append({'path':str(p),'change':'new' if not old.exists() else 'modified','sha256':hashlib.sha256(p.read_bytes()).hexdigest()})
from rolling_context.defaults import VALIDATED_POLICY
results={'scope':'NEW_ROOT only; controlled example-project gauntlet, not a full example-project build','new_root':str(ROOT),'baseline':read(OUT/'baseline/defaults.json'),'validated_defaults':VALIDATED_POLICY,'hardware':read(OUT/'hardware-before.json'),'telemetry_distributions':hardware,'gauntlet_raw_tokens':read(OUT/'gauntlet/fixture.json')['raw_history_tokens'],'candidates':rows,'cpu':cpu,'stability':{k:v['summary'] for k,v in stability.items()},'final_validation':read(OUT/'validation-winning/result.json'),'ready_summary_validation':read(OUT/'validation-winning/summary-generation-final.json'),'safe_ceiling':read(OUT/'safe-ceiling/result.json'),'physical_ceiling':read(OUT/'physical-ceiling/result.json'),'source_changes':changed,'gpu_fault_samples':sum(r['gpu_fault'] for r in rows),'status':'verification in progress'}
if (OUT/'elastic-local-results.json').exists():results['elastic_local']=read(OUT/'elastic-local-results.json')
# Keep the top-level JSON compact: hardware props/templates and large per-job
# transcripts remain in their source evidence files.
results['hardware']={k:v for k,v in results['hardware'].items() if k!='endpoints'}
for key in ['final_validation','ready_summary_validation','safe_ceiling','physical_ceiling']:
    value=results[key]
    results[key]={'evidence':str((OUT/({'final_validation':'validation-winning/result.json','ready_summary_validation':'validation-winning/summary-generation-final.json','safe_ceiling':'safe-ceiling/result.json','physical_ceiling':'physical-ceiling/result.json'}[key])).relative_to(ROOT)), 'passed':value.get('passed'),'score':value.get('score'),'raw_recovery':value.get('raw_recovery'),'elapsed_s':value.get('elapsed_s'),'exact_input_tokens':(value.get('generation') or {}).get('usage',{}).get('prompt_tokens'),'generation_allowance':value.get('normal_generation_allowance',value.get('generation_allowance'))}
results['metric_notes']={
    'gpu_fault':'Per-generation gpu_fault detects NVRM Xid only. Kernel also contains unattributed NVRM allocation warnings during campaign; see runtime-final.json.',
    'phases':'r0 is reasoning-off single-shot diagnostic with absent tool schemas; r1 has tools but pre-fix retrieval; compare final reasoning-on r2/r3 within phase.',
    'source_churn':'Canonical represented-source token weights, not literal rendered prompt tokens. Token-prefix experiment measures potential prefill separately.',
    'segments':'Early offline metrics primarily describe RAW clusters. Final selection metrics include quoted recall and COMPACT render sizes and filter old identical occurrences.',
    'repeats':'Small sample, one machine and controlled workload. Measured winner, not a universal optimum.',
    'cpu_throughput':'52.5 source tokens/s from one cold 8460-token job; sustainable backlog threshold is inferred, not stationary saturation measured.'}
for name in ['boundary-comparison','import-proof-final','runtime-final']:
    if (OUT/(name+'.json')).exists():results[name]=read(OUT/(name+'.json'))
if (OUT/'final-verify.txt').exists():results['full_suite_output']=(OUT/'final-verify.txt').read_text()
if (OUT/'validation-winning/summary-generation-final.json').exists():
    v=read(OUT/'validation-winning/summary-generation-final.json');results['final_segment_distribution']=v['selection']
if results.get('boundary-comparison') is not None and results.get('full_suite_output','').rstrip().endswith('verify: 7 passed, 0 failed'):
    results['status']='complete'
(ROOT/'ASTRA_CIRCULATOR_RESULTS.json').write_text(json.dumps(results,indent=2)+'\n')
print('RAW',results['gauntlet_raw_tokens']);print('FINALISTS',json.dumps([r for r in rows if r['repeat']>=2],indent=2));print('HARDWARE',hardware);print('STABILITY',results['stability']);print('CHANGES',[x['path'] for x in changed])
