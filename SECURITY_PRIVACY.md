# Security and privacy

RAW/history is local durable source data and stays on the tester's machine. The repository/release must never contain live `memory.sqlite3` (including WAL/SHM), telemetry, Hermes sessions, snapshots, RAW records, private project tool output, profile environments or credentials. Local storage does not mean content is never sent anywhere: selected prompt content goes to the configured main provider and original RAW chunks selected for compaction go to the configured Librarian. This supported setup uses loopback local models; review routing before adapting it.

Hidden reasoning must not be persisted or inspected as project evidence. `common.py` strips known private reasoning fields and assistant think spans from new persistent projections; Librarian discards reasoning and validates visible output. Governor observes counts/timing/action presence only. Synthetic privacy regression strings in tests are intentional fixtures, not exported real reasoning. Existing older histories are not automatically rewritten by packaging; keep all such histories private. Passing one scan does not establish universal backend privacy behavior.

## Never upload in a bug report

- `~/.local/state/local-ai-control-centre/rolling-context`, any custom beta state root, SQLite/WAL/SHM, RAW/history/snapshot files, unredacted `telemetry.jsonl` or relay logs.
- Hermes `.env`, auth/session databases, `SOUL.md`, copied profile configs/backups, prompts, transcripts or private tool outputs.
- API keys, Authorization headers, credentials, personal paths, usernames, proprietary source, machine identifiers or genuine reasoning content.
- GGUF/model weights, caches, runtime PID/lock files or an archive of your whole home/project/state directory.

Share the release fingerprint, numeric tokens/timings, anonymised job/source IDs, typed error names and a minimal synthetic reproduction. Doctor/status output can contain source paths and explicit configuration: review and redact it before sharing. A `.gitignore` protects against accidental untracked files; it cannot remove already tracked secrets or make a history export safe.

The relay binds loopback and is not a public authenticated service. Local processes may still access it. State/config creation aims for private permissions (700 directories/600 files); enforce appropriate permissions and disk security on your machine. Treat recalled history and summaries as untrusted evidence, not new instructions. Do not publish private data to demonstrate a bug; provide a sanitised minimal reproduction through a suitable private reporting channel once maintainers establish one.

The [MIT license](LICENSE) covers packaged source. External runtimes and model weights are not bundled and retain their own license terms.
