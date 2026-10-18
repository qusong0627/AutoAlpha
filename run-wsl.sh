#!/bin/bash
# WSL 启动脚本：因子挖掘独立后端
#   用法: wsl bash /mnt/d/git/autoalpha/run-wsl.sh
# 环境变量：
#   DB_PASSWORD                                    PG 密码（DATABASE_URL 未设置时必填）
#   DB_HOST/DB_PORT/DB_NAME/DB_USER                PG 连接参数（非密钥，有本机默认值）
#   DATABASE_URL                                   显式连接串（优先级高于分项，注意 +asyncpg 前缀）
#   QM_QUANTDB_DATA_DIR        QuantDB parquet 根
#   QM_QUANTCUSTOM_DATA_DIR    quantcustom（6_ml_datasets/rd_mined）
#   LOG_TRACE_PATH             挖掘日志根（默认 /data/alpha_agent_logs）
#   ENABLE_DOC_MINING          文档挖掘链开关（默认 false）
#   ENABLE_STARTUP_DDL=1       启动时建表/对账（首次部署打开即可；之后可关）
#   INTERNAL_CALL_SECRET       内部调用密钥（与 api 网关一致才认身份头）
set -e

# 必须使用 Python 3.12：pyqlib 只有 cp38~cp312 wheel 且无 sdist。
VENV=${VENV:-/home/qusong/autoalpha-venv312}
ROOT=/mnt/d/git/autoalpha
PORT=${PORT:-8001}
if ! "$VENV/bin/python" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)'; then
  echo "FATAL: 因子挖掘必须使用 Python 3.12，当前解释器：$("$VENV/bin/python" -V 2>&1)" >&2
  exit 1
fi

# shellcheck disable=SC1091
. "$ROOT/scripts/wsl/load-runtime-env.sh"

export PYTHONPATH="$ROOT"

# ── PG（本机 WSL 集群 18/main）──
export DB_HOST=${DB_HOST:-127.0.0.1}
export DB_PORT=${DB_PORT:-5432}
export DB_NAME=${DB_NAME:-quantmind}
export DB_USER=${DB_USER:-quantmind}
# 同步池（psycopg2）走分项；异步池（asyncpg）由 database_manager_v2 自动改写前缀
if [ -z "${DATABASE_URL:-}" ]; then
  if [ -z "${DB_PASSWORD:-}" ]; then
    echo "FATAL: DB_PASSWORD 或 DATABASE_URL 必须由运行环境提供；脚本不再内置数据库口令。" >&2
    exit 1
  fi
  DB_PASSWORD_URLENCODED=$("$VENV/bin/python" -c 'import os, urllib.parse; print(urllib.parse.quote_plus(os.environ["DB_PASSWORD"]))')
  export DATABASE_URL="postgresql+asyncpg://${DB_USER}:${DB_PASSWORD_URLENCODED}@${DB_HOST}:${DB_PORT}/${DB_NAME}"
fi

# 数据目录（/data/quantdb 是 E:\quantdb 的软链，见 scripts/wsl/link-quantdb.sh）
export QM_QUANTDB_DATA_DIR=${QM_QUANTDB_DATA_DIR:-/data/quantdb}
export QM_QUANTCUSTOM_DATA_DIR=${QM_QUANTCUSTOM_DATA_DIR:-/data/quantcustom}
export QM_QLIB_DATA_DIR=${QM_QLIB_DATA_DIR:-/data/qlib/cn_data}
# qlib_paths 解析里优先级最高，钉住它确保缓存读写落在同一处
export QLIB_PROVIDER_URI=${QLIB_PROVIDER_URI:-/data/qlib/cn_data}

echo "root=$ROOT  port=$PORT"
echo "pg=${DB_HOST}:${DB_PORT}/${DB_NAME} user=${DB_USER}"
echo "quantdb=${QM_QUANTDB_DATA_DIR} ($(readlink -f "$QM_QUANTDB_DATA_DIR" 2>/dev/null || echo '缺失'))"
echo "startup_ddl=${ENABLE_STARTUP_DDL:-0}  doc_mining=${ENABLE_DOC_MINING:-false}"

exec "$VENV/bin/python" -m uvicorn \
  backend.services.engine.app_factor_mining:app \
  --host 0.0.0.0 --port "$PORT" --access-log
