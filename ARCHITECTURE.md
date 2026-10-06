# Architecture and invariants

## Request flow

Hermes holds its canonical transcript and calls the native `local_rolling` ContextEngine at request boundaries. The engine creates a new selected request without rewriting that transcript. Hermes sends it through the loopback relay, which applies the provider's real chat template and tokenizer including tool definitions, validates physical admission, then streams the main model response. The current integration is Hermes plus fixed local llama.cpp routes; generic Agent -> Rolling Context -> Provider describes the design, not universal implemented adapters.

`engine.py` coordinates selection and tools; `common.py` owns persistence and token helpers; `pages.py`, `working_set.py`, `selection.py`, `coherent.py`, `admission.py`, `schema_budget.py`, `scheduling.py` and `compaction.py` implement context and summary lifecycle. `continuity.py` and `reconciliation.py` derive task state. `file_reads.py` and `visibility.py` track current versus archived file evidence. `relay.py`, `cli.py` and `provenance.py` enforce final admission, lifecycle and source attribution.

## RAW is the durable source of truth

Visible canonical messages and source versions are stored in local SQLite. Immutable SHA-256 source/page IDs identify the exact archived version; repeated identical records share storage and edited records produce new versions. FACTS are deterministic literal observations, INDEX supports lexical/FTS5 recall, and COMPACT is optional lossy source-linked memory. None replaces RAW. The bounded RAM cache is an accelerator, not durable authority. Private reasoning is removed before new persistent message projections; canonical history here means visible content and tool evidence, not hidden reasoning.

Residency states include ACTIVE, WARM, COOLING, COLD, COMPACTING, ARCHIVED and RECALL. Old raw pages can leave the selected request before a summary exists. The recent tail, instructions, task anchors and current tool atoms have priority. Coherent tool-call/result groups remain structurally valid. Oversized protected material can still make a request inadmissible.

## Librarian and READY/admission

Cold source episodes are queued for a single asynchronous worker, with a cross-profile file lock for CPU compaction. It reads original RAW rather than recursively compressing summaries. The known-good compactor is CPU-only and reasoning-off. Baseline and Librarian modes validate completion, bounds, structured shape and source attribution; failed, truncated or ungrounded outputs install no new representation. Librarian may fall back to baseline within the bounded job deadline. The 600-second total and 180-second requested reserve are bounded by the remaining budget (reserve capped at one third of total).

A queued job starts, produces a validated READY representation, and is considered at a later request boundary. READY does not mutate an in-flight generation. Admission records exact provenance and representation selection; each audited job was admitted once. Prove this with job/source IDs, admission/selection events and the subsequent wire request. A READY counter or `applied:false` by itself proves neither selection nor failure.

## Warm, compact and recall

Warm summaries have separate budgets and a block count cap. Relevance selection can use FACTS, COMPACT, bounded RAW excerpts or exact RAW. Recall uses identifiers, lexical matching and FTS5 with session/allowed-source filtering before result limits. Archive/history tools are exposed as `memory_retrieval`, never authoritative new current-user evidence.

`rolling_history_search` and `rolling_history_read` retrieve historical visible records. `rolling_file_snapshot` refreshes disk explicitly; `rolling_snapshot_read` and `rolling_raw_read` read exact archived evidence. Mutable-file results label successful disk rechecks `current_disk_capture`; failed rechecks fall back to `archived_raw`. Frozen request projections retain consistent versions on retries. Historical retrieval echoes are excluded from creating false new file-source evidence.

## Task continuity and controls

Derived task state keeps objectives, decisions, next actions, evidence and blockers within bounded source-linked sections. Stale state must be reconciled against current evidence; action promises alone do not prove completion. Task-state reconciliation fixes are regression-tested. Ordinary native Hermes continuation behavior still needs broad live validation.

Loop Guard observes visible plans, repeated unchanged inspection/tool results and lack of concrete progress at request boundaries. It injects a small transient system-derived control after bounded evidence and cooldowns. It does not poll files, inspect hidden reasoning, cancel in-flight requests or rewrite RAW. Legitimate analysis and changed evidence must remain protected. The 5/6/250 example means five no-progress turns, six cooldown turns, at most 250 control tokens. Live repeated-inspection intervention remains a beta test target.

Generation Governor is separate: `governor.py` and `governed_stream.py` use numeric counts, elapsed time and output/action presence to bound exhausted reasoning-only generations. Hidden text is neither retained nor compared. One request-local retry at most, normally with thinking disabled, preserves the 8,192 global generation reserve. It must recheck final physical admission and tool atoms; stop cleanly if recovery cannot fit. Useful visible output/tool action protects an ongoing request. Execution State is optional transient EXPLORE/IMPLEMENT/DEBUG guidance and is disabled in the known-good example. Legacy boundary `governor` and experimental elasticity are separate, disabled controls.

## Final physical guard and relay lifecycle

For LARGE, 73,728 - 8,192 - 1,024 = 64,512 normal prompt allowance. Content target/trigger are distinct from the exact provider prompt including instructions and tool schemas. Unknown tool schemas have a conservative nonzero fallback; only explicit no-tool auxiliary requests may have zero schema overhead. The relay normalises output-limit aliases consistently and rejects invalid/oversized requests before provider submission.

The launcher validates profile, shim, runtime and source/policy fingerprints, checks main server identity/context, refuses an occupied relay port, launches its relay and Hermes, then cleans up its directly owned children. SIGINT/SIGTERM/SIGHUP handlers are registered before child spawn. `check_shutdown()` after `hermes.wait()` preserves wrapper signal exit status (SIGTERM 143). Cleanup never scans for or kills unrelated port occupants/model servers. Client disconnect/provider failure cancellation applies to the owned request socket. No model-server start/restart is part of normal wrapper launch.

## Boundaries

Text-only; local durable history; no embeddings, cross-session recall, guaranteed summary fidelity, universal backend support, Windows EXE or promised KV-cache reuse. Provider aliases, tokenizer endpoints and CPU process validation are intentionally constrained in current source. Future ports/platforms/agents belong to the roadmap with dedicated compatibility evidence.

Preserved history does not prove conclusions are correct. Successful compaction does not validate generated code. Passing Rolling Context tests does not validate downstream agent output. Hallucinations are not eliminated; independent output and task validation remain necessary.
