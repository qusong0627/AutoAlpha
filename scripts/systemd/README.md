# systemd deployment files

These units target the single-user WSL development deployment described in
`PRELAUNCH_REVIEW.md`. Review the unit environment before installing them; the
backend unit deliberately uses `AUTH_DISABLED=1` and must remain local-only.

## journald retention

`60-autoalpha-journald.conf` sets system journal storage to at most 2 GiB, keeps
at least 5 GiB free on its filesystem, caps volatile journal storage at 512 MiB,
and expires journal files after 30 days. These settings apply to the entire WSL
distribution, including PostgreSQL and other system services, not only AutoAlpha.
The existing per-task log cleanup remains independently set to 90 days.

Preview or install from the repository root:

```bash
wsl bash /mnt/d/git/autoalpha/scripts/wsl/install-journald-limits.sh
wsl -u root bash /mnt/d/git/autoalpha/scripts/wsl/install-journald-limits.sh --apply
```

The installer refuses to overwrite a different file at its target and verifies
the merged settings after restarting journald. It does not vacuum existing logs.

To revert, remove only the installed drop-in and restart journald:

```bash
sudo rm -- /etc/systemd/journald.conf.d/60-autoalpha.conf
sudo systemctl restart systemd-journald
```

The repository template does not change the host by itself. After installation,
verify the merged settings and journal usage again; `SystemMaxUse` and
`SystemKeepFree` are upper/free-space controls, while `MaxRetentionSec` adds a
time-based bound. See the [systemd `journald.conf(5)` manual](https://www.freedesktop.org/software/systemd/man/latest/journald.conf.html).

## Windows host volume space check

`autoalpha-diskcheck.timer` checks `/mnt/c` every six hours because the WSL
`ext4.vhdx` grows on the Windows system volume even while WSL's own `df /`
reports ample virtual capacity. The service defaults to a 20 GiB free-space
threshold; override `AUTOALPHA_MIN_HOST_FREE_GIB` in the unit for this machine.
Below the threshold the check fails and `autoalpha-disk-alert.service` records a
critical journal entry. This is a local journal alert, not an external
notification. Preview or install the units from the repo root:

```bash
wsl bash /mnt/d/git/autoalpha/scripts/wsl/install-diskcheck.sh
wsl -u root bash /mnt/d/git/autoalpha/scripts/wsl/install-diskcheck.sh --apply
```

The installer refuses to overwrite a different unit file and enables the timer
only after installing and reloading all three units. Review the threshold and
mount before applying.

## Backend health probe

The `autoalpha-healthcheck.timer` and `autoalpha-health-alert.service` check
`http://127.0.0.1:8001/health` every minute and record a critical journal entry
when the probe fails. This monitors a backend running manually as well as one
managed by systemd; it does not restart the backend or send an external alert.
Install from the repository root:

```bash
wsl bash /mnt/d/git/autoalpha/scripts/wsl/install-health-monitor.sh
wsl -u root bash /mnt/d/git/autoalpha/scripts/wsl/install-health-monitor.sh --apply
```

## PostgreSQL backup

The backup target is `E:\AutoAlphaBackups`, mounted in WSL at
`/mnt/e/AutoAlphaBackups`. Its Windows DACL is protected from inheritance and
grants access only to the current Windows account, SYSTEM, and Administrators.
The backup script verifies that ACL before each run; it refuses other DrvFS
targets whose POSIX mode bits cannot prove privacy. PostgreSQL custom-format
dumps are not encrypted by this script, so keep the target ACL intact and
include the E: disk in recovery planning.

Preview or install from the repository root:

```bash
wsl bash /mnt/d/git/autoalpha/scripts/wsl/install-pg-backup.sh
wsl -u root bash /mnt/d/git/autoalpha/scripts/wsl/install-pg-backup.sh --apply
```

The installer checks the systemd units, runs and verifies one backup before
enabling the nightly timer, and records failures in the local journal. A scratch
database restore can be rehearsed with `scripts/wsl/pg-restore-check.sh`; the
script leaves the scratch database for inspection. This protects the PostgreSQL
database on C: against a single-disk failure, but it does not back up the 111 GB
QuantDB on E: or protect against failure of the E: disk itself.
