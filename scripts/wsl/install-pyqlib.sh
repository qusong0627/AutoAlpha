#!/bin/bash
# Compatibility entrypoint: pyqlib is supported only in the pinned Python 3.12 venv.
set -euo pipefail
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec "$SCRIPT_DIR/install-pyqlib-312.sh" "$@"
