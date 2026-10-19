#!/usr/bin/env bash
# Install the local PostgreSQL backup service and its nightly timer. Preview by default.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
UNIT_SOURCE_DIR=$(realpath -e -- "$SCRIPT_DIR/../systemd")
UNIT_TARGET_DIR=/etc/systemd/system
UNITS=(
  autoalpha-pg-backup.service
  autoalpha-pg-backup.timer
  autoalpha-pg-backup-alert.service
)

if [ "${1:-}" != "--apply" ] || [ "$#" -ne 1 ]; then
  if [ "$#" -eq 0 ]; then
    echo "Preview: install PostgreSQL backup units to $UNIT_TARGET_DIR."
    echo "The one-shot backup will run now, then nightly to /mnt/e/AutoAlphaBackups."
    echo "Pass --apply to install, run, verify, and enable the timer."
    exit 0
  fi
  echo "Usage: $0 [--apply]" >&2
  exit 2
fi
if [ "$(id -u)" -ne 0 ]; then
  echo "FATAL: run with WSL root to install system units." >&2
  exit 1
fi

for name in "${UNITS[@]}"; do
  source_file="$UNIT_SOURCE_DIR/$name"
  target_file="$UNIT_TARGET_DIR/$name"
  if [ -L "$target_file" ] || { [ -e "$target_file" ] && [ ! -f "$target_file" ]; }; then
    echo "FATAL: unit target is not a regular file: $target_file" >&2
    exit 1
  fi
  if [ -f "$target_file" ] && ! cmp -s -- "$source_file" "$target_file"; then
    echo "FATAL: existing $target_file differs; inspect it before replacing." >&2
    exit 1
  fi
done

install -d -m 0755 -- "$UNIT_TARGET_DIR"
for name in "${UNITS[@]}"; do
  source_file="$UNIT_SOURCE_DIR/$name"
  target_file="$UNIT_TARGET_DIR/$name"
  [ -f "$target_file" ] && continue
  tmp=$(mktemp "$UNIT_TARGET_DIR/.${name}.XXXXXX")
  trap 'rm -f -- "$tmp"' EXIT
  install -m 0644 -- "$source_file" "$tmp"
  mv -- "$tmp" "$target_file"
  trap - EXIT
done

systemctl daemon-reload
systemd-analyze verify \
  "$UNIT_TARGET_DIR/autoalpha-pg-backup.service" \
  "$UNIT_TARGET_DIR/autoalpha-pg-backup.timer" \
  "$UNIT_TARGET_DIR/autoalpha-pg-backup-alert.service"
if ! systemctl start autoalpha-pg-backup.service; then
  echo "FATAL: PostgreSQL backup failed; timer was not enabled." >&2
  exit 1
fi
result=$(systemctl show --property=Result --value autoalpha-pg-backup.service)
if [ "$result" != success ]; then
  echo "FATAL: PostgreSQL backup result was not successful; timer was not enabled." >&2
  exit 1
fi
systemctl enable --now autoalpha-pg-backup.timer
systemctl is-active --quiet autoalpha-pg-backup.timer
echo "PostgreSQL backup succeeded and autoalpha-pg-backup.timer is enabled."
journalctl -u autoalpha-pg-backup.service -n 4 --no-pager
