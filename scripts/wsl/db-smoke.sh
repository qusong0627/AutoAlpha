#!/bin/bash
# DB 端到端冒烟：确认接口真的从 PG 读数据（不是只挂上路由）
set -euo pipefail
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck disable=SC1091
. "$SCRIPT_DIR/load-runtime-env.sh" || exit 1
: "${DB_PASSWORD:?DB_PASSWORD 或 DATABASE_URL 必须由受保护的运行环境提供}"

BASE=${BASE:-http://127.0.0.1:8001}
SECRET=${INTERNAL_CALL_SECRET:?INTERNAL_CALL_SECRET 未设置——脚本不再内置默认值，请先 export 或写入 config/runtime.env}
H=(-H "X-Internal-Call: $SECRET" -H "X-User-Id: 10000001" -H "X-Tenant-Id: default")

call() {
  local path="$1"
  local code body
  body=$(curl -s -o /tmp/db_body.json -w "%{http_code}" "${H[@]}" "$BASE$path")
  code=$body
  printf "  %s  %-52s %s\n" "$code" "$path" "$(head -c 140 /tmp/db_body.json | tr -d '\n')"
}

echo "== health（看 startup.sync_db_pool）=="
curl -s "$BASE/health" | python3 -c "
import json,sys
d=json.load(sys.stdin)
print('  status:', d['status'])
for k,v in d.get('startup',{}).items(): print(f'  {k:22s} {v}')
"

echo
echo "== PG 直读接口 =="
call /api/v1/alpha-agent/tasks/history
call /api/v1/alpha-agent/tasks
call /api/v1/alpha-agent/factors
call /api/v1/alpha-agent/pool/overview
call /api/v1/alpha-agent/variant-ledger
call /api/v1/factor-backtest/batches
call /api/v1/factor-backtest/runs
call /api/v1/factor-research/leaderboard

echo
echo "== 写后读：建一条挖掘任务（PG 落库）=="
code=$(curl -s -o /tmp/mk.json -w "%{http_code}" "${H[@]}" \
  -H 'Content-Type: application/json' \
  -d '{"query":"wsl pg e2e probe","market":"a_share","loop_n":1,"algorithm":"statistical"}' \
  "$BASE/api/v1/alpha-agent/evolve")
echo "  POST /evolve -> $code  $(head -c 200 /tmp/mk.json | tr -d '\n')"

echo
echo "== 落库确认（直查 PG）=="
PGPASSWORD=$DB_PASSWORD psql -h 127.0.0.1 -U ${DB_USER:-quantmind} -d ${DB_NAME:-quantmind} -tAc \
  "SELECT count(*), coalesce(max(status),'-') FROM rd_agent_mining_tasks;"
PGPASSWORD=$DB_PASSWORD psql -h 127.0.0.1 -U ${DB_USER:-quantmind} -d ${DB_NAME:-quantmind} -tAc \
  "SELECT task_id, status, market, algorithm FROM rd_agent_mining_tasks ORDER BY created_at DESC LIMIT 3;"
