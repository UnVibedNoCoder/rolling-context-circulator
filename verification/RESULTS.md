# Packaged verification results — 2026-10-06

## Historical evidence retained

| Invocation | Actual result |
|---|---|
| 2026-10-05 restricted staging | 252 tests attempted; 35 denied socket operations and one read-only dependency-lock failure; six checks passed, one failed |
| 2026-10-05 isolated staging | 252 tests in 68.294s; seven checks passed; zero failed |
| 2026-10-05 isolated destination | 252 tests in 69.896s; seven checks passed; zero failed |
| 2026-10-06 pre-fix exact-root audit | 252 tests in 68.836s; seven checks passed; zero failed |
| 2026-10-06 first sanitised candidate | 255 tests in 69.463s; seven checks passed; zero failed |
| 2026-10-06 finalized initial evidence/manifest | 255 tests in 76.109s; seven checks passed; zero failed |
| 2026-10-06 post-summary before reader repair | 255 tests in 65.788s; one JSON decoding error; six checks passed, one failed; exit 1 |

These are measurements of their respective earlier artifacts, not independent proof of the final changed candidate. No failed run was hidden or relabelled.

## Current repaired candidate

| Check | Result |
|---|---|
| Exact-root repaired verifier | **257 tests in 69.217s; seven checks passed; zero failed; exit 0** |
| Regression coverage | Historical 252 retained; three explicit replay-input and two telemetry-reader tests added |
| Focused repair checks | Two deterministic reader tests and ten Governor HTTP tests passed |
| Python parse | All 85 shipped Python files parse |
| Shell syntax | install.sh, uninstall.sh, verify.sh pass Bash syntax checks |
| YAML examples | Both parse; generated LARGE matches source; known-good differs only in two control enable toggles |
| Documentation links/templates | Relative Markdown links resolve; both issue-template front matters parse |
| Manifest/inventory | 116 files, 115 manifested hashes; complete coverage except manifest itself |
| LARGE doctor | Exit 0; real Hermes discovery, packaged source/shim, no provenance errors |
| Main/compactor facts | Correct aliases/contexts, compactor GPU layers 0 and reasoning off |
| Doctor warning | Installed wrapper remains pointed at production; direct package/shim are correct |
| Sanitisation | No private-name/user-path/session/PID leaks or bundled runtime data; intentional synthetic privacy/stale-path fixtures retained |
| Full live model campaigns | Not performed during release sanitisation |

## Test-only telemetry synchronization

The failed post-summary invocation caught an incomplete JSON append in an asynchronous integration-test read. The runtime writer already locks appends exclusively. The test reader now takes the matching shared lock; no event/assertion is skipped and no malformed line is silently discarded. Completed malformed JSON still raises. Two deterministic tests exercise waiting for an active writer and rejecting corrupt completed data. Runtime modules remain byte-identical.

Final manifest/docs checks and the full verifier are repeated after this evidence is finalized; the terminal result confirms that artifact, with timing reported in the owner's final response. Timings above remain measurements of their respective invocations.

Synthetic mock HTTP error-path tracebacks and a ResourceWarning did not cause the successful suite failures; the separate JSON decoding error above did, and is preserved. No assertion was weakened or skipped. Tests used temporary state/mock sockets in a read-only host view and private dependency snapshot. Doctor used synthetic temporary profile data and read-only health/identity probes. No production source/state, installed wrapper, model config or service was modified.
