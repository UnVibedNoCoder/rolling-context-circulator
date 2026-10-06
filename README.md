# Rolling Context / Circulator

Proposed release **0.3.0-beta.1** — Linux technical beta for Hermes and local llama.cpp. The source is licensed under [MIT](LICENSE). This supervised technical beta does not establish broad runtime reliability.

Long agent sessions can exceed a model's physical prompt budget or lose useful evidence during ordinary compaction. Circulator keeps canonical visible messages and immutable source versions in local SQLite, then builds a bounded working context for each request. An asynchronous Memory Librarian supplies optional source-linked summaries. A relay counts the actual templated prompt, including tools, before forwarding it to the main provider.

```text
Hermes agent -> native local_rolling ContextEngine -> loopback relay -> main llama.cpp model
                         |                               |
                         v                               v
                local RAW/pages/versions           final physical guard
                         |
                 asynchronous Librarian -> optional READY summaries -> later admission
```

## Start here

1. Read [INSTALL.md](INSTALL.md) for dependencies and isolated installation.
2. Follow [QUICKSTART.md](QUICKSTART.md) for prepare, doctor, startup, and rollback.
3. Read [CONFIGURATION.md](CONFIGURATION.md) before applying the known-good LARGE example.
4. Run `PYTHONDONTWRITEBYTECODE=1 ./verify.sh` before a live test.
5. Follow [BETA_TESTING.md](BETA_TESTING.md) and [SECURITY_PRIVACY.md](SECURITY_PRIVACY.md) when reporting results.

The package contains `rolling_context/`, `hermes-circulator`, install/uninstall tooling, `verify.sh`, all current tests and their synthetic fixtures, sanitised historical campaign scripts (with legacy reasoning-inspection and one-off operational helpers excluded), and two example configs. Hermes, llama.cpp, models, and private histories are external dependencies, not bundled files. Historical scripts are described in [scripts/README.md](scripts/README.md); they are not the normal startup path.

## Supported environment and requirements

Linux, Python 3.10+, Bash, SQLite with FTS5, PyYAML for `/usr/bin/python3`, an installed Hermes with native ContextEngine support, and a local llama.cpp server exposing `/v1/models`, `/props`, `/apply-template`, `/tokenize`, and chat completion SSE. The source uses `fcntl`, `/proc`, Unix signals and local HTTP routing. There is no native Windows EXE or universal-agent integration in this release. Other OpenAI-compatible providers require compatibility work; the OpenAI chat endpoint alone is insufficient.

Hermes is normally discovered through `~/.local/bin/hermes` and `~/.hermes/hermes-agent`; see INSTALL for the compatibility snapshot and limitations. No hardware-independent GPU/RAM minimum has been established. The measured LARGE setup used a 73,728 physical context; ensure your model and hardware can actually support it.

## Known-good LARGE example

Main Qwen3.8-27B: loopback 8084, context 73,728, alias `qwen38-27b-atx-73k`. CPU Librarian Qwen3.5-4B: 8083, context 65,536, alias `qwen35-4b-compactor`, GPU layers 0, reasoning off. Relay: 8094. Generation reserve: 8,192; physical safety margin: 1,024; normal input allowance: 64,512. Content target/trigger: 32,000/36,000. These are the validated example, not universal backend defaults.

```bash
# From the unpacked package, after setup and starting compatible servers yourself:
./hermes-circulator doctor --profile large --state-dir "$HOME/.local/state/rolling-context-beta"
./hermes-circulator run --profile large --state-dir "$HOME/.local/state/rolling-context-beta" -- --toolsets terminal,file,todo
```

The wrapper launches its relay and Hermes; it does not launch model servers. Existing occupants of relay ports are left untouched. Exiting or interrupting the wrapper cleans up only its owned children. See ARCHITECTURE for signal handling and generation recovery.

## Verification and telemetry

Offline verification checks launcher/interpreter/PyYAML, imports with the external Hermes source, every manifested file hash, and the unit suite. Historical release verification was 252 tests and seven checks with zero failures. [PACKAGE_AUDIT.md](PACKAGE_AUDIT.md) records this copy's fresh results, independently of the historical live evidence.

`status --profile large --state-dir ...` shows profile and relay status. Private state contains `memory.sqlite3`, `telemetry.jsonl`, profile configuration/session data and `logs/relay-large.log`. Events include selection ledgers, compaction lifecycle, summary admission, final relay counts, provider timings, Loop Guard, Governor and source/policy fingerprints. `active_tokens` in engine events and exact relay prompt counts measure different stages. A READY counter alone does not prove admission; correlate source/job IDs and selected summary versions with completed requests.

## Troubleshooting

- Missing YAML: install PyYAML for the launcher's `/usr/bin/python3`, not only a separate virtual environment.
- Missing Hermes engine: check compatible Hermes source, launcher and runtime; this package does not bundle Hermes.
- Source/shim mismatch after moving a package: stop the beta session, then prepare a fresh isolated profile in the final package location. Doctor fails safely on source mismatches; there is no silent repair.
- Port occupied: identify the owner, close your own session normally, or use a separate test machine. Do not kill unrelated occupants.
- Main server absent/wrong model/context: start the correct existing server and rerun doctor. A correct source fingerprint does not prove server readiness.
- Compactor unavailable/busy/rejected: RAW circulation/recall continue; inspect typed failure events. Check alias, 65,536 context, CPU-only process and reasoning-off flags.
- Prompt rejected: inspect exact prompt plus tool-schema count and protected material. Keep physical limits, reserve and safety intact; do not mask this by raising context metadata.

## Beta status and limitations

In an independently audited downstream coding workload, Rolling Context successfully preserved continuity while the underlying agent still produced incorrect technical work and overstated its own verification. Context continuity therefore does not guarantee output correctness.

Preserved history does not prove conclusions are correct. Successful compaction does not validate generated code. Passing Rolling Context tests does not validate downstream agent output. Hallucinations are not eliminated; independent output and task validation remain necessary.

[FINDINGS.md](FINDINGS.md) separates the independently audited downstream workload evidence from open beta gates: two jobs, exact-once admission, one 35,178 -> 26,689 content-token reduction, continued execution after compaction, and a tightest observed 350-token physical margin. Multi-hour runs, 3+ cycles, live repeated-read interventions and backend breadth still need testing. Summaries are lossy; schema/provenance validation cannot prove semantic fidelity. Text chat only; no embeddings, cross-session recall, multimodal handling or promised KV reuse. Native Hermes plugin failures can fail open; the relay is the final physical guard.

[ARCHITECTURE.md](ARCHITECTURE.md), [CODEX_HANDOFF.md](CODEX_HANDOFF.md), [CONTRIBUTING.md](CONTRIBUTING.md), and [RELEASE_NOTES.md](RELEASE_NOTES.md) support review and future work. The [MIT license](LICENSE) covers this source; external Hermes, llama.cpp and models retain their own licenses. No repository was initialised, pushed or tagged during packaging.
