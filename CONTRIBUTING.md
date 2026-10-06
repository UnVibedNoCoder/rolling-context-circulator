# Contributing and reporting

This is a proposed technical beta licensed under [MIT](LICENSE). Contributions to this source are under the same license; external dependencies retain their own terms. Do not push/tag this candidate as a final release without the owner's decision.

Keep changes narrow and fix reproduced correctness defects before optimisation. Preserve RAW, canonical visible transcript, provenance, exact physical admission, 8,192 reserve, safety margin and process ownership. Never tune model/server settings as part of an unrelated code fix. Separate experimental settings from the known-good example.

For a bug report include the candidate/source fingerprint, Hermes and llama.cpp versions, Linux/Python/PyYAML versions, selected profile, redacted effective settings, precise reproduction steps using synthetic content, expected/actual behavior, typed error and focused/full test results. For lifecycle bugs include anonymised job/source/generation identifiers and timestamps for queued/start/READY/admission/selection/provider completion. For token failures include exact numeric prompt, requested output allowance, schema overhead and safety margin. For process bugs describe owned-child exit status and unchanged foreign occupants. Do not claim completion from READY alone.

Never attach the whole state directory, RAW/history databases, unredacted telemetry/config/doctor output, hidden reasoning, secrets, personal paths or models. Read SECURITY_PRIVACY and share a small manually reviewed numeric/redacted excerpt. If the report cannot be made public safely, use a private maintainer channel when one is established; this package does not invent one.

## Local change workflow

1. Work in a separate reviewed checkout, identify the actual authoritative root, and reproduce with temporary state.
2. Add a meaningful regression for a correctness/lifecycle/privacy change; preserve existing assertions.
3. Run the focused test, `PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -m unittest discover -s tests -q`, then the verifier after regenerating the manifest for intentional changes.
4. Regenerate manifest hashes only after reviewing source differences; keep every shipped file except the manifest itself covered. A mismatch must fail validation.
5. Use doctor/live beta evidence only with isolated state and authorised test resources. State what remains unverified.

PR descriptions should explain the concrete trigger, previous failure, new behavior, validation and material limits. Never embed private data in examples or issues. Historical campaign scripts are not default checks and may require optional private inputs that must remain local.
