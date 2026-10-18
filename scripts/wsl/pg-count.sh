#!/bin/bash
# 一行式 PG 概览（供 verify-all.ps1 调用，避免在 PowerShell 里嵌套引号）
set -euo pipefail
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck disable=SC1091
. "$SCRIPT_DIR/load-runtime-env.sh" || exit 1
: "${DB_PASSWORD:?DB_PASSWORD 或 DATABASE_URL 必须由受保护的运行环境提供}"
export PGPASSWORD="$DB_PASSWORD"
psql -h ${DB_HOST:-127.0.0.1} -U ${DB_USER:-quantmind} -d ${DB_NAME:-quantmind} -tAc \
  "SELECT (SELECT count(*) FROM information_schema.tables WHERE table_schema='public')
          || ' tables / '
          || (SELECT count(*) FROM rd_agent_mining_tasks) || ' mining tasks / '
          || (SELECT count(*) FROM rd_agent_factors) || ' factors';"
