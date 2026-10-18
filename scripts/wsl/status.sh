#!/bin/bash
# 部署状态总览
set -uo pipefail
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck disable=SC1091
. "$SCRIPT_DIR/load-runtime-env.sh" || exit 1
: "${DB_PASSWORD:?DB_PASSWORD 或 DATABASE_URL 必须由受保护的运行环境提供}"
echo "=============== Python / venv ==============="
for V in /home/qusong/autoalpha-venv312 /home/qusong/autoalpha-venv; do
  [ -x "$V/bin/python" ] && echo "  $V -> $("$V/bin/python" -V 2>&1)"
done
echo "  qlib in 312: $(/home/qusong/autoalpha-venv312/bin/python -c 'import qlib;print("yes",qlib.__version__)' 2>/dev/null || echo 'not yet')"

echo
echo "=============== PostgreSQL ==============="
systemctl is-enabled postgresql 2>/dev/null | sed 's/^/  enabled: /'
systemctl is-active postgresql 2>/dev/null | sed 's/^/  active : /'
pg_lsclusters 2>/dev/null | sed 's/^/  /'
PGPASSWORD=$DB_PASSWORD psql -h 127.0.0.1 -U ${DB_USER:-quantmind} -d ${DB_NAME:-quantmind} \
  -tAc "SELECT 'PG '||version();" 2>&1 | head -n 1 | sed 's/^/  /'

echo
echo "=============== 表 / 索引 ==============="
PGPASSWORD=$DB_PASSWORD psql -h 127.0.0.1 -U ${DB_USER:-quantmind} -d ${DB_NAME:-quantmind} \
  -tAc "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';" | sed 's/^/  tables: /'
PGPASSWORD=$DB_PASSWORD psql -h 127.0.0.1 -U ${DB_USER:-quantmind} -d ${DB_NAME:-quantmind} \
  -tAc "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY 1;" | sed 's/^/   - /'
PGPASSWORD=$DB_PASSWORD psql -h 127.0.0.1 -U ${DB_USER:-quantmind} -d ${DB_NAME:-quantmind} \
  -tAc "SELECT count(*) FROM rd_agent_mining_tasks;" | sed 's/^/  mining task rows: /'

echo
echo "=============== 服务 ==============="
curl -s -m 5 http://127.0.0.1:8001/health | python3 -c "
import json,sys
try: d=json.load(sys.stdin)
except Exception: print('  (服务未运行)'); raise SystemExit
print('  status:', d['status'], '| mounted:', len(d['mounted']), '| failed:', len(d['failed']))
print('  startup:', {k:('ok' if v=='ok' or v.startswith('ok') else v) for k,v in d.get('startup',{}).items()})
"
