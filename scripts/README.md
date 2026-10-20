# AutoAlpha factor-mining scripts

This repository is a standalone factor-mining deployment mirror. The scripts here are
not the full QuantMind operations tree; use the canonical entrypoints below.

## Start order

1. Prepare PostgreSQL and create the protected WSL runtime environment. Run this
   once as the service user; the prompts hide provider/API and database passwords,
   and the script generates the internal and JWT secrets:

   ```bash
   wsl bash /mnt/d/git/autoalpha/scripts/wsl/init-runtime-env.sh
   ```

   It creates `~/.config/autoalpha/runtime.env` with mode `0600` and refuses to
   overwrite an existing file. If the file exists but is missing required values,
   use `wsl bash /mnt/d/git/autoalpha/scripts/wsl/init-runtime-env.sh --complete`;
   this preserves existing entries and atomically adds only missing credentials.
   For manual setup, see `config/runtime.env.example`.
2. Start the development backend in WSL:

   ```bash
   wsl bash /mnt/d/git/autoalpha/scripts/wsl/start-dev.sh
   ```

3. Start the slim Vite frontend in Windows PowerShell:

   ```powershell
   D:\git\autoalpha\scripts\dev-web.cmd
   ```

The backend uses Python 3.12 and PostgreSQL. The local development entrypoint defaults
to `AUTH_DISABLED=1`; keep the Vite host on loopback. Do not expose this no-login setup
to an untrusted network.

## Verification

Run the Windows checks from PowerShell:

```powershell
powershell -File D:\git\autoalpha\scripts\verify-all.ps1
powershell -File D:\git\autoalpha\scripts\web-smoke.ps1
powershell -File D:\git\autoalpha\scripts\no-auth-smoke.ps1
```

Run backend and data checks inside WSL:

```bash
wsl bash /mnt/d/git/autoalpha/scripts/wsl/status.sh
wsl bash /mnt/d/git/autoalpha/scripts/wsl/run-verify.sh
wsl bash /mnt/d/git/autoalpha/scripts/wsl/pg-check.sh
wsl bash /mnt/d/git/autoalpha/scripts/wsl/quantdb-check.sh
wsl bash /mnt/d/git/autoalpha/scripts/wsl/task-lease-smoke.sh
```

## Script groups

- `alpha_agent/`: paper-factor import, statistical/RD-Agent runners, naming, and
  factor pipeline utilities. Runner paths are referenced from repository root by
  algorithm manifests; do not move them without updating those manifests.
- `wsl/`: canonical runtime, database, cache, data-link, environment, and verification
  scripts. Historical probes, old-auth smoke checks, task-specific monitors, obsolete
  package installers, and the `/evolve`-creating legacy DB smoke are in `wsl/archive/`.
  Use the start and verification commands above for routine operation.
- `dev-web.cmd`: starts the slim frontend from `web/` using the checked-in lockfile
  and the configured npmmirror registry.
- `verify-all.ps1`, `web-smoke.ps1`, `no-auth-smoke.ps1`: Windows-side deployment
  verification and frontend proxy checks.

`run-wsl.sh` keeps authentication enabled, but this mirror has no login route or
configured trusted auth gateway, so it is not a usable production entrypoint yet.
The supported workflow is local-only `scripts/wsl/start-dev.sh` with `AUTH_DISABLED=1`.

## Local backend service and health probe

Systemd templates are provided for the local no-login setup. The backend unit runs
`start-dev.sh` as `qusong`, reads the protected runtime env file, and restarts on
process exit. A one-minute timer checks `/health`, including a bounded PostgreSQL
`SELECT 1`; failures are recorded at critical priority in the system journal. This
is a local journal alert, not an email or push notification, and the database check
does not verify write permissions. Keep `AUTH_DISABLED=1` limited to the trusted
local machine.
The backend also prunes terminal task-log directories older than 90 days at startup
and once per day; active and queued task directories are retained.

After stopping any manually started backend that already owns port 6789, install the
units from WSL:

```bash
sudo install -m 0644 /mnt/d/git/autoalpha/scripts/systemd/autoalpha-{backend.service,healthcheck.service,healthcheck.timer,health-alert.service} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now autoalpha-backend.service autoalpha-healthcheck.timer
sudo systemctl status autoalpha-backend.service autoalpha-healthcheck.timer --no-pager
journalctl -u autoalpha-healthcheck.service -u autoalpha-health-alert.service -n 40 --no-pager
```

The backend unit assumes this checkout is `/mnt/d/git/autoalpha`, Python 3.12 is at
`/home/qusong/autoalpha-venv312`, and secrets are in
`/home/qusong/.config/autoalpha/runtime.env`. The templates are not enabled by this
repository; install them only when systemd should own the backend lifecycle.

## PostgreSQL backup and restore rehearsal

`wsl/pg-backup.sh` writes a compressed custom-format dump, a SHA256 sidecar, and prunes
old dumps after a successful backup. It fails closed unless `AUTOALPHA_BACKUP_DIR` is
an owned, private POSIX directory on a filesystem different from PostgreSQL's data
filesystem. Set that path and `AUTOALPHA_BACKUP_RETENTION_DAYS` in the protected
`~/.config/autoalpha/runtime.env`; do not use a repository directory or an unprotected
DrvFS mount.

The supplied systemd unit runs nightly at 02:30. It assumes this checkout is at
`/mnt/d/git/autoalpha` and the runtime env file is `/home/qusong/.config/autoalpha/runtime.env`:

```bash
sudo install -m 0644 /mnt/d/git/autoalpha/scripts/systemd/autoalpha-pg-backup.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now autoalpha-pg-backup.timer
sudo systemctl start autoalpha-pg-backup.service
journalctl -u autoalpha-pg-backup.service -n 40 --no-pager
```

To rehearse a restore, choose a new database name with the required
`autoalpha_restore_` prefix. The script verifies the dump checksum, refuses to
overwrite an existing database, restores into the scratch database, and leaves it
available for manual inspection:

```bash
AUTOALPHA_RESTORE_DB=autoalpha_restore_20261018 \
  bash /mnt/d/git/autoalpha/scripts/wsl/pg-restore-check.sh /path/to/autoalpha-quantmind-<timestamp>.dump
```

This covers the PostgreSQL database only. QuantDB's separate 111 GB dataset still
needs its own replica/backup destination and restore procedure.
