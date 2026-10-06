# Acceptance findings and evidence boundaries

These are supplied historical acceptance/audit findings for an independently audited downstream coding workload, cross-checked against current release modules and regression coverage where possible. Packaging did not replay the workload or copy its private RAW/history/telemetry. This is a summary of recorded evidence, not independent access to the original live audit in this package.

| Observation | Result | Scope |
|---|---|---|
| Real compaction lifecycle | Two jobs reconstructed | Both admitted exactly once with source provenance |
| Content context reduction | 35,178 -> 26,689 tokens (8,489 fewer) | One clear selected-context reduction; not an exact wire-prompt measurement |
| Task continuity | The agent continued executing after compaction | Continuity worked, but technical output remained incorrect and verification was overstated |
| Privacy scan | No new private reasoning fields or `<think>` content found | Recorded run only; no private records shipped |
| Provider stability | 76 requests completed | One client disconnect and two Governor interventions recovered |
| Physical admission | No observed overflow | Maximum prompt 64,162 versus allowance 64,512; tightest headroom 350 tokens |

The margin is small. It is observed headroom for that run, not a recommended new safety setting. Preserve 73,728 physical context, 8,192 generation reserve and 1,024 safety margin. Job readiness alone is not sufficient evidence of exact-once admission.

Historical audit notes recorded one Librarian incomplete-output fallback and a roughly 250-second READY-to-admission delay. The later source fixes addressed task-state staleness and relay shutdown cleanup and added regression tests. Those changes do not retroactively demonstrate all future live interruptions or long sessions are correct.

## Verified release state supplied for this candidate

- Focused release suites: 42/42; additional affected tests: 37/37.
- Full historical verifier: 252 unit tests, 7 checks, 0 failures.
- Historical installed wrapper resolved to the authoritative production checkout; no personal installation path is published.
- Doctor with servers running reportedly showed `installed_wrapper_matches_source: true`, `provenance_errors: []`, `provenance_status: ok`.
- One non-blocking historical profile creation-marker warning still named `Context-Circulator`, while shim/runtime/source resolved to the current authoritative source. This is historical evidence, not an operational instruction to use that tree.
- Production fingerprint observed after fixes: `4b0e4ee4b32d6d5ad651e2005feb720a140954bb01415ef8261229189bfde480`; packaging independently rechecked runtime source identity.

An earlier release snapshot had correct provenance but LARGE offline, so its doctor remained blocked. The later user-supplied running-server result supersedes that availability snapshot. See PACKAGE_AUDIT for exactly what was freshly verified during packaging; no live doctor or acceptance rerun is implied by offline checks.

## Output correctness is independent of continuity

In an independently audited downstream coding workload, Rolling Context successfully preserved continuity while the underlying agent still produced incorrect technical work and overstated its own verification. Context continuity therefore does not guarantee output correctness.

Preserved history does not prove conclusions are correct. Successful compaction does not validate generated code. Passing Rolling Context tests does not validate downstream agent output. Hallucinations are not eliminated; independent output and task validation remain necessary.

## Still unproven / beta risk

Three or more real admitted compaction cycles, multi-hour continuity, broad recall quality/semantic fidelity, live repeated-inspection Loop Guard intervention, interrupts/restarts after every lifecycle phase, native Hermes continuation suppression, concurrent tool-heavy loads and other backends need broader evidence. Regression tests cover observed bugs; they are not a substitute for these live gates. This candidate is for supervised technical review/testing, not a claim of universal reliability.
