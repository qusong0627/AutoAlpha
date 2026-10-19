#!/usr/bin/env bash
# Install the repository's global journald retention drop-in. Preview by default.
set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
SOURCE=$(realpath -e -- "$SCRIPT_DIR/../systemd/60-autoalpha-journald.conf")
TARGET_DIR=/etc/systemd/journald.conf.d
TARGET=$TARGET_DIR/60-autoalpha.conf

if [ "${1:-}" != "--apply" ] || [ "$#" -ne 1 ]; then
  if [ "$#" -eq 0 ]; then
    echo "Preview: install $SOURCE to $TARGET and restart systemd-journald."
    echo "This config is global to the WSL distribution. Pass --apply to proceed."
    exit 0
  fi
  echo "Usage: $0 [--apply]" >&2
  exit 2
fi
if [ "$(id -u)" -ne 0 ]; then
  echo "FATAL: run with WSL root to install the system journald drop-in." >&2
  exit 1
fi
if [ -L "$TARGET_DIR" ] || [ -L "$TARGET" ] || { [ -e "$TARGET" ] && [ ! -f "$TARGET" ]; }; then
  echo "FATAL: journald target directory/file is not a regular path." >&2
  exit 1
fi
if [ -f "$TARGET" ] && ! cmp -s -- "$SOURCE" "$TARGET"; then
  echo "FATAL: existing $TARGET differs; inspect it before replacing." >&2
  exit 1
fi

install -d -m 0755 -- "$TARGET_DIR"
tmp=$(mktemp "$TARGET_DIR/.60-autoalpha.conf.XXXXXX")
cleanup() { rm -f -- "$tmp"; }
trap cleanup EXIT
install -m 0644 -- "$SOURCE" "$tmp"
mv -f -- "$tmp" "$TARGET"
tmp=""
trap - EXIT

systemctl restart systemd-journald
systemctl is-active --quiet systemd-journald
merged=$(systemd-analyze cat-config systemd/journald.conf)
for setting in \
  "SystemMaxUse=2G" \
  "SystemKeepFree=5G" \
  "RuntimeMaxUse=512M" \
  "RuntimeKeepFree=1G" \
  "MaxRetentionSec=30day"; do
  if ! grep -Fqx "$setting" <<< "$merged"; then
    echo "FATAL: merged journald config is missing $setting" >&2
    exit 1
  fi
done
echo "Installed global journald limits: $TARGET"
journalctl --disk-usage
