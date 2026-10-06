# Configuration reference

Runtime config belongs in the prepared isolated profile's `config.yaml`, under `context.local_rolling`. The package examples contain no credentials and are inert until deliberately applied to a prepared profile. `prepare` derives the state path and shim from the actual installation, and copies preferences from the user's existing Hermes config. Keep those generated paths; merge reviewed policy values rather than copying a whole example over an existing profile. Stop the beta session before editing its config. `prepare --refresh` backs up and regenerates config, which can reset overrides.

## Defaults versus the measured example

`rolling_context/defaults.py` defines VALIDATED_POLICY; `provenance.effective_settings` adds base URLs, profile, state/cache and overhead; `cli.generated_config` builds the isolated Hermes configuration. These are different layers. The generic policy defaults to baseline compaction, with Loop Guard and Generation Governor off. Preparing LARGE explicitly selects Librarian and agent reasoning effort medium, but leaves those control toggles off. The audited effective LARGE profile enabled Loop Guard and Generation Governor and disabled Execution State. `examples/large-generated-defaults.yaml` shows fresh LARGE; `examples/large-known-good.yaml` shows that known-good policy. They are examples, not universal defaults, and neither migrates a production profile automatically.

## Routing and physical controls

| Setting | Source/prepared behavior | Known-good LARGE example / meaning |
|---|---|---|
| `profile` | `large` effective default; CLI accepts fast/large | `large`; must match profile directory and relay |
| `state_dir` | default `~/.local/state/local-ai-control-centre/rolling-context`; prepare resolves absolute | Use fresh `~/.local/state/rolling-context-beta` for testing; private durable state |
| `main_url` | LARGE loopback 8084; FAST 8082 | `http://127.0.0.1:8084`; main tokenizer/provider |
| `compactor_url` | loopback 8083 | `http://127.0.0.1:8083`; optional CPU worker |
| `compactor_model` | generated alias `qwen35-4b-compactor` | Qwen3.5-4B, exact alias |
| `compactor_context_length` | generated 65,536 | 65,536; CPU verifier currently hard-checks this |
| `model.default`, `model.provider`, `model.api_mode` | generated fixed alias/custom/chat_completions | `qwen38-27b-atx-73k`, custom, chat_completions |
| `model.base_url`, matching `custom_providers[].base_url` | generated relay URL | `http://127.0.0.1:8094/v1` |
| `model.context_length`, provider context | LARGE 73,728; FAST 65,536 | 73,728 physical context, not tunable merely by changing YAML |
| `custom_providers[].extra_body.max_tokens` | 8,192 | 8,192; this is the actual Hermes output-route setting, not `model.max_tokens` |
| `generation_reserve_tokens` | 8,192 | 8,192; preserve globally; recovery is request-local |
| safety margin | fixed 1,024 in code, no YAML knob | 1,024; final normal LARGE input allowance 64,512 |
| main alias / relay / upstream ports | CLI and relay profile tables | LARGE `qwen38-27b-atx-73k`: 8094 -> 8084; FAST `qwen38-27b-atx`: 8092 -> 8082 |
| `compression.enabled` | generated false; required by doctor/run | false; native Hermes compression must not mutate canonical history |
| `compression.micro_compact`, `proactive_prune_tokens`, `idle_compact_after_seconds` | false / 0 / 0 | avoid competing compression/pruning |
| `agent.reasoning_effort` | generated LARGE medium | main-agent setting; CPU thinking remains off |
| `fallback_model` | empty list | no silent alternate routing |
| `plugins`, `mcp_servers` | generated plugin preferences reset and empty MCP servers | native engine shim is separate from general plugins |

Changing `main_url` alone does not change the relay's hardcoded upstream. Current relay uses local HTTP, fixed profile ports/aliases/context, and llama.cpp template/tokenizer endpoints. CPU checks currently look for one `llama-server` on port 8083 with GPU layers `0`, reasoning `off`, alias and context verified through endpoints. Generic remote/authenticated backends are not drop-in supported.

## Working context and memory

All token values below refer to the relevant engine representation/content budgets, not necessarily the final templated wire count.

| `context.local_rolling` key | Policy default / known-good example | Purpose |
|---|---|---|
| `mode` | circulator | current context mode |
| `selection_policy` | coherent | bounded coherent selection; legacy stable/circulator paths exist |
| `semantic_policy` | cold_chunks | compact cold source episodes, independently of prompt shedding |
| `segmentation_policy` | coherent | coherent grouping; legacy tool_safe path exists |
| `recall_policy` | macro | coherent recall selection |
| `target_tokens` | 32,000 | useful content target; physical safety is separate |
| `trigger_tokens` | 36,000 | ends occupancy epoch / triggers selection pressure |
| `tail_tokens` | 19,200 | preferred recent tail |
| `minimum_tail_tokens` | 8,000 | tail floor subject to structure and physical admission |
| `segment_max_tokens` | 16,000 | soft source macrosegment ceiling |
| `recall_budget_tokens` | 14,400 | historical recall budget |
| `warm_budget_tokens` | 4,000 | warm summary budget |
| `global_budget_tokens` | 2,048 | pinned/global anchor budget |
| `chunk_min`, `chunk_target`, `chunk_max` | 4,000 / 8,000 / 12,000 | cold CPU chunks; positive ordered bounds required |
| `warm_max_blocks` | 2 | cap on warm memory blocks |
| `continuity_retained_fraction` | 0.85 | prior context retention accounting across boundaries |
| `task_state_budget_tokens` | 1,024 | bounded derived task continuity state |
| `ram_cache_bytes` | 67,108,864 | 64 MiB bounded cache, not durable authority |
| `overhead_reserve` | 8,192 | initial conservative overhead guess; measured schema overhead follows |
| `compact_source_aliases` | true | short source aliases during summarisation; validate expanded identities |
| `summary_max_tokens` | 1,536 | baseline compactor output cap; not Librarian cap |
| `summary_memory_max_tokens` | 4,096 | canonical stored/expanded memory cap |
| `compactor_mode` | baseline generic; Librarian in fresh LARGE | known-good `librarian`; A/B switch without changing physical safety |
| `librarian_max_tokens` | 4,096 | Librarian output allowance |
| `librarian_fallback_reserve_seconds` | 180 | requested baseline fallback reserve, capped at one third of total deadline |
| `job_timeout` | 600 seconds | total bounded compaction lifetime |
| `file_workspace` | absent; current working directory | root for relative current-file snapshots; absolute local path if supplied, never commit personal value |

`auxiliary.compression` is generated as custom CPU alias, `/v1` URL on 8083, 65,536 context, timeout 600, reasoning_effort none and extra_body max_tokens 1,536 / `chat_template_kwargs.enable_thinking: false`. The native Librarian uses its separate 4,096 allowance; do not mistake the auxiliary baseline output cap for its cap. Main/server launch settings remain external and are not tuned by packaging.

## Loop Guard, Governor and Execution State

Nested values belong under `context.local_rolling`.

| Key | Source default | Known-good example / constraint |
|---|---|---|
| `loop_guard.enabled` | false | true in measured profile |
| `loop_guard.task_mode` | auto | auto; alternatives implementation/analysis |
| `loop_guard.no_progress_turns` | 5 | 5; validated range 5..20 |
| `loop_guard.cooldown_turns` | 6 | 6; range 3..30 |
| `loop_guard.max_control_tokens` | 250 | 250; range 100..250 |
| `generation_governor.enabled` | false | true in measured profile |
| `generation_governor.soft_tokens` | 2,816 | numeric soft threshold; range 2,500..4,000 |
| `generation_governor.hard_tokens` | 3,840 | numeric hard threshold; range 3,500..8,192; soft <= hard |
| `generation_governor.no_action_seconds` | 90 | range 60..300 |
| `generation_governor.max_retries` | 1 | range 0..1; never unbounded |
| `generation_governor.retry_thinking` | false | thinking disabled on bounded recovery by default |
| `generation_governor.useful_visible_chars` | 32 | useful visible output threshold, range 1..64 |
| `execution_state.enabled` | false | false in known-good profile |
| `execution_state.max_tokens` | 250 | range 100..250; optional transient EXPLORE/IMPLEMENT/DEBUG controls |

Control toggles do not authorise hidden reasoning persistence. Loop Guard is request-boundary evidence-based intervention; Generation Governor watches streaming numeric/output progress and can cancel/retry one request. Their telemetry and failure modes differ. Enabling Generation Governor also enables visible inspection tracking; inspect source before changing interplay.

## Advanced / legacy controls (keep disabled or unchanged)

These are developer controls rather than newly recommended tunings. Defaults come from call-site fallback values when absent.

| Key | Fallback / constraint | Purpose |
|---|---|---|
| `schema_fallback_tokens` | 16,384 (minimum nonzero) | conservative cost when tools cannot be measured |
| `tool_schema_fingerprint` | absent | identify observed schema; do not invent a fingerprint |
| `tools_enabled` | unknown unless explicitly supplied | only explicit false may imply no tools |
| `schema_request_class` | absent | identifies explicit auxiliary-zero-tools requests |
| `burst_quiet_tokens`, `burst_quiet_turns`, `burst_cooldown_turns` | 2,000 / 3 / 3 | coherent burst settling and cooldown |
| `eviction_batch_tokens` | 4,000 | legacy selection low-water batch |
| `elastic.enabled` | false | experimental target adaptation; leave disabled |
| `elastic.min_tokens`, `max_tokens` | required when enabled | must contain target within 28,000..44,000 |
| `elastic.step_tokens`, `cooldown_turns` | 2,000 / 8 | adaptation step and cooldown |
| `elastic.grow_turns`, `shrink_turns` | 3 / 6 | sustained pressure versus fresh-prefill evidence |
| `elastic.cache_floor`, `prefill_floor_ms` | 0.5 / 10,000 | cache/prefill thresholds |
| legacy `governor.enabled` | absent/disabled | older boundary reminder; distinct from Generation Governor |
| legacy `governor.repeat_read_turns`, `soft_reasoning_ms` | 4 / 30,000 | older visible boundary feedback thresholds |

Pins/global anchors and native retrieval inputs are operator/tool evidence, not substitutes for current disk or immutable RAW. Never put personal absolute paths, credentials or live history in repository configs.

## CLI options

Commands: prepare, doctor, run, status, enable, disable. All accept `--profile {fast,large}`, `--state-dir PATH`, `--hermes PATH`. Prepare adds `--source-home PATH`, `--refresh`, `--copy-secrets`. Run forwards chat arguments after `--` but refuses model/provider/base-url/api-key/profile overrides and non-chat subcommands. Installer supports `--no-prepare`, `--profile`, `--state-dir`, `--copy-secrets`; its default prepares both profiles. Verifier supports `--doctor`, `--profile`, `--state-dir`; offline use does not require model servers. Uninstaller supports `--state-dir` and destructive `--remove-state` with explicit interactive confirmation. Keep test state separate from default production state.
