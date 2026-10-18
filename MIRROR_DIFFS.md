# Mirror divergence inventory

Compared local file contents with the committed `next` tree using Git blob SHA-1 object IDs. The upstream checkout is queried read-only with `git ls-tree` and `git show`.

Scope: `backend/`, `config/`, `docs/`, `alphaagent/`, `requirements/`, `scripts/`, plus `.npmrc`, `AGENTS.md`, `README.md`, `pyproject.toml`, `run-wsl.sh`, and root factor-mining requirements files. Excluded generated `__pycache__` / `.pyc`, `node_modules`, `dist`, and runtime env files.

Within this scope: **74 common files differ at the byte level**; after CRLF/LF normalization, **23 are newline-only differences and 51 still differ in content**. There are **72 mirror-only files** and **1797 upstream-only files**. `web/` is a purpose-built local app and is not byte-compared. The upstream `electron/` app is intentionally omitted per `AGENTS.md`.

## Common files with content differences

- `AGENTS.md`
- `backend/scripts/export_factor_report.py`
- `backend/scripts/factor_dedup_report.py`
- `backend/scripts/factor_deep_dive.py`
- `backend/scripts/factor_regime_analysis.py`
- `backend/scripts/md_to_pdf_report.py`
- `backend/scripts/mining_factor_validate.py`
- `backend/scripts/mining_pool_rebuild.py`
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
- `backend/services/engine/factor_report/service.py`
- `backend/services/engine/factor_research/service.py`
- `backend/services/engine/mining_plugins/pool_service.py`
- `backend/services/engine/minute_qlib_provider.py`
- `backend/services/engine/qlib_app/services/__init__.py`
- `backend/services/engine/qlib_app/services/rd_agent_persistence.py`
- `backend/services/engine/rd_agent/llm_env.py`
- `backend/services/engine/rd_agent/market_adapters/a_share.py`
- `backend/services/engine/rd_agent/mining_datasets.py`
- `backend/services/engine/rd_agent/rd_loop_wrapper.py`
- `backend/services/engine/routers/alpha_agent.py`
- `backend/services/engine/routers/alpha_agent_docs.py`
- `backend/shared/auth.py`
- `backend/shared/export_disclaimer.py`
- `backend/shared/runtime_secrets.py`
- `config/monitoring_config.json`
- `config/unified/environments/development.yaml`
- `config/unified/environments/production.yaml`
- `config/unified/services/ai-strategy.yaml`
- `config/unified/shared/security.yaml`
- `scripts/README.md`
- `scripts/alpha_agent/run_rd_agent.py`
- `scripts/alpha_agent/run_statistical_mining.py`

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
- `pyproject.toml`
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
- `scripts/systemd/autoalpha-backend.service`
- `scripts/systemd/autoalpha-health-alert.service`
- `scripts/systemd/autoalpha-healthcheck.service`
- `scripts/systemd/autoalpha-healthcheck.timer`
- `scripts/systemd/autoalpha-pg-backup.service`
- `scripts/systemd/autoalpha-pg-backup.timer`
- `scripts/verify-all.ps1`
- `scripts/web-smoke.ps1`
- `scripts/wsl/audit-serialized-secrets.py`
- `scripts/wsl/build-qlib-cache.sh`
- `scripts/wsl/build_qlib_cache.py`
- `scripts/wsl/check-312.sh`
- `scripts/wsl/db-smoke.sh`
- `scripts/wsl/diag-python.sh`
- `scripts/wsl/facts.sh`
- `scripts/wsl/fe-smoke.sh`
- `scripts/wsl/fix-data-owner.sh`
- `scripts/wsl/healthcheck.sh`
- `scripts/wsl/init-runtime-env.sh`
- `scripts/wsl/install-deps-312.sh`
- `scripts/wsl/install-deps.sh`
- `scripts/wsl/install-duckdb.sh`
- `scripts/wsl/install-email.sh`
- `scripts/wsl/install-extra.sh`
- `scripts/wsl/install-jinja2.sh`
- `scripts/wsl/install-passlib.sh`
- `scripts/wsl/install-psycopg2.sh`
- `scripts/wsl/install-pyqlib-312.sh`
- `scripts/wsl/install-pyqlib.sh`
- `scripts/wsl/install-rdagent.sh`
- `scripts/wsl/link-quantdb.sh`
- `scripts/wsl/load-runtime-env.sh`
- `scripts/wsl/monitor-task.sh`
- `scripts/wsl/pg-backup.sh`
- `scripts/wsl/pg-check.sh`
- `scripts/wsl/pg-count.sh`
- `scripts/wsl/pg-restore-check.sh`
- `scripts/wsl/pg-setup.sh`
- `scripts/wsl/poll-validate.sh`
- `scripts/wsl/probe-pyqlib-mirrors.sh`
- `scripts/wsl/probe-pyqlib.sh`
- `scripts/wsl/probe-python312.sh`
- `scripts/wsl/probe-quantdb.sh`
- `scripts/wsl/probe-quantdb2.sh`
- `scripts/wsl/probe_pyqlib_dist.py`
- `scripts/wsl/provide-python.sh`
- `scripts/wsl/quantdb-check.sh`
- `scripts/wsl/rebuild-venv.sh`
- `scripts/wsl/run-verify.sh`
- `scripts/wsl/setup-python312.sh`
- `scripts/wsl/smoke.sh`
- `scripts/wsl/start-dev.sh`
- `scripts/wsl/status.sh`
- `scripts/wsl/task-probe.sh`
- `scripts/wsl/validate-summary.sh`
- `scripts/wsl/verify-rdagent.sh`
- `scripts/wsl/verify_mounts.py`

## Upstream-only counts by directory

- `README.md/`: 1 files omitted from the slim mirror
- `alphaagent`: 1 files omitted from the slim mirror
- `backend`: 1635 files omitted from the slim mirror
- `requirements`: 8 files omitted from the slim mirror
- `scripts`: 152 files omitted from the slim mirror

This inventory is a safety reference, not a synchronization instruction. Do not overwrite from `next` without reviewing every listed divergence. Rebuild it after future mirror or source changes with `python scripts/mirror-diff-inventory.py --upstream D:/git/quantmindoss`.
