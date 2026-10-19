#!/bin/bash
set -uo pipefail
TID=${1:-9f57d49edb124840}
D=/data/alpha_agent_logs/$TID
LOG=$D/validate.log
PY=/home/qusong/autoalpha-venv312/bin/python

for i in $(seq 1 20); do
  n=$(grep -cE "factor-backtest.*market=a_share ok" "$LOG" 2>/dev/null || true)
  n=${n:-0}
  echo "[$(date +%H:%M:%S)] 已完成回测: $n / 7"
  [ "$n" -ge 7 ] && break
  sleep 20
done

echo
echo "=== 因子 id -> 名称 ==="
"$PY" - "$TID" <<'PY'
import json, sys, urllib.request
tid = sys.argv[1]
with urllib.request.urlopen(
    f"http://127.0.0.1:8001/api/v1/alpha-agent/factors?task_id={tid}&limit=50", timeout=20
) as r:
    items = json.loads(r.read().decode())["data"].get("factors") or []
for f in items:
    print(f"  {f.get('factor_id')}  {f.get('factor_name')}")
PY

echo
echo "=== validate.log 汇总 ==="
grep -E "factor-backtest.*(ok|failed)|验证完成|验证完成|unavailable" "$LOG" | tail -30
