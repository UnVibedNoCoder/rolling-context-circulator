"""Render the campaign handover from its immutable result files."""
import json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];OUT=ROOT/'work/synthetic-campaign'
r=json.loads((ROOT/'ASTRA_CIRCULATOR_RESULTS.json').read_text())
v=json.loads((OUT/'validation-winning/summary-generation-final.json').read_text());m=v['selection']
finalists=[x for x in r['candidates'] if x['repeat']>=2]
table='\n'.join(f"| {x['candidate']} (r{x['repeat']}) | {x['quality']}/20 | {x['input_tokens_first']:,} | {x['seconds']:.2f} | {x['requests']} | {x['archive_calls']} | {x['prefill_ms']/1000:.2f} |" for x in finalists)
changes='\n'.join('- `'+x['path']+'` — '+x['change'] for x in r['source_changes'])
scripts='\n'.join('- `'+str(p)+'`' for p in sorted((ROOT/'scripts').glob('circulator_*.py')))
report=f'''# Astra Context Circulator stress report

2 October 2026. Implementation under test: `/path/to/rolling-context` (NEW_ROOT).

## Decision

**Use fixed 32,000 target / 36,000 circulation trigger, with append-stable selection epochs and coherent macrosegment recall. Keep elasticity disabled by default.** This is the best measured engineering balance on this machine and gauntlet: all facts and executable checks passed, no archive rereads in either decisive 32K trial, and less work than the large/elastic candidates. It is not a universal optimum.

The winning defaults are installed in `rolling_context/defaults.py`, used by the engine and newly generated profile configurations. Existing external profiles, installed launcher, Hermes, server settings and project files were not changed. The installed `~/.local/bin/hermes-circulator` still resolves to the old tree: use the NEW_ROOT launcher explicitly.

| Recommended setting | Value and purpose |
|---|---|
| Selection / semantic / recall policies | `coherent` / `cold_chunks` / `macro` |
| Fixed target / trigger | 32,000 / 36,000, including engine overhead reserve |
| Preferred / minimum recent tail | 19,200 / 8,000; whole clusters take precedence |
| Source macrosegment soft maximum | 16,000; natural user/task and complete tool-cycle boundaries |
| Automatic recall budget | 14,400; actual prompt headroom remains authoritative |
| Warm COMPACT / original-objective budgets | 4,000 / 2,048 |
| CPU source minimum / target / maximum | 4,000 / 8,000 / 12,000 |
| CPU output / canonical summary memory caps | 1,536 / 4,096 (expanded provenance costs extra tokens) |
| Queue | One pending/running job per session; shared-state CPU lock; durable cold backlog |
| Worker deadline / retry policy | 600 seconds; existing 30-second cooldown and bounded retries |
| Elastic default | Disabled |
| Ordinary physical prompt admission | At most 64,512 with 8,192 output + 1,024 margin |
| Near-physical experiment | 72,474 actual input + 64 output + 1,024 margin; test only |

32K is a target, not a padded minimum. Coherent retention may operate below it or grow toward the 36K epoch trigger. An oversized protected atom may exceed that trigger; the relay still enforces physical capacity. No in-flight KV or generation mutation occurs.

## Baseline and scope proof — MEASURED

The original NEW_ROOT defaults were 35K target, 40K trigger, 24K preferred / 18K minimum tail, 8/10/12K source chunks, 2,048 CPU output, 4K warm and 4K recall budgets. RAW storage was per message. Default selection moved its window; an optional stable selector and narrow elastic controller also existed.

Baseline: 63 unit tests passed. `work/synthetic-campaign/baseline/` preserves the initial source, settings, tests and import proof. Only NEW_ROOT was executed as the Circulator. The installed Hermes ContextEngine interface was imported read-only with bytecode disabled; no old Circulator code was used.

Final import paths for engine, relay, scheduling/compactor, CLI, pages, selection, controllers, common, coherent policy and metrics are recorded in `import-proof-final.json`. All resolve beneath NEW_ROOT. SHA-256, length, mtime and mode comparison found **zero changes** in the protected old tree (29 files), example-project (8,199), Hermes (78,361), and ai-stack (one launch script). The additional historical launch directory did not exist. ai-stack embeds the physical launch settings; its hash and the live argv/environment identity were unchanged. These proofs are `boundaries-before.json`, `boundaries-after.json`, `boundary-comparison.json`, and `runtime-final.json`.

## Hardware, server state, GPU observations — MEASURED

- Intel Core i9-14900K, 32 logical CPUs.
- LARGE `127.0.0.1:8084`, `qwen38-27b-atx-73k`, physical context 73,728, two RTX 3080 10 GB operating as one model.
- CPU `127.0.0.1:8083`, `qwen35-4b-compactor`, physical context 65,536, zero GPU layers, temperature 0.1, reasoning off.
- Test relay `:8094`, created and closed by each harness; all durable state under NEW_ROOT.
- No changes to tensor split, layers, KV types, batch/ubatch, CUDA-graph flag, model files or llama.cpp. Complete `/props`, argv and relevant environment flags are in `hardware-before.json`; final identity comparison passed.

The servers were initially offline and were started with the existing ai-stack profile. The first short-lived execution did not keep them available; a persistent campaign parent then kept the existing servers alive. At completion the two campaign-owned processes were checked against recorded executable, exact argv, start time, environment and idle slots, then individually stopped through pidfds, restoring the initial offline state. The sampler was also stopped.

**No NVIDIA Xid was observed.** The kernel did contain 23 NVRM allocation/error lines at about 09:06 BST, including `NV_ERR_NO_MEMORY`. Attribution is not established; these are not treated as Circulator correctness failures or hidden as a clean kernel. No recorded model request failed in association with them. The harness field `gpu_fault` detects **Xid only**. Full kernel output is retained in `runtime-final.json` and per-generation records.

## Method and evidence

All evidence paths below are relative to `work/synthetic-campaign/` unless otherwise stated. Raw JSON/JSONL, original failed trials and CSV are retained.

1. Baseline append replay at approximately 50/100/200/400K RAW, plus exact-token working-set screening. The first `baseline-screen.log` used reset snapshots and is diagnostic only; `baseline-v2-screen.log` is the corrected append replay.
2. A hard gauntlet rapidly injects **{r['gauntlet_raw_tokens']:,} RAW tokens**, with checkpoints around 100/250/500K. It combines read-only example-project example-project source, controlled coding episodes, huge tool output, old formula errors and fixes, superseded clutch/palette decisions, unrelated city work, exact paths/errors, and a final synthesis requiring distant current facts.
3. The answer is scored as 12 exact fact checks plus eight executable Python tests. Negative input must raise the exact old error; the generated function must combine the old repaired formula, recent offset, current clutch limit and corrected palette. No import/attribute execution is allowed by the evaluator.
4. Coarse fixed targets: 12, 20, 32, 40, 48, 56, 64K, plus broad elastic and legacy. Earlier tokenizer-only screening also covers 16/24/28/36K. Fine comparisons add 28K and a smaller-segment 28K candidate. Only promising regions receive later live trials.
5. Matched source-segment screens: 3/6/10/16/20K soft maxima at the same 28K target. Natural boundaries and atomic groups override exact token sizes.
6. CPU original-citation jobs and coherent short-citation jobs; final fresh real CPU plus unavailable-worker integration; exact RAW pagination; ready-summary generation; ordinary and physical capacity probes.

Fresh state is used for each candidate; unique initial prompt markers reduce accidental main prefix-cache advantage. Actual llama.cpp cache/prefill counts are retained. CPU cache reuse varied and is explicitly reported. Temperature/seed do not establish universal reproducibility on this hardware; these are controlled small-sample engineering trials.

**Phase caveat:** r0 had reasoning off and the harness initially omitted actual tool schemas. Its model requests for recovery are counted as incomplete single-shot answers, not proof every fact was wrong. r1 supplied tools but exposed the old retrieval ranking/self-retrieval loop. r2/r3 use corrected retrieval, real tools and reasoning on, with bounded recovery. Compare timings within this final phase; do not pool all phases into one latency distribution. Offline substring presence is evidence availability, not model correctness.

## Strongest live candidates — MEASURED

| Candidate | Score | First actual input | Whole task, s | Requests | Archive calls | Total prefill, s |
|---|---:|---:|---:|---:|---:|---:|
{table}

Fixed32K repeated: median **56.50 seconds**, range51.71–61.29; two of two complete in one request without archive calls. At this sample size p95 is the slower observation, not a trustworthy population tail estimate. The final installed-policy ready-summary run separately passed20/20 in **{v['elapsed_s']:.2f}s**, one request.

Small12K and20K can recover correctly, but need retrieval. The12K recovery almost rebuilt the prompt (13,918 new prefill tokens and only565 cached on its second request). The20K second request retained19,035 cached tokens and added1,166. Large64K and elastic preserve enough evidence but spend substantially more on prefill without a quality improvement here. Near-full context did not beat the medium working set.

Baseline live probes exposed a wrong current clutch limit and confused port; the optional old stable selector missed newly requested old evidence between rebuilds. This is why the final implementation changes demand-driven recall rather than merely shrinking the target.

## Segmentation, continuity and stability — MEASURED

RAW remains exact, durable and fine-indexed. Prompt recall now assembles larger chronological source clusters, keeping calls, all results, and immediate interpretation together. User boundaries separate episodes; the16K ceiling is soft and never splits an atomic tool group. Large archived dumps can appear as labelled excerpts with character offsets and source IDs; this is not a claim that the full source text remains in the prompt. Exact RAW is always readable. No unrelated topic is merged to hit a size target; no padding is added.

At target28K the3/6/10/16/20K source-unit screen selected17/13/11/10/10 unique units respectively. The20K ceiling brought no count advantage over16K. In the matched live28K comparison,16K source clusters completed in74.19s versus82.20s with6K clusters; both required two requests and two archive calls. This is modest evidence for coherence, not a large statistical claim.

Final installed-policy selection: **{m['active_tokens']:,} engine active tokens**, **{m['tail_tokens']:,} tail**, **{m['raw_history_tokens']:,} RAW history**. It represented12 unique source units through **{m['measured_display_segments']} selected display occurrences**: {m['raw_segment_count']} RAW occurrences plus {m['recall_segment_count']} quoted recall/COMPACT units. Repeated tiny acknowledgement/question occurrences explain why occurrence count differs from unique-unit count.

| Rendered/source-weighted display size metric | Tokens or fraction |
|---|---:|
| Mean | {m['segment_mean_tokens']:.1f} |
| Median | {m['segment_median_tokens']} |
| p10 / p90 | {m['segment_p10_tokens']} / {m['segment_p90_tokens']} |
| Minimum | {m['segment_min_tokens']} |
| Token fraction in units below1K | {m['fraction_active_in_segments_under_1k']:.2%} |
| Token fraction in units below2K | {m['fraction_active_in_segments_under_2k']:.2%} |
| Token fraction in units below4K | {m['fraction_active_in_segments_under_4k']:.2%} |

Thus16K source clusters do **not** mean every rendered quote is16K. Most token mass is in substantial units, while genuine short user decisions remain short. Wrapper/system/tool-schema tokens are excluded from segment-size fractions. Earlier offline distributions were RAW-only; the final production metrics include quoted units and filter old identical occurrences, so those distributions should not be pooled indiscriminately.

The fragmentation diagnostic is:

`100 × (0.30 × token fraction below2K + 0.25 × (1 − source Jaccard) + 0.20 × removed/previous source tokens + 0.15 × partial RAW cluster fraction + 0.10 × duplicate-ledger fraction)`.

Underlying fields include segment size quantiles, replaced units, retained/new/removed source-token weights, retained fraction, Jaccard, recall units, partial RAW clusters and duplicate ledger rows. The ledger metric does not detect every possible textual paraphrase overlap. Cross-file semantic coherence is assessed by the actual gauntlet rather than a perfect semantic cluster oracle. Large windows can score better (roughly3.7 versus11 for a32K checkpoint) while taking longer; the score does not choose the winner.

In a separate matched13-boundary append experiment above500K RAW, about822 tokens arrive each turn:

| Exact-token prefix / selection measure | Legacy | Winner |
|---|---:|---:|
| Potential new prefill p50 | 28,025 | 822 |
| Potential new prefill p95 | 28,340 | 31,571 |
| Total potential prefill across12 appends | 228,267 | 70,510 |
| Epoch rebuilds / boundaries | 13/13 | 3/13 |
| Selection latency p50 / p95, ms | 684 / 1,025 | 253 / 480 |
| Source Jaccard p50 | 0.951 | 0.971 |

This is a tokenizer-prefix experiment, not timed inference on every append. Actual cache reuse depends on server state. Routine append stability improves sharply; a rare full epoch rebuild still has a larger p95 cost. Winner retained-source fraction median1.0, minimum0.927; removed-source tokens median0, p954,425. These are canonical source weights, not literal rendered-token replacement. Legacy lacked those new fields; zero placeholders in the collector are unavailable data, not evidence of zero legacy churn.

## CPU compactor and queue — MEASURED and INFERRED

Original full-SHA-citation trials:2K/1,024-output ready82.56s;4K/1,024 ready63.54s;8K/1,536 truncated140.01s;12K/2,048 truncated204.28s; repeated4K/1,536 ready49.34s;8K/1,536 ready133.58s. Different coverage/cache states prevent a simple speed ranking.

With short source aliases expanded and validated back to exact SHA IDs:

| Actual coherent source | Input / output tokens | Time | Quality observation |
|---|---:|---:|---|
| 8,460 | 8,759 / 1,130 | 161.06s | All8 literal target facts in text; cold prefill |
| 12,811 | 13,143 / 834 | 127.25s | Four of8 facts;8,243 cached input tokens; mixed-topic omissions |
| 21,977 | 22,384 / 610 | 234.52s | Five of8 facts;12,627 cached; lost named invariants |

The cold8.46K trial prefills at94.21 tokens/s and decodes at16.65 tokens/s. Its1,130-token aliased output expands to2,167 canonical stored tokens, explaining the separate4,096 memory cap. This demonstrates provenance overhead reduction, not a controlled runtime speedup. Text-only fact scoring avoids hits inside hashes; small numeric matches remain weak evidence. Valid citations do not prove faithful summaries: even the successful production summary contains imprecise causal prose. Exact RAW and later superseding evidence remain authoritative.

**Recommendation:** independent cold coherent episodes near8K, minimum4K, maximum12K; retain only useful nonoverlapping summaries. Do not batch unrelated episodes to hit the target. Larger sources reduced summary count but lost important facts and did not establish a throughput win. Oversized atomic groups stay RAW rather than being split merely for the CPU.

**Measured service rate:**8,460/161.06 ≈52.5 source tokens/s (0.37 jobs/minute) for the cold trial. Final8,457-token production job took124.99s under different cache/concurrency conditions, about67.7 source tokens/s. **Inferred planning threshold:** sustained eligible cold ingress above roughly50–70 tokens/s will outrun this measured service range; this is not a proven stationary maximum. At500K, processing all cold history would take hours and is not the goal. Schedule useful cold coverage, keep the rest durable and searchable.

CPU interval samples (one core=100%) while above100%: median1,494%, p952,522%, peak2,823%; CPU-server RSS peak5.46GiB. LARGE process RSS peaked14.90GiB. The full distributions include idle periods separately; they are not lifetime `ps %cpu`. Raw two-second CPU/RAM/GPU samples are in `hardware-monitor.jsonl`.

One pending/running job per session keeps the submitted queue bounded for that session. A file lock serializes workers sharing the state directory, with live slot checks before CPU submission. Durable eligible cold material is the latent backlog. There is no new global queue cap across unlimited sessions, no FIFO fairness guarantee, and no multi-hour saturation/drain benchmark. A transaction rechecks pending/source coverage before enqueueing, preventing duplicate pending work. Existing cooldown/retry bounds remain; a stopped engine cannot recursively drain jobs.

Final pressure test had a real CPU job plus a second session queued on the same lock. The queued session reported75.80s oldest age; its deliberately unavailable endpoint eventually failed. Main selection remained386.9ms while queued and459.4ms while the CPU was active, and both tasks scored20/20. The worker did not catch up with all500K history, and was not required to: circulation and exact recall continued independently.

## Elasticity — decision and limitations

The tested opt-in policy spans12K floor,24K normal,48K wide and64K ceiling; grow after two pressure observations, shrink after six lower-demand observations, four-turn cooldown, duplicate boundaries ignored. Unit regression also exercises a28K normal tier and proves64→48→28→12 shrink without oscillation. Demand uses recalled source-cluster size and protected atom size, not just a token-count timer.

The hard task expanded24→64K, scored20/20, and took94.33s versus51.71–61.29s fixed32K. A subsequent local-task experiment starting64K shrank to48K after six observations and then **stayed48K**, because relevance demand remained broad. It answered correctly in43.29s versus33.07s fixed32K. The final local query itself caused additional recall. Therefore integrated elasticity did not reliably return to the floor on this history, although the controller's full-range mechanics work.

**Keep fixed32K as the default.** The optional tiers are an explicit experimental policy, not a claimed elastic optimum. Do not enable them merely because they can expand. This rejection is supported by extra prefill and sticky high demand without a quality gain.

## Capacity and final end-to-end verification — MEASURED

Ordinary ceiling: **64,298 actual input tokens**,8,192 generation allowance,1,024 margin, physical73,728. All20 checks passed; actual output2,217; prefill71.694s at896.84 tokens/s; decode44.684s at49.59 tokens/s.

Physical experiment: **72,474 actual input**,64 output allowance,1,024 margin. Reply `CEILING_OK`; prefill82.283s at880.79 tokens/s. Template-only planning counted72,510 (36-token template-kwargs difference); actual server usage is authoritative. This does not claim73K input plus8K output fits.

Fresh winning-state validation above511K RAW passed:

- Real LARGE + CPU job ready,20/20 main checks; independent unavailable CPU session also20/20.
- Exact archived12,494-character record recovered in two pages, equal to the canonical original, outside the hot tail.
- Initial selected snapshot unchanged while the worker ran; ready summary entered at a later boundary.
- Real subsequent generation containing the committed summary passed20/20. After the final telemetry correction, another real generation again passed20/20, one request, active31,077/tail18,670, nine cited sources in one ready COMPACT block.
- No malformed call/result groups in the tested selections; no Xid.
- Full final suite: **73 tests pass**; `verify.sh`: **7 checks pass,0 failures**. Exact captured output: `final-verify.txt`. Baseline63; nine policy regressions plus one telemetry-occurrence regression add10.

## Changes and rollback

Source/tests changed or added (all absolute paths beneath NEW_ROOT):

{changes}

Policy changes: append-stable epochs and boundary recall; coherent source clusters with fine indexing; BM25-first relevance where requested; exclusion of memory-tool query/results from recall to prevent loops; validated short CPU source aliases; independent cold scheduling with duplicate coverage checks; queue-age and fragmentation metrics; centralized installed defaults. Legacy moving/stable code remains available.

Additional modified deliverables: `README.md`, `START_HERE.md`, `VALIDATION.md`, `install-manifest.json`. New top-level deliverables: this report and `ASTRA_CIRCULATOR_RESULTS.json`. New campaign harnesses:

{scripts}

All generated fixtures, databases, copied baseline, temporary test directories, logs, manifests and CSV live under `work/synthetic-campaign/`. That directory is audit evidence, not an external deployment. `candidate-comparison.csv` contains all phases with repeat labels; consult phase caveats before comparing.

Use `recommended-policy.json` as the exact winning settings dictionary. `legacy-fallback.json` restores the captured legacy values and explicitly selects `circulator/prefix/tool_safe/fine`, disables source aliases and elasticity. Apply either as engine settings or the `context_engine` settings block in a **NEW_ROOT-local** prepared profile. Start a fresh session when switching policies. Exact RAW is retained across policy changes. Do not run the old installed launcher or overwrite external prepared profiles for this campaign.

To verify without live servers:

```bash
cd "/path/to/rolling-context"
PYTHONDONTWRITEBYTECODE=1 TMPDIR="$PWD/work/synthetic-campaign/tmp" ./verify.sh
```

The harness scripts intentionally refuse to overwrite many existing state directories. Preserve this campaign and choose a new NEW_ROOT-local output root for a new campaign; do not blindly rerun destructive setup. `install.sh` was not run and no external profile activation occurred.

## Remaining boundaries — NOT TESTED / INFERRED

- No full example-project build, example-project edit, or unattended native Hermes interactive coding session. This is real engine/relay/GPU/CPU inference and memory-tool use against a controlled coding gauntlet with real read-only project source.
- No exhaustive combination grid, statistical population p95, broad task suite, thermal steady-state saturation proof, or infinite-session scheduler fairness test. Long-term throughput thresholds are inferred from measured service rates.
- Segmentation uses structural task/user/tool boundaries, not a semantic dependency graph. A requirement spanning user turns can occupy several related source units; the gauntlet checks whether recall recombines them correctly.
- A32K win here does not guarantee arbitrary old facts are automatically retrieved. Exact search/read and pinned operator invariants remain necessary tools. COMPACT text can omit or misstate facts despite valid provenance.
- No FAST retuning or physical server optimization was attempted. Physical capacity tests are separate from recommended ordinary occupancy.

The concrete shipped decision is fixed32K/36K, coherent16K-soft source clusters,19.2K preferred tail/8K minimum, and independent4/8/12K CPU compaction with1,536 output. The evidence supports this choice over the tested small, large and elastic alternatives.
'''
(ROOT/'ASTRA_CIRCULATOR_STRESS_REPORT.md').write_text(report)
