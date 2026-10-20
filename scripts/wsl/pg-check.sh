#!/bin/bash
# 校验 PG 侧建表结果 + 服务健康
set -euo pipefail
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck disable=SC1091
. "$SCRIPT_DIR/load-runtime-env.sh" || exit 1

APP_USER=${PGUSER_APP:-quantmind}
APP_PASS=${PGPASS_APP:-${DB_PASSWORD:-}}
APP_DB=${PGDB_APP:-quantmind}
if [ -z "$APP_PASS" ]; then
  echo "FATAL: 请通过 PGPASS_APP、DB_PASSWORD 或 DATABASE_URL 提供数据库密码。" >&2
  exit 1
fi

echo "== 服务 /health =="
curl -s http://127.0.0.1:6789/health | python3 -m json.tool 2>/dev/null || echo "(服务未起)"

echo
echo "== PG 表清单 =="
PGPASSWORD="$APP_PASS" psql -h 127.0.0.1 -U "$APP_USER" -d "$APP_DB" -c "\dt" 2>&1 | head -n 40

echo
echo "== 挖掘任务中心的幂等去重索引（PG 部分唯一索引是否建成）=="
PGPASSWORD="$APP_PASS" psql -h 127.0.0.1 -U "$APP_USER" -d "$APP_DB" -tAc \
  "SELECT indexname FROM pg_indexes WHERE tablename='rd_agent_mining_tasks';" 2>&1

echo
echo "== 表数量 =="
PGPASSWORD="$APP_PASS" psql -h 127.0.0.1 -U "$APP_USER" -d "$APP_DB" -tAc \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';" 2>&1
