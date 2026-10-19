# Mirror divergence inventory

Compared local file contents with the committed `next` tree using Git blob SHA-1 object IDs. The upstream checkout is queried read-only with `git ls-tree` and `git show`.

Scope: `backend/`, `config/`, `docs/`, `alphaagent/`, `requirements/`, `scripts/`, plus `.npmrc`, `AGENTS.md`, `README.md`, `pyproject.toml`, `run-wsl.sh`, and root factor-mining requirements files. Excluded generated `__pycache__` / `.pyc`, `node_modules`, `dist`, and runtime env files.

Within this scope: **80 common files differ at the byte level**; after CRLF/LF normalization, **22 are newline-only differences and 58 still differ in content**. There are **88 mirror-only files** and **1797 upstream-only files**. `web/` is a purpose-built local app and is not byte-compared. The upstream `electron/` app is intentionally omitted per `AGENTS.md`.

## Common files with content differences

- `AGENTS.md`
- `backend/scripts/export_factor_report.py`
- `backend/scripts/factor_dedup_report.py`
- `backend/scripts/factor_deep_dive.py`
- `backend/scripts/factor_regime_analysis.py`
- `backend/scripts/md_to_pdf_report.py`
- `backend/scripts/mining_factor_validate.py`
- `backend/scripts/mining_pool_rebuild.py`
- `backend/scripts/promote_rd_mined.py`
- `backend/scripts/rd_mined_materialize.py`
- `backend/services/api/routers/admin/__init__.py`
- `backend/services/api/routers/admin/alpha_factor_pipeline.py`
- `backend/services/engine/alpha_agent/data_snapshot.py`
- `backend/services/engine/alpha_agent/direction_decompose.py`
- `backend/services/engine/alpha_agent/doc_credentials.py`
- `backend/services/engine/alpha_agent/doc_mining_settings.py`
- `backend/services/engine/alpha_agent/doc_organize.py`
- `backend/services/engine/alpha_agent/doc_parse_service.py`
- `backend/services/engine/alpha_agent/factor_codegen.py`
- `backend/services/engine/alpha_agent/launcher.py`
- `backend/services/engine/alpha_agent/llm_client.py`
- `backend/services/engine/alpha_agent/mineru_client.py`
- `backend/services/engine/alpha_agent/mining_config.py`
- `backend/services/engine/alpha_agent/task_store.py`
- `backend/services/engine/data_platform/quantdb_factor_reader.py`
- `backend/services/engine/data_platform/quantdb_hub.py`
- `backend/services/engine/factor_backtest/engine.py`
- `backend/services/engine/factor_backtest/router.py`
- `backend/services/engine/factor_backtest/store.py`
- `backend/services/engine/factor_report/datasets.py`
- `backend/services/engine/factor_report/service.py`
- `backend/services/engine/factor_research/discovery.py`
- `backend/services/engine/factor_research/service.py`
- `backend/services/engine/mining_plugins/pool_service.py`
- `backend/services/engine/minute_qlib_provider.py`
- `backend/services/engine/qlib_app/services/__init__.py`
- `backend/services/engine/qlib_app/services/rd_agent_persistence.py`
- `backend/services/engine/rd_agent/llm_env.py`
- `backend/services/engine/rd_agent/market_adapters/a_share.py`
- `backend/services/engine/rd_agent/mining_datasets.py`
- `backend/services/engine/rd_agent/qlib_conf_template.py`
- `backend/services/engine/rd_agent/rd_loop_wrapper.py`
- `backend/services/engine/routers/alpha_agent.py`
- `backend/services/engine/routers/alpha_agent_docs.py`
- `backend/services/engine/routers/rd_agent.py`
- `backend/shared/auth.py`
- `backend/shared/export_disclaimer.py`
- `backend/shared/request_id.py`
- `backend/shared/runtime_secrets.py`
- `config/monitoring_config.json`
- `config/unified/environments/development.yaml`
- `config/unified/environments/production.yaml`
- `config/unified/services/ai-strategy.yaml`
- `config/unified/shared/security.yaml`
- `pyproject.toml`
- `scripts/README.md`
- `scripts/alpha_agent/run_rd_agent.py`
- `scripts/alpha_agent/run_statistical_mining.py`

## Reviewed intentional content divergences

These entries were checked against the local deployment decisions and the actual changed lines. The remaining content differences are still unclassified; this section is not approval to synchronize or overwrite them.

| Path(s) | Intent |
|---|---|
| `backend/scripts/export_factor_report.py`; `backend/scripts/factor_dedup_report.py`; `backend/scripts/factor_deep_dive.py`; `backend/scripts/factor_regime_analysis.py`; `backend/scripts/md_to_pdf_report.py` | Replace report branding from `QuantMind` to `AutoAlpha`; no calculation logic changed in the reviewed hunks. |
| `config/monitoring_config.json`; `config/unified/environments/development.yaml`; `config/unified/environments/production.yaml`; `config/unified/services/ai-strategy.yaml`; `config/unified/shared/security.yaml` | Remove baked-in mail, database, and JWT credential defaults; require environment-provided values or leave them empty. |
| `AGENTS.md` | Local deployment and mirror-safety instructions; intentionally maintained only in this mirror. |
| `backend/services/api/routers/admin/__init__.py`; `backend/services/api/routers/admin/alpha_factor_pipeline.py`; `backend/services/engine/qlib_app/services/__init__.py`; `backend/shared/auth.py` | Standalone deployment deltas recorded in `AGENTS.md`: omit unshipped admin routers, bound promote compute, lazy-load the backtest stack, and retire the public development internal secret. |
| `scripts/README.md` | Replace upstream script guidance with the actual WSL/Windows setup and verification workflow for this mirror. |
| `backend/services/engine/alpha_agent/direction_decompose.py`; `backend/services/engine/alpha_agent/doc_credentials.py`; `backend/services/engine/alpha_agent/doc_mining_settings.py`; `backend/services/engine/alpha_agent/doc_organize.py`; `backend/services/engine/alpha_agent/doc_parse_service.py`; `backend/services/engine/alpha_agent/factor_codegen.py`; `backend/services/engine/alpha_agent/launcher.py`; `backend/services/engine/alpha_agent/llm_client.py`; `backend/services/engine/alpha_agent/mineru_client.py`; `backend/services/engine/rd_agent/llm_env.py`; `backend/services/engine/routers/alpha_agent.py`; `backend/services/engine/routers/alpha_agent_docs.py`; `backend/shared/runtime_secrets.py` | Keep LLM, Embedding, and MinerU credentials in the server environment; remove Profile/Redis credential resolution and writes; sanitize endpoint reporting and secret-bearing errors/logs. |
| `backend/services/engine/alpha_agent/task_store.py` | Add task owner and lease fields, owner-fenced writes, expired-lease recovery, and clear the lease timestamp when recovery finalizes an orphan. The schema migration is applied locally; queued takeover, duplicate-start fencing, and expired-task recovery still require fault-path verification. |
| `backend/services/engine/alpha_agent/data_snapshot.py` | Extend task-level data provenance snapshots to recognize the shared dataset path registry and flat parquet datasets; this records observed files/partitions and is fail-open for task creation. |
| `backend/services/engine/alpha_agent/mining_config.py` | Add shared mining/validation window and frequency normalization, sample-outside-window checks, data coverage validation, and the qlib end-of-data guard. These are local mining workflow changes. |
| `backend/services/engine/factor_backtest/store.py` | Extend the backtest ledger for run parameters, additional terminal states, series payloads, and batch tracking. Runtime DDL compatibility and existing-data migration remain to be verified against PostgreSQL. |
| `backend/services/engine/data_platform/quantdb_factor_reader.py` | Treat configured factor sources with a currently missing directory as configured-but-not-ready, so listing sources does not fail the whole request. |
| `backend/services/engine/data_platform/quantdb_hub.py` | Read minute bars from either legacy per-symbol parquet files or the newer date-partitioned full-market layout, with date and symbol filtering. Validate both layouts against the deployed QuantDB before relying on this path. |
| `backend/services/engine/factor_backtest/router.py`; `backend/services/engine/factor_backtest/engine.py` | Capture factor-source provenance in backtest rows and evaluate A-share factor code against the resolved mining source panel. |
| `backend/services/engine/factor_report/service.py`; `backend/services/engine/factor_research/service.py` | Append timestamped build/promote subprocess output to persistent log files instead of keeping output only in the parent process. Confirm log permissions and rotation for long-running use. |
| `backend/services/engine/rd_agent/qlib_conf_template.py` | Run generated RD-Agent factor code through bubblewrap with network unshared, credential variables removed, runtime env files hidden, and only the task workspace writable; fail closed when bubblewrap or the isolated runner is unavailable. Runtime execution still needs verification in the target deployment environment. |
| `backend/services/engine/routers/rd_agent.py` | Validate backtest date order, cap lightweight backtests to one concurrent task, and run generated factor code in a separate isolated child process with timeout and resource bounds. Verify timeout/cancellation races and process cleanup at runtime. |
| `backend/shared/request_id.py` | Add bounded character validation to caller-supplied `X-Request-ID` values so malformed or log-injection strings are replaced with a generated UUID before response headers and access logs. |
| `backend/services/engine/rd_agent/rd_loop_wrapper.py`; `backend/services/engine/rd_agent/mining_datasets.py`; `backend/services/engine/rd_agent/market_adapters/a_share.py`; `backend/services/engine/minute_qlib_provider.py` | Move A-share mining data access from generated H5 files to task-scoped parquet panels plus on-demand QuantDB dataset reads; add explicit dataset/column mappings and minute-panel construction. Other markets retain their existing paths. Qlib path repair compares resolved symlink targets and preserves existing directories without deleting a colliding backup. This is a substantial local data-pipeline redesign and needs end-to-end data-backed verification. |
| `backend/scripts/rd_mined_materialize.py`; `backend/scripts/promote_rd_mined.py`; `backend/services/engine/data_platform/quantdb_factor_reader.py`; `backend/services/engine/factor_report/datasets.py`; `backend/services/engine/factor_research/discovery.py` | Serialize materialization/promotion and mark an in-progress target as unavailable until the full copy and manifest are complete; block training/report/research readers from incomplete data. The promoter rejects symlink dataset roots, partition directories, and parquet files; copies through an exclusive same-directory temp file. Crash recovery, symlink rejection, and concurrent writer behavior still need runtime verification. |
| `backend/services/engine/data_platform/quantdb_hub.py`; `backend/scripts/rd_mined_materialize.py`; `backend/scripts/mining_factor_validate.py`; `backend/scripts/mining_pool_rebuild.py` | Adapt QuantDB reads, materialization, factor validation, and pool rebuild to the parquet panel execution path, including isolated factor-code execution and compatibility handling for old H5 inputs. End-to-end execution remains unverified because the local `rd_mined` dataset is not yet present. |
| `backend/services/engine/mining_plugins/pool_service.py` | Include Chinese factor display names in pool queries and API results, falling back to the original name or factor ID. |
| `backend/services/engine/qlib_app/services/rd_agent_persistence.py` | Add a Chinese display-name column and batch update path for persisted factors. Verify the additive DDL and existing-row behavior against PostgreSQL. |
| `scripts/alpha_agent/run_rd_agent.py`; `scripts/alpha_agent/run_statistical_mining.py` | Persist factor-source provenance for RD-Agent outputs and schedule factor naming for persisted statistical results; provenance/naming failures are treated as non-blocking. |
| `backend/shared/export_disclaimer.py` | Change generated report disclaimer branding to AutoAlpha; no calculation change identified in the reviewed hunk. |
| `pyproject.toml` | Restrict package metadata to Python 3.12, matching the factor-mining runtime and the supported `pyqlib` wheel target. The hashed deployment lock is separately scoped to Linux x86_64 / Python 3.12. |

Reviewed: **58 of 58** content-different common files. Classified by purpose: **88 of 88 mirror-only files**; archived WSL bring-up scripts are retained as historical references, not canonical deployment entrypoints. The disposable lease verifier is a validation utility and does not submit a real mining job.

## Newline-only differences

- `.npmrc`
- `alphaagent/components/coder/factor_coder/template.jinjia2`
- `alphaagent/components/coder/factor_coder/template_debug.jinjia2`
- `alphaagent/components/coder/model_coder/model_execute_template_v1.txt`
- `alphaagent/components/coder/model_coder/model_execute_template_v2.txt`
- `alphaagent/pyproject.toml`
- `backend/scripts/data/finance_sentiment_lexicon.tsv`
- `backend/shared/requirements/ai.txt`
- `backend/shared/requirements/auth.txt`
- `backend/shared/requirements/backtest.txt`
- `backend/shared/requirements/market-data.txt`
- `backend/shared/requirements/observability.txt`
- `config/postgres/master-pg_hba.conf`
- `config/postgres/master-postgresql.conf`
- `config/redis/master-redis.conf`
- `config/redis/sentinel.conf`
- `config/redis/slave-redis.conf`
- `docs/images/architecture.svg`
- `requirements/ai.txt`
- `requirements/base.txt`
- `requirements/data.txt`
- `requirements/database.txt`

## Mirror-only files

- `backend/services/engine/alpha_agent/factor_naming.py`
- `backend/services/engine/app_factor_mining.py`
- `backend/services/engine/data_platform/panel_cache.py`
- `backend/services/engine/rd_agent/factor_sources.py`
- `backend/services/engine/rd_agent/legacy_code.py`
- `backend/services/engine/rd_agent/quantdb_data.py`
- `backend/shared/secret_redaction.py`
- `config/runtime.env.example`
- `requirements-factor-mining.lock`
- `requirements-factor-mining.txt`
- `run-wsl.sh`
- `scripts/alpha_agent/name_factor_batch.py`
- `scripts/dev-web.cmd`
- `scripts/mirror-diff-inventory.py`
- `scripts/no-auth-smoke.ps1`
- `scripts/systemd/60-autoalpha-journald.conf`
- `scripts/systemd/README.md`
- `scripts/systemd/autoalpha-backend.service`
- `scripts/systemd/autoalpha-disk-alert.service`
- `scripts/systemd/autoalpha-diskcheck.service`
- `scripts/systemd/autoalpha-diskcheck.timer`
- `scripts/systemd/autoalpha-health-alert.service`
- `scripts/systemd/autoalpha-healthcheck.service`
- `scripts/systemd/autoalpha-healthcheck.timer`
- `scripts/systemd/autoalpha-pg-backup-alert.service`
- `scripts/systemd/autoalpha-pg-backup.service`
- `scripts/systemd/autoalpha-pg-backup.timer`
- `scripts/verify-all.ps1`
- `scripts/web-smoke.ps1`
- `scripts/wsl/archive/README.md`
- `scripts/wsl/archive/check-312.sh`
- `scripts/wsl/archive/db-smoke.sh`
- `scripts/wsl/archive/diag-python.sh`
- `scripts/wsl/archive/facts.sh`
- `scripts/wsl/archive/fe-smoke.sh`
- `scripts/wsl/archive/install-deps.sh`
- `scripts/wsl/archive/install-duckdb.sh`
- `scripts/wsl/archive/install-email.sh`
- `scripts/wsl/archive/install-extra.sh`
- `scripts/wsl/archive/install-jinja2.sh`
- `scripts/wsl/archive/install-passlib.sh`
- `scripts/wsl/archive/install-psycopg2.sh`
- `scripts/wsl/archive/install-pyqlib-312.sh`
- `scripts/wsl/archive/install-pyqlib.sh`
- `scripts/wsl/archive/poll-validate.sh`
- `scripts/wsl/archive/probe-pyqlib-mirrors.sh`
- `scripts/wsl/archive/probe-pyqlib.sh`
- `scripts/wsl/archive/probe-python312.sh`
- `scripts/wsl/archive/probe-quantdb.sh`
- `scripts/wsl/archive/probe-quantdb2.sh`
- `scripts/wsl/archive/probe_pyqlib_dist.py`
- `scripts/wsl/archive/smoke.sh`
- `scripts/wsl/archive/task-probe.sh`
- `scripts/wsl/archive/validate-summary.sh`
- `scripts/wsl/audit-serialized-secrets.py`
- `scripts/wsl/build-qlib-cache.sh`
- `scripts/wsl/build_qlib_cache.py`
- `scripts/wsl/disk-space-check.sh`
- `scripts/wsl/fix-data-owner.sh`
- `scripts/wsl/healthcheck.sh`
- `scripts/wsl/init-runtime-env.sh`
- `scripts/wsl/install-deps-312.sh`
- `scripts/wsl/install-diskcheck.sh`
- `scripts/wsl/install-health-monitor.sh`
- `scripts/wsl/install-journald-limits.sh`
- `scripts/wsl/install-pg-backup.sh`
- `scripts/wsl/install-rdagent.sh`
- `scripts/wsl/link-quantdb.sh`
- `scripts/wsl/load-runtime-env.sh`
- `scripts/wsl/monitor-task.sh`
- `scripts/wsl/pg-backup.sh`
- `scripts/wsl/pg-check.sh`
- `scripts/wsl/pg-count.sh`
- `scripts/wsl/pg-restore-check.sh`
- `scripts/wsl/pg-setup.sh`
- `scripts/wsl/pg-url-env.sh`
- `scripts/wsl/provide-python.sh`
- `scripts/wsl/quantdb-check.sh`
- `scripts/wsl/rebuild-venv.sh`
- `scripts/wsl/run-verify.sh`
- `scripts/wsl/setup-python312.sh`
- `scripts/wsl/start-dev.sh`
- `scripts/wsl/status.sh`
- `scripts/wsl/task-lease-smoke.py`
- `scripts/wsl/task-lease-smoke.sh`
- `scripts/wsl/verify-rdagent.sh`
- `scripts/wsl/verify-windows-backup-acl.ps1`
- `scripts/wsl/verify_mounts.py`

## Upstream-only counts by directory

- `README.md/`: 1 files omitted from the slim mirror
- `alphaagent`: 1 files omitted from the slim mirror
- `backend`: 1635 files omitted from the slim mirror
- `requirements`: 8 files omitted from the slim mirror
- `scripts`: 152 files omitted from the slim mirror

This inventory is a safety reference, not a synchronization instruction. Do not overwrite from `next` without reviewing every listed divergence. Rebuild it after future mirror or source changes with `python scripts/mirror-diff-inventory.py --upstream D:/git/quantmindoss`.
