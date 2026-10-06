# Linux installation

This is an add-on to an existing compatible Hermes installation and local llama.cpp backend. It does not install models, Hermes, or llama.cpp. Do not run the installer on a production workstation just to inspect the package: it changes the per-user launcher symlink.

## Requirements

- Linux with Bash, `/proc`, Unix signals and `fcntl`.
- `/usr/bin/python3` >= 3.10, SQLite FTS5, and PyYAML importable by that interpreter. On Debian/Ubuntu the distribution package is typically `python3-yaml`; use your distribution's package management.
- Installed Hermes CLI at `~/.local/bin/hermes`, a usable Hermes runtime, and source at `~/.hermes/hermes-agent` for offline tests and verifier imports. The wrapper commands accept `--hermes PATH`; prepare accepts `--source-home PATH`.
- Hermes native APIs `agent.context_engine.ContextEngine`, `agent.memory_provider.spawn_context_thread`, and `plugins.context_engine` discovery, including the `local_rolling` shim and native retrieval tools.
- Local llama.cpp APIs `/v1/models`, `/props`, `/apply-template`, `/tokenize`, streaming `/v1/chat/completions`; expected aliases and contexts in CONFIGURATION.

The packaging machine's external Hermes checkout reported commit `234badf4012af380d23c91eae55d045a69c69ffb`. This records the local compatibility snapshot, not a clean upstream guarantee or a universal minimum version; Hermes is not vendored and its working-tree modifications were not audited. Another machine must validate real discovery with doctor.

## Inspect and verify before installing

Keep the package at its final location, for example `~/src/rolling-context`. Paths with spaces work when quoted. If your archive tool loses execute bits, restore them with:

```bash
chmod +x hermes-circulator install.sh uninstall.sh verify.sh
/usr/bin/python3 --version
/usr/bin/python3 -c 'import yaml, sqlite3; c=sqlite3.connect(":memory:"); c.execute("CREATE VIRTUAL TABLE x USING fts5(text)")'
PYTHONDONTWRITEBYTECODE=1 HERMES_DISABLE_LAZY_INSTALLS=1 ./verify.sh
```

Do not interpret a skipped engine import as a complete verification result. Full unit tests also need Hermes APIs available at the conventional source path.

## Prepare an isolated beta profile

Use a fresh state directory to keep the beta separate from existing sessions:

```bash
./hermes-circulator prepare --profile large --state-dir "$HOME/.local/state/rolling-context-beta"
```

Prepare copies configuration and optional SOUL/personality into isolated profiles, creates a shim pointing at this package, and shares dependency environments by symlink. It does not copy credentials/session databases. It does not enable all known-good control settings automatically; see CONFIGURATION. `--copy-secrets` explicitly copies an existing `.env` for local tool use; it is unnecessary for ordinary loopback testing and must never be included in a release or bug report. `--refresh` backs up and rewrites the isolated configuration; it can reset overrides. Prefer fresh state for package relocation.

## Optional per-user launcher installation

```bash
./install.sh --no-prepare
# Or install and prepare only LARGE in the fresh beta state:
./install.sh --profile large --state-dir "$HOME/.local/state/rolling-context-beta"
```

The installer places a symlink at `~/.local/bin/hermes-circulator`, replacing an existing symlink. Record its prior target if you need to restore it. Add `~/.local/bin` to your shell PATH if needed. Direct `./hermes-circulator` commands avoid installing the symlink. `install.sh` without options prepares both FAST and LARGE at the default state root; use the explicit beta form above on a workstation with existing sessions.

Start external compatible model servers yourself, then run doctor and the commands in QUICKSTART. Doctor must check actual Hermes discovery and server identity/context as well as provenance. Preparing a config alone does not prove readiness.

## Uninstall / rollback

Exit the beta session normally. `./hermes-circulator disable --profile large --state-dir "$HOME/.local/state/rolling-context-beta"` disables future beta launches; ordinary `hermes` uses its original configuration. `./uninstall.sh` removes the installed symlink only if it points at this package and retains state. Restore a recorded previous symlink target if needed. `--remove-state` deletes durable history after an interactive confirmation: back it up privately first. The uninstaller does not manage model servers.
