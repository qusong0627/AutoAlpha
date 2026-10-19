#!/bin/bash
# Restore a trusted AutoAlpha dump into an isolated scratch database for rehearsal.
set -euo pipefail
umask 077

if [ "$#" -ne 1 ]; then
  echo "Usage: AUTOALPHA_RESTORE_DB=autoalpha_restore_<date> $0 /path/to/backup.dump" >&2
  exit 2
fi
DUMP_PATH=$1
if [ -L "$DUMP_PATH" ] || [ ! -f "$DUMP_PATH" ]; then
  echo "FATAL: 备份必须是普通文件，不能是符号链接。" >&2
  exit 1
fi
DUMP_PATH=$(realpath -e -- "$DUMP_PATH")
CHECKSUM_PATH="$DUMP_PATH.sha256"
if [ -L "$CHECKSUM_PATH" ] || [ ! -f "$CHECKSUM_PATH" ]; then
  echo "FATAL: 缺少备份校验文件：$CHECKSUM_PATH" >&2
  exit 1
fi
(cd -- "$(dirname -- "$DUMP_PATH")" && sha256sum --check --status "$(basename -- "$CHECKSUM_PATH")") || {
  echo "FATAL: PostgreSQL 备份 SHA256 校验失败。" >&2
  exit 1
}

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
APP_PASS=${PGPASS_APP:-${PG_URL_PASSWORD:-${DB_PASSWORD:-}}}
RESTORE_DB=${AUTOALPHA_RESTORE_DB:-}
if [ -z "$APP_PASS" ]; then
  echo "FATAL: 请在受保护的 runtime.env 中设置 DB_PASSWORD 或 DATABASE_URL。" >&2
  exit 1
fi
if [[ ! "$RESTORE_DB" =~ ^autoalpha_restore_[a-zA-Z0-9_]+$ ]]; then
  echo "FATAL: AUTOALPHA_RESTORE_DB 必须使用 autoalpha_restore_ 前缀。" >&2
  exit 1
fi

unset DATABASE_URL DB_PASSWORD
export PGPASSWORD="$APP_PASS"
unset APP_PASS PG_URL_PASSWORD
PG_ARGS=(--host "$APP_HOST" --port "$APP_PORT" --username "$APP_USER" --no-password)
EXISTS=$(psql "${PG_ARGS[@]}" --dbname postgres --no-psqlrc --tuples-only --no-align \
  --command "SELECT 1 FROM pg_database WHERE datname = '$RESTORE_DB'")
if [ "$EXISTS" = "1" ]; then
  echo "FATAL: 目标数据库已存在，不覆盖：$RESTORE_DB" >&2
  exit 1
fi

pg_restore --list "$DUMP_PATH" >/dev/null
createdb "${PG_ARGS[@]}" --template=template0 "$RESTORE_DB"
echo "Restoring trusted dump into scratch database: $RESTORE_DB"
pg_restore "${PG_ARGS[@]}" --dbname "$RESTORE_DB" --exit-on-error --no-owner --no-acl "$DUMP_PATH"
TABLE_COUNT=$(psql "${PG_ARGS[@]}" --dbname "$RESTORE_DB" --no-psqlrc --tuples-only --no-align \
  --command "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE'")
if [[ ! "$TABLE_COUNT" =~ ^[0-9]+$ ]] || [ "$TABLE_COUNT" -lt 1 ]; then
  echo "FATAL: 恢复后未发现 public 基表；请检查数据库 $RESTORE_DB。" >&2
  exit 1
fi
echo "Restore rehearsal succeeded: database=$RESTORE_DB public_tables=$TABLE_COUNT"
echo "Database retained for manual inspection; remove it only after review."
