#!/bin/bash
# Secure, single-database PostgreSQL backup. Destination must be on a different
# filesystem from PGDATA; set AUTOALPHA_BACKUP_DIR to an independent disk/off-host mount.
set -euo pipefail
umask 077

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck disable=SC1091
. "$SCRIPT_DIR/load-runtime-env.sh" || exit 1

BACKUP_DIR=${AUTOALPHA_BACKUP_DIR:-}
if [ -z "$BACKUP_DIR" ] || [[ "$BACKUP_DIR" != /* ]] || [ "$BACKUP_DIR" = "/" ]; then
  echo "FATAL: AUTOALPHA_BACKUP_DIR 必须是独立备份盘上的绝对目录。" >&2
  exit 1
fi
if [ -L "$BACKUP_DIR" ]; then
  echo "FATAL: 备份目录不能是符号链接：$BACKUP_DIR" >&2
  exit 1
fi
REPO_ROOT=$(realpath -e -- "$SCRIPT_DIR/../..")
BACKUP_DIR=$(realpath -m -- "$BACKUP_DIR")
case "$BACKUP_DIR/" in
  "$REPO_ROOT/"*)
    echo "FATAL: 备份目录不能位于代码仓库内。" >&2
    exit 1
    ;;
esac
mkdir -p -m 700 -- "$BACKUP_DIR"
if [ -L "$BACKUP_DIR" ] || [ ! -d "$BACKUP_DIR" ]; then
  echo "FATAL: 备份目标必须是普通目录：$BACKUP_DIR" >&2
  exit 1
fi
chmod 700 -- "$BACKUP_DIR"
BACKUP_DIR=$(realpath -e -- "$BACKUP_DIR")
BACKUP_OWNER=$(stat -c '%u' -- "$BACKUP_DIR")
BACKUP_MODE=$(stat -c '%a' -- "$BACKUP_DIR")
if [ "$BACKUP_OWNER" != "$(id -u)" ] || [[ ! "$BACKUP_MODE" =~ ^[0-7]{3,4}$ ]] \
  || (( (8#$BACKUP_MODE & 077) != 0 )); then
  echo "FATAL: 备份目录必须由当前服务用户拥有且不可被组/其他用户访问。" >&2
  exit 1
fi

APP_HOST=${PGHOST:-${DB_HOST:-127.0.0.1}}
APP_PORT=${PGPORT:-${DB_PORT:-5432}}
APP_USER=${PGUSER_APP:-${DB_USER:-quantmind}}
APP_DB=${PGDB_APP:-${DB_NAME:-quantmind}}
APP_PASS=${PGPASS_APP:-${DB_PASSWORD:-}}
RETENTION_DAYS=${AUTOALPHA_BACKUP_RETENTION_DAYS:-14}
if [[ ! "$APP_USER" =~ ^[a-zA-Z_][a-zA-Z0-9_]*$ ]] || [[ ! "$APP_DB" =~ ^[a-zA-Z_][a-zA-Z0-9_]*$ ]]; then
  echo "FATAL: PostgreSQL 用户名和数据库名只能包含字母、数字、下划线。" >&2
  exit 1
fi
if [ -z "$APP_PASS" ] && [ -n "${DATABASE_URL:-}" ]; then
  APP_PASS=$(python3 -c 'import os, urllib.parse; print(urllib.parse.unquote(urllib.parse.urlsplit(os.environ["DATABASE_URL"]).password or ""))')
fi
if [ -z "$APP_PASS" ]; then
  echo "FATAL: 请在受保护的 runtime.env 中设置 DB_PASSWORD 或 DATABASE_URL。" >&2
  exit 1
fi
if [[ ! "$RETENTION_DAYS" =~ ^[1-9][0-9]*$ ]]; then
  echo "FATAL: AUTOALPHA_BACKUP_RETENTION_DAYS 必须是正整数。" >&2
  exit 1
fi

export PGPASSWORD="$APP_PASS"
unset APP_PASS
PG_ARGS=(--host "$APP_HOST" --port "$APP_PORT" --username "$APP_USER" --no-password)
PGDATA=$(psql "${PG_ARGS[@]}" --dbname "$APP_DB" --no-psqlrc --tuples-only --no-align \
  --command 'SHOW data_directory')
PGDATA=$(realpath -e -- "$PGDATA")
SOURCE_MOUNT=$(findmnt --noheadings --output SOURCE --target "$PGDATA")
BACKUP_MOUNT=$(findmnt --noheadings --output SOURCE --target "$BACKUP_DIR")
if [ -z "$SOURCE_MOUNT" ] || [ -z "$BACKUP_MOUNT" ] || [ "$SOURCE_MOUNT" = "$BACKUP_MOUNT" ]; then
  echo "FATAL: 无法证明备份目录与 PGDATA 位于不同文件系统。" >&2
  echo "  PGDATA mount: ${SOURCE_MOUNT:-unknown}" >&2
  echo "  backup mount: ${BACKUP_MOUNT:-unknown}" >&2
  exit 1
fi

exec 9>"$BACKUP_DIR/.pg-backup.lock"
if ! flock -n 9; then
  echo "已有 PostgreSQL 备份正在运行，跳过本次。" >&2
  exit 0
fi

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
BASE="autoalpha-${APP_DB}-${STAMP}-$$"
FINAL_DUMP="$BACKUP_DIR/$BASE.dump"
FINAL_SHA="$FINAL_DUMP.sha256"
TMP_DUMP="$BACKUP_DIR/.$BASE.dump.tmp.$$"
TMP_SHA="$BACKUP_DIR/.$BASE.sha256.tmp.$$"
SUCCESS=0
cleanup() {
  rm -f -- "$TMP_DUMP" "$TMP_SHA"
  if [ "$SUCCESS" -ne 1 ]; then
    rm -f -- "$FINAL_DUMP" "$FINAL_SHA"
  fi
}
trap cleanup EXIT

pg_dump "${PG_ARGS[@]}" --dbname "$APP_DB" --format=custom --compress=6 \
  --no-owner --no-acl --file "$TMP_DUMP"
test -s "$TMP_DUMP"
mv -- "$TMP_DUMP" "$FINAL_DUMP"
(cd -- "$BACKUP_DIR" && sha256sum "$(basename -- "$FINAL_DUMP")" > "$TMP_SHA")
mv -- "$TMP_SHA" "$FINAL_SHA"
sync -f "$FINAL_DUMP"
sync -f "$FINAL_SHA"
SUCCESS=1

find "$BACKUP_DIR" -mindepth 1 -maxdepth 1 -type f \
  \( -name "autoalpha-${APP_DB}-*.dump" -o -name "autoalpha-${APP_DB}-*.dump.sha256" \) \
  -mtime "+$RETENTION_DAYS" -delete

echo "PostgreSQL backup created: $FINAL_DUMP"
echo "SHA256: $FINAL_SHA"
