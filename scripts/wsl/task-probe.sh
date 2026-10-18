#!/bin/bash
# 观察挖掘任务的生命周期与失败点（确认 PG 状态机在跑，并暴露缺什么数据）
set -euo pipefail
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck disable=SC1091
. "$SCRIPT_DIR/load-runtime-env.sh" || exit 1
: "${DB_PASSWORD:?DB_PASSWORD 或 DATABASE_URL 必须由受保护的运行环境提供}"

BASE=${BASE:-http://127.0.0.1:8001}
SECRET=${INTERNAL_CALL_SECRET:?INTERNAL_CALL_SECRET 未设置——脚本不再内置默认值，请先 export 或写入 config/runtime.env}
H=(-H "X-Internal-Call: $SECRET" -H "X-User-Id: 10000001" -H "X-Tenant-Id: default")
PGPASSWORD=$DB_PASSWORD

TASK_ID=$(PGPASSWORD=$PGPASSWORD psql -h 127.0.0.1 -U ${DB_USER:-quantmind} -d ${DB_NAME:-quantmind} -tAc \
  "SELECT task_id FROM rd_agent_mining_tasks ORDER BY created_at DESC LIMIT 1;" | tr -d '[:space:]')

echo "task_id=$TASK_ID"
echo
echo "== GET /tasks/{id} =="
curl -s "${H[@]}" "$BASE/api/v1/alpha-agent/tasks/$TASK_ID" | python3 -m json.tool 2>/dev/null | head -n 30

echo
echo "== PG 行 =="
PGPASSWORD=$PGPASSWORD psql -h 127.0.0.1 -U ${DB_USER:-quantmind} -d ${DB_NAME:-quantmind} -x -c \
  "SELECT task_id,status,error_message,updated_at FROM rd_agent_mining_tasks WHERE task_id='$TASK_ID';"

echo "== 子进程日志尾部 =="
LOG="/data/alpha_agent_logs/$TASK_ID/subprocess_stdout.log"
[ -f "$LOG" ] || LOG="/tmp/alpha_agent_logs/$TASK_ID/subprocess_stdout.log"
if [ -f "$LOG" ]; then tail -n 25 "$LOG"; else echo "(无日志: $LOG)"; ls -la /tmp/alpha_agent_logs 2>/dev/null | head; fi
