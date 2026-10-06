# Codex / Sol / Work / Hermes handoff

Copy the following context into a new development session. Use the package directory on your own machine; do not assume the packager's filesystem exists.

## Handoff prompt

You are reviewing the proposed Rolling Context / Circulator 0.3.0-beta.1 Linux technical beta. First read README, INSTALL, CONFIGURATION, ARCHITECTURE, FINDINGS, BETA_TESTING, SECURITY_PRIVACY and PACKAGE_AUDIT. The installed/reviewed package on this machine is the source root for this session; ask the owner to identify a different authoritative production checkout before changing it.

The owner-identified authoritative production checkout supplied this package. Obsolete checkouts and old agent-output directories are not source-of-truth and were not copied. Never fall back to another tree without identifying the current authority. Runtime fingerprint at packaging was `4b0e4ee4b32d6d5ad651e2005feb720a140954bb01415ef8261229189bfde480`. Source comparison and manifest identify the actual release copy. The production tree was not intentionally edited for this release.

Understand Agent/Hermes -> native local_rolling engine -> loopback relay -> llama.cpp. RAW visible history and immutable source versions stay local; selected request copies may evict raw content while exact recall remains. Asynchronous Librarian summaries use original RAW, validate source identity and bounds, become READY, and are admitted at a later request boundary. Warm/compact/recall budgets are independent. The relay performs the exact final templated token check including tools. Task state is derived evidence, never proof of success on its own.

Frozen known-good controls: LARGE Qwen3.8-27B :8084 / 73,728; CPU Qwen3.5-4B :8083 / 65,536, GPU layers 0, reasoning off; generation reserve 8,192; safety 1,024; target/trigger 32,000/36,000; tail/min 19,200/8,000; segment max 16,000; recall/warm/global 14,400/4,000/2,048; chunks 4,000/8,000/12,000; Librarian output and stored cap 4,096; baseline output 1,536; fallback reserve 180s; job timeout 600s; Loop Guard enabled 5/6/250; Generation Governor enabled; Execution State and elasticity disabled. These are example effective settings, not a claim that every fresh profile enables those controls. Do not tune architecture, models, ports or reserves to disguise a correctness issue.

Known tests: historical focused release 42/42, affected existing 37/37, full suite 252 tests and verifier seven checks / zero failures. PACKAGE_AUDIT supplies fresh copy verification. Historical downstream audit reconstructed two jobs admitted once with provenance, selected-content reduction 35,178 -> 26,689, continued execution after compaction, privacy pass, 76 completed provider requests, one disconnect and two Governor recoveries. Maximum observed prompt 64,162 / 64,512 allowance leaves just 350 tokens. Do not overstate this as broad reliability. Three-plus cycles, long runs, live repeated-inspection intervention and compatibility breadth remain beta gates.

In an independently audited downstream coding workload, Rolling Context successfully preserved continuity while the underlying agent still produced incorrect technical work and overstated its own verification. Context continuity therefore does not guarantee output correctness. Preserved history does not prove conclusions are correct. Successful compaction does not validate generated code. Passing Rolling Context tests does not validate downstream agent output. Hallucinations are not eliminated; independent output and task validation remain necessary.

Keep fixes narrow. Inspect the actual request path/configuration and process owners before runtime changes. Preserve concurrent sessions, RAW/history, known-good server profiles and physical limits. Never inspect/store/log private chain-of-thought. Unknown tool schema cost is nonzero conservatively. Numeric recovery is request-local, at most one retry, never a global reserve reduction. Archive retrieval is memory_retrieval; session filtering precedes FTS limits; current disk capture and archived RAW must remain distinct. Provenance mismatch fails safely with no silent repair. Fix installation via the current installer; never make an obsolete tree authoritative.

Use direct ownership for relay/Hermes child cleanup; no broad process kills or killing by port. Preserve `check_shutdown()` after `hermes.wait()` and signal exit semantics. Tests cover task-state reconciliation and cleanup bugs; request real live lifecycle evidence for new claims. Do not start model workloads or mutate production state during a read-only audit. Default verification after a narrow code fix: focused regression, `/usr/bin/python3` full suite, offline verifier, then an explicitly authorised doctor/live test with isolated state. Compare source/policy fingerprints and source/shim/wrapper attribution. Report observed evidence separately from inference and open validation.

The source has an owner-authorized MIT license. Do not initialise/push/tag without the owner's explicit instruction. Windows EXE/universal-agent/backend work is roadmap scope and must not appear as implemented support without source and tests proving it.

## First review actions

1. Verify manifest and SOURCE_COMPARISON, then run offline tests with compatible external Hermes.
2. Check generated defaults versus known-good examples and document any environment dependency.
3. Review source provenance, admission exactness, privacy projections, task-state reconciliation and owned-child cleanup.
4. Run beta scenarios only against isolated, owned resources; produce a redacted evidence table.
5. Keep proposed version metadata separate until the owner authorises coordinated runtime version changes; MIT licensing is already authorized and recorded in LICENSE.
