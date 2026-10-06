# Historical experimental campaign tools

These Python tools are preserved from the authoritative tree for developer inspection. They are not the normal install/start/verification path and were not run during release packaging. Several expect excluded `work/` campaign outputs, external project inputs, benchmark archives, or a particular Hermes runtime; therefore they are not turnkey beta tests. Running/importing some scripts can create files, issue model requests, start test servers or launch agents.

The historical `astra_reasoning.py` and `astra_audit.py` are excluded because they inspect archived hidden reasoning. The one-off `circulator_finish_runtime.py` is excluded because it held obsolete process identities. The archive-specific `continuity_replay.py` is excluded because it depended on private snapshot identifiers, calibrated overhead and a live-history default. The campaign helper `circulator_live.py` no longer accumulates or exports reasoning text. The ceiling-probe response export uses the existing reasoning redactor; the live/continuity campaign helpers remove assistant think spans from exported visible content. These are package-only privacy changes; runtime modules are untouched.

Machine-specific absolute paths have been replaced by explicit `/path/to/...` placeholders. Workload names, project paths and output directories are generic. `circulator_stress.py` now checks the local package's engine file rather than a personal source-root string; `circulator_boundary.py` derives ROOT from its own location. SOURCE_COMPARISON lists all edited files. Runtime source and launch/install/uninstall/verify tooling are unchanged.

`astra_replay.py` and `astra_ceiling_probe.py` require both `--archive PATH` and `--session SESSION`; neither discovers private archives or selects a historical session automatically. Supply a disposable, offline SQLite snapshot with the expected `snapshots` and `records` tables. They open that snapshot read-only with `immutable=1`, which must not be used for a live WAL database. Missing files and unknown sessions fail before model checks or output creation. Both helpers expose `--help` without importing the engine. Replay screens the latest four request snapshots and defaults `--source` to this package; the ceiling probe uses the latest request. Full model campaigns remain opt-in and were not live validated during sanitisation.

For example, after inspecting the helper and providing your own synthetic inputs:

```bash
/usr/bin/python3 scripts/astra_replay.py --archive /path/to/offline-synthetic.sqlite3 --session synthetic-session --label synthetic-replay
/usr/bin/python3 scripts/astra_ceiling_probe.py --archive /path/to/offline-synthetic.sqlite3 --session synthetic-session --profile large --target 32000 --label synthetic-probe
```

Outputs can contain supplied visible source/project content. Keep them local and review them before sharing; scripts that redact reasoning do not automatically sanitize every credential or private tool output. The repository includes no archive or campaign result data.

Use the offline unit suite and BETA_TESTING first. Before opting into any campaign script, review its entire entry point, provide isolated inputs/outputs, replace placeholders with your own local resources, and inspect ports/process ownership. `astra_servers.py` includes historical hardware-specific model launch parameters; do not treat those as recommended universal defaults or run it against production services. Paths under `work/astra-original` refer to an optional user-supplied comparison input, not a bundled obsolete source tree. No private archive/model/project source is included.
