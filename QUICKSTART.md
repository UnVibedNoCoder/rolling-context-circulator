# Quickstart: isolated LARGE beta

Complete INSTALL first and keep the package in its final location. Run from the package directory. Have compatible Qwen main and CPU compactor servers running on 8084 and 8083; the wrapper will create its own relay on 8094.

```bash
PYTHONDONTWRITEBYTECODE=1 HERMES_DISABLE_LAZY_INSTALLS=1 ./verify.sh
./hermes-circulator prepare --profile large --state-dir "$HOME/.local/state/rolling-context-beta"
```

Review the generated profile at `~/.local/state/rolling-context-beta/profiles/large/config.yaml`. For the audited control settings, use the `context.local_rolling` values in `examples/large-known-good.yaml` following CONFIGURATION. Do not replace the generated `state_dir`, shim, model route or unrelated preferences blindly. Fresh source defaults leave Loop Guard and Generation Governor disabled; the example enables them. No production settings are changed by reading it.

```bash
./hermes-circulator doctor --profile large --state-dir "$HOME/.local/state/rolling-context-beta"
./hermes-circulator run --profile large --state-dir "$HOME/.local/state/rolling-context-beta" -- --toolsets terminal,file,todo
./hermes-circulator status --profile large --state-dir "$HOME/.local/state/rolling-context-beta"
```

Doctor should show `installed_wrapper_matches_source: true` after the optional installer has linked this package, `provenance_errors: []`, `provenance_status: ok`, and compatible server checks. With a direct launcher and no symlink installation, an installed-wrapper warning may be expected; shim/runtime source must still match. A historical profile-marker warning is diagnostic, not evidence that a stale shim is safe.

Run a disposable project task with verifiable file changes and tests. Record selected source IDs, compaction job IDs, READY and admission events, exact relay prompt count and completed provider responses. Continue until at least three jobs have been admitted for broader beta evidence. Avoid using confidential project content in a shareable report.

Exit Hermes normally or interrupt with Ctrl-C. Confirm the owned relay exits while existing model servers and foreign processes remain alive. For rollback:

```bash
./hermes-circulator disable --profile large --state-dir "$HOME/.local/state/rolling-context-beta"
```

Do not run campaign scripts for basic setup; some launch models or require private inputs. See BETA_TESTING for controlled live failures and restart tests.
