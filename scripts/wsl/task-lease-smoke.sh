#!/bin/bash
# Verify task lease ownership and recovery with disposable PostgreSQL rows.
set -euo pipefail
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)
# shellcheck disable=SC1091
. "$SCRIPT_DIR/load-runtime-env.sh" || exit 1
VENV=${VENV:-/home/qusong/autoalpha-venv312}
if [ ! -x "$VENV/bin/python" ]; then
  echo "FATAL: Python 3.12 venv not found: $VENV" >&2
  exit 1
fi
cd "$REPO_ROOT"
exec "$VENV/bin/python" "$SCRIPT_DIR/task-lease-smoke.py"
