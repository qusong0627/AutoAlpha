#!/bin/bash
# Compatibility entrypoint; all factor-mining dependencies install from the
# checked-in Python 3.12 hash lock via the canonical script.
set -euo pipefail
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec "$SCRIPT_DIR/install-deps-312.sh" "$@"
