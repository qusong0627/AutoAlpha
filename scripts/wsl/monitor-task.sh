#!/bin/bash
# 监视一次挖掘任务：轮询任务状态 + tail 子进程日志。
# 用法: monitor-task.sh <task_id> [iters] [interval_s]
set -uo pipefail
TID=${1:?usage: monitor-task.sh <task_id> [iters] [interval_s]}
ITERS=${2:-12}
INTERVAL=${3:-15}
PY=/home/qusong/autoalpha-venv312/bin/python
LOG=/data/alpha_agent_logs/$TID/subprocess_stdout.log

for i in $(seq 1 "$ITERS"); do
  printf '[%s] ' "$(date +%H:%M:%S)"
  "$PY" - "$TID" <<'PY'
import json, sys, urllib.request
tid = sys.argv[1]
try:
    r = urllib.request.urlopen(
        f"http://127.0.0.1:6789/api/v1/alpha-agent/tasks/{tid}", timeout=10
    )
    d = json.loads(r.read().decode())["data"]
    print(
        f"status={d.get('status')} phase={d.get('phase')} "
        f"pct={d.get('progress_pct')} loop={d.get('current_loop')}/{d.get('loop_n')} "
        f"factors={d.get('factor_count')} "
        f"err={(d.get('error') or '')[:90]!r}"
    )
except Exception as e:  # noqa: BLE001
    print("poll error:", e)
PY
  if [ -f "$LOG" ]; then
    tail -n 3 "$LOG" | sed 's/^/    | /'
  fi
  [ "$i" -lt "$ITERS" ] && sleep "$INTERVAL"
done
