# Beta testing protocol

Use a disposable project and a fresh state root outside the package. Start with INSTALL/QUICKSTART and record package fingerprint, Hermes/backend versions, model alias/context, effective policy fingerprint and redacted doctor results. Never upload the private state root. Preserve frozen controls while testing; record any explicit experimental change as a separate run.

Score context continuity and downstream output correctness separately. Preserved history, successful compaction and passing Rolling Context tests do not validate an agent's generated code or conclusions. An independently audited downstream workload retained continuity while producing incorrect technical work and overstating verification. Hallucinations are not eliminated; validate task artifacts independently.

For every scenario capture timestamps, job/source identifiers, queue/start/READY/admission/selected versions, final prompt tokens plus generation allowance, provider completions and actual verified task artifacts. A readiness count alone is insufficient. Report failed/aborted attempts as well as successful runs.

| Try to break it | Procedure | Required evidence / pass criterion |
|---|---|---|
| Multi-hour continuity | Run a real disposable coding task for several hours with explicit milestones/tests | Agent retains task/decisions, makes verifiable changes and completes or explains a real blocker without false completion |
| 3+ compaction cycles | Grow visible tool/content history until at least three real jobs reach READY and are admitted | Every admission has correct source/version provenance, each job admitted once, completed requests use selected memory, exact RAW remains readable |
| Interrupts | Separately interrupt before relay readiness, during provider streaming, while compaction runs, and after READY | Owned Hermes/relay exit; wrapper signal status is appropriate; existing model servers/foreign processes survive; state remains recoverable |
| Restarts | Exit normally, restart against same isolated beta state, then resume a known task | RAW/source versions survive; no stale task-state success; no duplicate/stranded admission claimed without evidence |
| Tool-heavy workloads | Use many tool definitions, large logs, oversized records and interleaved tool calls/results | No orphan/malformed atoms, conservative schema accounting, final templated count within physical allowance; correct rejection if protected content cannot fit |
| Repeated-read loops | Ask the agent to repeatedly inspect unchanged disposable files after it has enough evidence to act | Loop Guard evaluates and intervenes live under enabled controls; changed evidence and legitimate analysis are not suppressed; transient control is absent from RAW |
| Governor recovery | In a controlled request provoke exhausted generation without visible/action output | Numeric trigger and at most one retry, then useful output or explicit stopped/incomplete status; 8,192 reserve unchanged; no hidden text persisted |
| Provider failures | Stop only a test-owned provider, inject timeout/truncated SSE/incomplete summary, then recover | Typed failures, no partial invalid summary installed, RAW recall still works, no retry storm, no accidental server/process kill |
| Port conflicts | Occupy relay port 8094 with a test-owned harmless service; try wrapper launch | Clear refusal and existing occupant remains alive; close your test service afterwards |
| Compactor absent/busy | Run with unavailable or occupied CPU compactor | Main circulation proceeds; jobs fail/defer honestly; no GPU or reasoning-on silent substitution |
| Other backends | On a separate machine test an OpenAI-compatible server with required tokenizer/template/props endpoints and explicit aliases | Record unsupported APIs/identity checks as incompatibility; do not claim support merely because chat completions work |
| File provenance / recall | Read a file, edit/delete it, recall old snapshots, ask for a refresh | Current disk and archived RAW labels/versions are accurate; session-filtered search cannot leak another session |
| Privacy | Use harmless synthetic reasoning-channel markers in a controlled fixture | New stored messages/summaries/task state contain no hidden fields/spans; do not inspect genuine private chain-of-thought |

Live failure injection must target only resources you created for that test. Use direct owned-child handles; inspect owner PID/argv/parent before any manual signal. Never use broad `pkill`, kill by port alone, or delete state to hide a failure. Offline regression tests should run first; do not run historical server-management campaign scripts on shared production ports.

Submit a beta report with scenario, environment, effective settings, durations, cycle count, anonymised IDs, numeric token headroom, exact failure/pass observation and resulting task tests. Distinguish queued, READY, admitted, provider-completed and task-completed states. Follow CONTRIBUTING and SECURITY_PRIVACY for redaction. Broad beta gates remain open until actual repeatable live evidence exists.
