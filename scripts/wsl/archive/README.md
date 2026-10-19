# Archived WSL bring-up scripts

These scripts are retained as historical diagnostics or setup attempts. They are
not supported runtime entrypoints and are not part of the normal verification
sequence. Several assume the old Python 3.14 venv, old authentication behavior,
or a specific task ID.

Archived groups:

- `probe-*`, `probe_*.py`, `facts.sh`, `diag-python.sh`, `check-312.sh`: one-off
  environment and data-layout discovery.
- `fe-smoke.sh`, `smoke.sh`: replaced by the Windows-side `web-smoke.ps1` and
  `no-auth-smoke.ps1` checks.
- `task-probe.sh`, `poll-validate.sh`, `validate-summary.sh`: task-specific
  diagnostics with stale defaults or schema assumptions.
- `install-deps.sh`, `install-*` package helpers: superseded by the hashed
  Python 3.12 lock and `../install-deps-312.sh`.
- `db-smoke.sh`: legacy end-to-end probe that creates a real `/evolve` mining
  task. Do not use it for routine health checks.

Current entrypoints are documented in `scripts/README.md` and the repository
`AGENTS.md`. The archive is retained for reference; it is not a recommendation
to run these scripts against a live environment.
