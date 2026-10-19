#!/bin/bash
# 部署状态总览
set -uo pipefail
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck disable=SC1091
. "$SCRIPT_DIR/load-runtime-env.sh" || exit 1
. "$SCRIPT_DIR/pg-url-env.sh"
if [ -n "${DATABASE_URL:-}" ]; then
  resolve_pg_url_env || exit 1
  apply_pg_url_options
fi

APP_HOST=${PGHOST:-${PG_URL_HOST:-${DB_HOST:-127.0.0.1}}}
APP_PORT=${PGPORT:-${PG_URL_PORT:-${DB_PORT:-5432}}}
APP_USER=${PGUSER_APP:-${PG_URL_USER:-${DB_USER:-quantmind}}}
APP_DB=${PGDB_APP:-${PG_URL_DATABASE:-${DB_NAME:-quantmind}}}
APP_PASS=${PGPASS_APP:-${PG_URL_PASSWORD:-${DB_PASSWORD:-}}}
if [ -z "$APP_PASS" ]; then
  echo "FATAL: 请在受保护的 runtime.env 中设置 DB_PASSWORD 或 DATABASE_URL。" >&2
  exit 1
fi
unset DATABASE_URL DB_PASSWORD
export PGPASSWORD="$APP_PASS"
unset APP_PASS PG_URL_PASSWORD
PG_ARGS=(--host "$APP_HOST" --port "$APP_PORT" --username "$APP_USER" --no-password)
echo "=============== Python / venv ==============="
for V in /home/qusong/autoalpha-venv312 /home/qusong/autoalpha-venv; do
  [ -x "$V/bin/python" ] && echo "  $V -> $("$V/bin/python" -V 2>&1)"
done
echo "  qlib in 312: $(/home/qusong/autoalpha-venv312/bin/python -c 'import qlib;print("yes",qlib.__version__)' 2>/dev/null || echo 'not yet')"

echo
echo "=============== PostgreSQL ==============="
overall_status=0
systemctl is-enabled postgresql 2>/dev/null | sed 's/^/  enabled: /' || echo "  enabled: unavailable"
systemctl is-active postgresql 2>/dev/null | sed 's/^/  active : /' || echo "  active : unavailable"
pg_lsclusters 2>/dev/null | sed 's/^/  /' || echo "  clusters: unavailable"

if pg_version=$(psql "${PG_ARGS[@]}" --dbname "$APP_DB" -tAc "SELECT 'PG '||version();" 2>&1); then
  printf '  %s\n' "$pg_version"
else
  echo "  ERROR: PostgreSQL connection check failed."
  printf '  %s\n' "$pg_version" | sed 's/^/    /'
  overall_status=1
fi

echo
echo "=============== 表 / 索引 ==============="
if tables=$(psql "${PG_ARGS[@]}" --dbname "$APP_DB" -tAc \
  "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';" 2>&1); then
  echo "  tables: $tables"
else
  echo "  ERROR: table count query failed."
  overall_status=1
fi
if table_names=$(psql "${PG_ARGS[@]}" --dbname "$APP_DB" -tAc \
  "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY 1;" 2>&1); then
  printf '%s\n' "$table_names" | sed '/^[[:space:]]*$/d; s/^/   - /'
else
  echo "  ERROR: table listing failed."
  overall_status=1
fi
if mining_rows=$(psql "${PG_ARGS[@]}" --dbname "$APP_DB" -tAc \
  "SELECT count(*) FROM rd_agent_mining_tasks;" 2>&1); then
  echo "  mining task rows: $mining_rows"
else
  echo "  ERROR: mining task count query failed."
  overall_status=1
fi

echo
echo "=============== 服务 ==============="
if health=$(curl -fsS -m 5 http://127.0.0.1:8001/health 2>/dev/null); then
  printf '%s' "$health" | python3 -c "
import json,sys
try: d=json.load(sys.stdin)
except Exception: print('  ERROR: health response is not JSON'); raise SystemExit(1)
print('  status:', d['status'], '| mounted:', len(d['mounted']), '| failed:', len(d['failed']))
print('  startup:', {k:('ok' if v=='ok' or v.startswith('ok') else v) for k,v in d.get('startup',{}).items()})
sys.exit(0 if d.get('status') == 'healthy' and not d.get('failed') else 1)
" || overall_status=1
else
  echo "  ERROR: backend health endpoint unavailable."
  overall_status=1
fi

exit "$overall_status"
