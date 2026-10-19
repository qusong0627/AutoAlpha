#!/usr/bin/env bash
# Install and enable the local backend /health probe. Preview by default.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
UNIT_SOURCE_DIR=$(realpath -e -- "$SCRIPT_DIR/../systemd")
UNIT_TARGET_DIR=/etc/systemd/system
UNITS=(
  autoalpha-healthcheck.service
  autoalpha-healthcheck.timer
  autoalpha-health-alert.service
)

if [ "${1:-}" != "--apply" ] || [ "$#" -ne 1 ]; then
  if [ "$#" -eq 0 ]; then
    echo "Preview: install /health probe and alert units to $UNIT_TARGET_DIR."
    echo "This checks the local backend once per minute; pass --apply to enable the timer."
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
systemctl enable --now autoalpha-healthcheck.timer
systemctl start autoalpha-healthcheck.service
systemctl is-active --quiet autoalpha-healthcheck.timer
result=$(systemctl show --property=Result --value autoalpha-healthcheck.service)
if [ "$result" != success ]; then
  echo "FATAL: backend health probe failed; inspect autoalpha-healthcheck.service." >&2
  exit 1
fi
echo "Installed and enabled autoalpha-healthcheck.timer."
journalctl -u autoalpha-healthcheck.service -n 2 --no-pager
