#!/usr/bin/env bash
set -euo pipefail

HEALTH_URL=${HEALTH_URL:-http://127.0.0.1:6789/health}
response=$(curl --fail --silent --show-error --max-time 5 "$HEALTH_URL") || {
  echo "AutoAlpha health endpoint is unreachable: $HEALTH_URL" >&2
  exit 1
}

printf '%s' "$response" | python3 -c '
import json
import sys

try:
    health = json.load(sys.stdin)
except Exception as exc:
    print(f"Invalid /health response: {type(exc).__name__}", file=sys.stderr)
    raise SystemExit(1)

if health.get("status") != "healthy" or health.get("failed"):
    failed = health.get("failed") or {}
    checks = health.get("checks") or {}
    components = sorted(
        {str(name) for name in failed}
        | {str(name) for name, status in checks.items() if status != "ok"}
    )
    components = ",".join(components) or "service status"
    print(f"AutoAlpha is degraded: {components}", file=sys.stderr)
    raise SystemExit(1)
' 
