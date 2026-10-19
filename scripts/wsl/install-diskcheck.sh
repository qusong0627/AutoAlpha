#!/usr/bin/env bash
# Install and enable the local host-volume disk check. Preview by default.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
UNIT_SOURCE_DIR=$(realpath -e -- "$SCRIPT_DIR/../systemd")
UNIT_TARGET_DIR=/etc/systemd/system
UNITS=(
  autoalpha-diskcheck.service
  autoalpha-diskcheck.timer
  autoalpha-disk-alert.service
)

if [ "${1:-}" != "--apply" ] || [ "$#" -ne 1 ]; then
  if [ "$#" -eq 0 ]; then
    echo "Preview: install three unit files to $UNIT_TARGET_DIR and enable autoalpha-diskcheck.timer."
    echo "This runs a read-only C: free-space check every six hours; pass --apply to proceed."
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
systemctl enable --now autoalpha-diskcheck.timer
systemctl start autoalpha-diskcheck.service
systemctl is-active --quiet autoalpha-diskcheck.timer
result=$(systemctl show --property=Result --value autoalpha-diskcheck.service)
if [ "$result" != success ]; then
  echo "FATAL: disk-space check failed; inspect autoalpha-diskcheck.service." >&2
  exit 1
fi
echo "Installed and enabled autoalpha-diskcheck.timer."
journalctl -u autoalpha-diskcheck.service -n 1 --no-pager
