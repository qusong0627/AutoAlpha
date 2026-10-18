#!/bin/bash
# WSL PostgreSQL 引导：启动集群 + 建角色/库（幂等）
#   用法: wsl -u root bash /mnt/d/git/autoalpha/scripts/wsl/pg-setup.sh
# 参数（环境变量）：PGUSER_APP / PGPASS_APP / PGDB_APP；DB 密码必须显式提供
set -euo pipefail

APP_USER=${PGUSER_APP:-quantmind}
APP_PASS=${PGPASS_APP:-${DB_PASSWORD:-}}
APP_DB=${PGDB_APP:-quantmind}
unset PGPASS_APP DB_PASSWORD
if [ -z "$APP_PASS" ]; then
  echo "FATAL: 请通过 PGPASS_APP 或 DB_PASSWORD 环境变量提供数据库密码。" >&2
  exit 1
fi
if [[ ! "$APP_USER" =~ ^[a-zA-Z_][a-zA-Z0-9_]*$ ]] || [[ ! "$APP_DB" =~ ^[a-zA-Z_][a-zA-Z0-9_]*$ ]]; then
  echo "FATAL: PGUSER_APP / PGDB_APP 只能包含字母、数字和下划线，并以字母或下划线开头。" >&2
  exit 1
fi
if [[ "$APP_PASS" == *$'\n'* || "$APP_PASS" == *$'\r'* ]]; then
  echo "FATAL: 数据库密码不能包含换行符。" >&2
  exit 1
fi
APP_PASS_SQL=${APP_PASS//\'/\'\'}
APP_PASS_SQL="'${APP_PASS_SQL}'"

echo "== systemd? =="
if systemctl is-system-running >/dev/null 2>&1; then
  echo "systemd active"
  systemctl enable postgresql >/dev/null 2>&1
  systemctl start postgresql
else
  echo "systemd 不可用，回退 pg_ctlcluster"
  pg_ctlcluster 18 main start 2>/dev/null || true
fi

sleep 2
echo "== clusters =="
pg_lsclusters

echo "== 建角色/库（幂等）=="
if ! sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='${APP_USER}'" | grep -q 1; then
  sudo -u postgres psql -v ON_ERROR_STOP=1 <<SQL
CREATE ROLE "${APP_USER}" LOGIN PASSWORD ${APP_PASS_SQL} CREATEDB;
SQL
fi

sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='${APP_DB}'" \
  | grep -q 1 || sudo -u postgres createdb -O "${APP_USER}" "${APP_DB}"

echo "== 连通性自检（TCP + scram）=="
PGPASSWORD="${APP_PASS}" psql -h 127.0.0.1 -p 5432 -U "${APP_USER}" -d "${APP_DB}" \
  -c "SELECT version();" -c "SHOW server_version;" -c "SELECT current_user, current_database();"
