# Final package audit — 2026-10-06

Verdict: **READY FOR GITHUB** as the proposed 0.3.0-beta.1 public repository source package. This is repository hygiene/readiness, not broad live-runtime reliability or a guarantee about an agent's output. No Git initialization, repository creation, push, tag or publication was performed.

## Authorized release-only changes

The owner authorized MIT licensing and public sanitisation. LICENSE contains the standard MIT grant with neutral contributor attribution. The placeholder was removed; documentation and release metadata agree on MIT. External Hermes, llama.cpp and model weights remain external and retain their own terms.

The downstream workload is anonymised throughout source, docs, fixture data, test names and inventories. `tests/fixtures/synthetic_coding_loop.json` uses a generic worker workload without identifiable game details. Repeated plan/churn structure, decision locks, visible tool/output cases, triggering conditions, privacy checks and original behavioral assertions remain; domain-specific text and markers were generalized. Other historical campaign project names, paths and output locations are generic.

The fixed-process cleanup and private archive-specific continuity replay are excluded. Retained archive helpers require explicit archive and session arguments, open offline snapshots read-only, and reject missing files/unknown sessions before output creation or model checks. The replay uses the latest four request snapshots; the ceiling probe uses the latest request. Neither discovers a private archive or chooses a historical session. Three regressions cover required inputs, missing-file no-create behavior and unknown-session archive preservation.

Repeated verification exposed an existing integration-test reader race against an active telemetry append. The runtime writer already uses an exclusive file lock; the package test reader now takes the matching shared lock. Event assertions are unchanged. It neither skips events nor silently discards partial/malformed JSON: two deterministic regressions prove it waits for an active writer and rejects completed corrupt records. This is a test-only synchronization fix, not a change to runtime telemetry, Governor or relay behavior.

## Source integrity and inventory

116 files are included, all UTF-8 text. The 115-file manifest covers every file except itself; inventory and hashes match. All 26 runtime modules and launcher/install/uninstall/verifier are byte-identical to the authoritative production source and pre-fix package. Runtime fingerprint remains `4b0e4ee4b32d6d5ad651e2005feb720a140954bb01415ef8261229189bfde480`.

SOURCE_COMPARISON records release-only source/test differences, generic fixture renaming, added files and exclusions. Original regression behavior is retained, with five additional tests. Four historical helpers are excluded: two hidden-reasoning inspectors, one fixed-process cleanup, and one archive-specific replay. Private histories/campaign exports and dependency environments are not package inputs.

## Sanitisation

Recursive filename/content scans and manual review found zero private downstream project-name variants, zero other private project names, zero real personal usernames/home paths, zero mounted-drive/old agent-output paths, zero real session-shaped or UUID constants, and zero fixed process identities. There are no credential-shaped keys/tokens/passwords, bearer values or email addresses. One secret-assignment match is a deliberately synthetic privacy sentinel in `tests/test_librarian.py`, not a real secret.

No RAW/history conversation export, telemetry dump, private hidden reasoning, SQLite/WAL/SHM, runtime state, model/binary files, logs, caches/bytecode, lock/PID/socket files, home-directory copy, Git metadata or editor/temporary junk is bundled. Remaining path occurrences are classified in SANITISATION_RESULTS: portable home-relative defaults, declared Linux requirements, opt-in external placeholders, generic campaign dependencies, and intentional synthetic stale-source/mismatch fixtures. Privacy-field names and think-tag strings are protection code/documentation and synthetic negative tests, not actual private reasoning. Pattern scans cannot prove absence of every possible secret.

## Verification evidence

The repaired candidate's exact-root verifier passed **257 unit tests in 69.217 seconds; 7 checks passed; 0 failed**, exit 0. This includes the historical 252 tests plus three replay-input and two telemetry-reader regressions. All 85 Python files parse, the three shell scripts pass Bash syntax checks, both YAML examples parse and match current generated defaults/control differences, issue-template front matter parses, relative Markdown links resolve, and complete manifest coverage passes. Replay `--help` works without engine imports.

An earlier sanitised candidate passed 255 tests twice. A later post-summary run attempted 255 tests in 65.788 seconds and failed with one JSON decoding error in Governor HTTP evidence reading (six checks passed, one failed). That failure, the test-only diagnosis and repair, and all previous measured results remain in verification/RESULTS; failed runs were not hidden or relabelled as passes. Focused reader tests (2) and Governor HTTP tests (10) passed before the repaired full verifier.

Temporary-profile LARGE doctor passed real external Hermes discovery and source/shim attribution (`provenance_status: ok`, no errors). LARGE alias/context on 8084 / 73,728 and compactor alias/context on 8083 / 65,536 were correct; compactor GPU layers 0 and reasoning off were verified. The expected warning is that the existing installed wrapper points to production, while direct package launch and isolated shim point to this package. The installed wrapper was deliberately not changed.

Tests ran in a read-only host/package view with only temporary test paths and a private external dependency copy writable. Mock servers used an isolated loopback network/PID namespace. Doctor used synthetic temporary source/profile data and read-only host health/identity checks; no production profile or credential content was copied, no inference was launched, and no model servers were started/stopped. Synthetic mock-error tracebacks and a ResourceWarning are recorded without hiding failures; the repaired suite completed `OK`. Final manifest/docs validation and the complete verifier are repeated after these evidence files are finalized; the terminal result is authoritative.

## Output correctness and limits

In an independently audited downstream coding workload, Rolling Context successfully preserved continuity while the underlying agent still produced incorrect technical work and overstated its own verification. Context continuity therefore does not guarantee output correctness.

Preserved history does not prove conclusions are correct. Successful compaction does not validate generated code. Passing Rolling Context tests does not validate downstream agent output. Hallucinations are not eliminated; independent output and task validation remain necessary. Continued execution is not evidence of a correct technical result.

Long runs, three-plus real admitted cycles, live repeated-inspection intervention, semantic recall fidelity, interruption/restart phase coverage, native continuation suppression and backend breadth remain broader beta gates. Historical observations and small samples are labelled in FINDINGS and verification/RESULTS. Candidate version metadata remains separate from preserved runtime version strings. No Windows/universal-agent promise is made.

## Preservation and next step

Production source hashes, modes, sizes and modification times were unchanged; this release work made no production-state or installed-wrapper changes. Frozen engine behavior, budgets, LARGE configuration, Loop Guard/Generation Governor settings, compaction and recall policies remain unchanged. Existing sessions/services were left alone; only audit-owned temporary resources were used.

The directory is clean and suitable as the public GitHub repository root. Git initialization and publishing await a separate explicit owner instruction.
