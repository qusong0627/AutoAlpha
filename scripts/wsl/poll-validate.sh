#!/bin/bash
# 轮询任务因子的验证结果（ic/icir/sharpe/status）。
set -uo pipefail
TID=${1:-9f57d49edb124840}
ITERS=${2:-18}
INTERVAL=${3:-30}
PY=/home/qusong/autoalpha-venv312/bin/python
D=/data/alpha_agent_logs/$TID

for i in $(seq 1 "$ITERS"); do
  printf '\n[%s] ' "$(date +%H:%M:%S)"
  "$PY" - "$TID" <<'PY'
import json, sys, urllib.request
tid = sys.argv[1]
with urllib.request.urlopen(
    f"http://127.0.0.1:8001/api/v1/alpha-agent/factors?task_id={tid}&limit=50", timeout=20
) as r:
    items = json.loads(r.read().decode())["data"].get("factors") or []
done = 0
for f in items:
    ic, icir, sh = f.get("ic"), f.get("icir"), f.get("sharpe")
    st = f.get("status")
    if ic is not None or st not in (None, "pending"):
        done += 1
    print(f"    {f.get('factor_name'):20s} status={st!s:10s} ic={ic} icir={icir} sharpe={sh}")
print(f"    -> 有结果/已定态: {done}/{len(items)}")
PY
  tail -n 1 "$D/validate.log" 2>/dev/null | sed 's/^/    vlog| /'
  [ "$i" -lt "$ITERS" ] && sleep "$INTERVAL"
done
