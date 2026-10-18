#!/bin/bash
# 本机开发启动。**脚本内不保留任何密钥默认值**（fail-closed）：
# 缺 INTERNAL_CALL_SECRET / 可用的服务端 LLM Key 时直接拒绝启动。
#   - 优先读环境变量；
#   - 也可写入权限受限的 runtime.env（默认 ~/.config/autoalpha/runtime.env），脚本自动加载。
# 当前镜像没有登录路由或已接入的可信认证网关；run-wsl.sh 不构成可用的生产入口。
#
# 必须使用 Python 3.12：pyqlib 只有 cp38~cp312 的 wheel、没有 sdist。
set -e

VENV=${VENV:-/home/qusong/autoalpha-venv312}
ROOT=/mnt/d/git/autoalpha
PORT=${PORT:-8001}
if ! "$VENV/bin/python" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)'; then
  echo "FATAL: 因子挖掘必须使用 Python 3.12，当前解释器：$("$VENV/bin/python" -V 2>&1)" >&2
  exit 1
fi
# 默认 0.0.0.0 是**刻意保留**的：Windows 侧的 wslrelay 是通过 WSL 的 eth0 IP
# 连进来的，改成 127.0.0.1 会打断 Windows→localhost:8001 的访问链路。
# 真正的局域网暴露面在 vite（跑在 Windows 上，见 web/vite.config.ts）。
HOST=${HOST:-0.0.0.0}

# ── 统一加载环境变量 / 受保护的 runtime.env ──────────────────────────
# 非空进程环境变量优先；缺失时才从 ext4 上权限受限的 env 文件补齐。
# shellcheck disable=SC1091
. "$ROOT/scripts/wsl/load-runtime-env.sh"

# 缺值即拒绝启动（fail-closed）。历史教训：本脚本曾内置一个"开发用"内部密钥
# （现已退役，并已列入 auth.py 的 _PUBLIC_INTERNAL_DEFAULTS 黑名单）与一枚真实
# DeepSeek Key——前者会被后端当作"已配置密钥"接受，任何读过仓库的人都能凭它冒充
# 管理员；后者是可直接盗用的计费凭据。
require_env() {
  local name="$1"
  local value
  value=$(printenv -- "$name" 2>/dev/null || true)
  if [ -z "$value" ]; then
    echo "FATAL: $name 未设置——本部署不再内置密钥默认值（fail-closed）。" >&2
    echo "  任选其一：" >&2
    echo "    1) export $name='<真实值>' 后重跑本脚本" >&2
    echo "    2) 写入 ${QM_RUNTIME_ENV_FILE}（格式 KEY=value，每行一条）" >&2
    echo "    3) 交互式补齐私有配置：bash $ROOT/scripts/wsl/init-runtime-env.sh --complete" >&2
    echo "  模板见：$ROOT/config/runtime.env.example" >&2
    exit 1
  fi
  printf '%s' "$value"
}

# 内部密钥：必须是一个**随机生成**的真实密钥，且**不能**出现在
# backend/shared/auth.py 的 _PUBLIC_INTERNAL_DEFAULTS 里（那里的值一律视为未配置）。
# 注意别把这两件事搞反：被列入该集合 = 失效，不是安全。
if [ -z "${INTERNAL_CALL_SECRET:-}" ] && [ -n "${DEV_SECRET:-}" ]; then
  INTERNAL_CALL_SECRET="$DEV_SECRET"   # 兼容旧变量名
fi
export INTERNAL_CALL_SECRET=$(require_env INTERNAL_CALL_SECRET)
export PYTHONPATH="$ROOT"
export ENABLE_STARTUP_DDL=${ENABLE_STARTUP_DDL:-1}
export ENABLE_DOC_MINING=${ENABLE_DOC_MINING:-false}
# Local experiment bypass: allow RD-Agent to start below the default 8-core/32-GB gate.
# This may make the development machine slow or unresponsive under heavy workloads.
export ALPHA_AGENT_SKIP_HW_LOCK=${ALPHA_AGENT_SKIP_HW_LOCK:-1}
export LOG_TRACE_PATH=${LOG_TRACE_PATH:-/data/alpha_agent_logs}
# 免登录：本地独立部署不接用户体系，直接放行并注入管理员替身身份。
# 仅限本机；对外暴露前务必置 0，并同时关掉前端的 VITE_DISABLE_AUTH。
export AUTH_DISABLED=${AUTH_DISABLED:-1}
export DEV_USER_ID=${DEV_USER_ID:-10000001}
export DEV_TENANT_ID=${DEV_TENANT_ID:-default}

# LLM Key 只从服务端环境读取。当前 LLM client 支持以下任一变量；拒绝空值和
# 仓库中常见的占位符，避免后端起来后所有挖掘任务才因没有凭证失败。
has_llm_key=false
for name in DEEPSEEK_API_KEY AI_IDE_LLM_API_KEY AI_IDE_API_KEY OPENAI_API_KEY; do
  value=$(printenv -- "$name" 2>/dev/null || true)
  lower_value=${value,,}
  case "$lower_value" in
    ""|*your-deepseek-api-key*|*mock-api-key*|sk-在此*) continue ;;
  esac
  has_llm_key=true
  break
done
if [ "$has_llm_key" != true ]; then
  echo "FATAL: 未配置可用的服务端 LLM API Key。请在受保护的 runtime.env 或服务环境中设置 DEEPSEEK_API_KEY、AI_IDE_LLM_API_KEY、AI_IDE_API_KEY 或 OPENAI_API_KEY。" >&2
  echo "  本机私有配置可运行：bash $ROOT/scripts/wsl/init-runtime-env.sh --complete" >&2
  exit 1
fi
export DEEPSEEK_BASE_URL=${DEEPSEEK_BASE_URL:-https://api.deepseek.com}
export DEEPSEEK_MODEL=${DEEPSEEK_MODEL:-deepseek-flash}

# ── P0-4 护栏（可在 env 文件里覆盖）─────────────────────────────────
# qlib joblib worker 上限：qlib 默认按可用 CPU 数起 worker（本机 36 核），
# 一次重计算就会派生二十多个进程；这里钉一个保守上限。
export QLIB_KERNELS=${QLIB_KERNELS:-4}
# promote* 重计算的硬超时（秒）与并发上限
export FACTOR_PROMOTE_TIMEOUT_SECONDS=${FACTOR_PROMOTE_TIMEOUT_SECONDS:-180}
export FACTOR_PROMOTE_MAX_CONCURRENCY=${FACTOR_PROMOTE_MAX_CONCURRENCY:-2}

# PG（本机 WSL 集群 18/main）
export DB_HOST=${DB_HOST:-127.0.0.1}
export DB_PORT=${DB_PORT:-5432}
export DB_NAME=${DB_NAME:-quantmind}
export DB_USER=${DB_USER:-quantmind}
if [ -z "${DATABASE_URL:-}" ]; then
  export DB_PASSWORD=$(require_env DB_PASSWORD)
  DB_PASSWORD_URLENCODED=$("$VENV/bin/python" -c 'import os, urllib.parse; print(urllib.parse.quote_plus(os.environ["DB_PASSWORD"]))')
  export DATABASE_URL="postgresql+asyncpg://${DB_USER}:${DB_PASSWORD_URLENCODED}@${DB_HOST}:${DB_PORT}/${DB_NAME}"
fi

# 数据目录（/data/quantdb 是 E:\quantdb 的软链，见 scripts/wsl/link-quantdb.sh）
export QM_QUANTDB_DATA_DIR=${QM_QUANTDB_DATA_DIR:-/data/quantdb}
export QM_QUANTCUSTOM_DATA_DIR=${QM_QUANTCUSTOM_DATA_DIR:-/data/quantcustom}
export QM_QLIB_DATA_DIR=${QM_QLIB_DATA_DIR:-/data/qlib/cn_data}
# QLIB_PROVIDER_URI 是 qlib_paths 解析里的最高优先级：钉住它，
# ensure_qlib_cache 就会把缓存建在 WSL 本地 ext4（而非 9p 的 E:）。
export QLIB_PROVIDER_URI=${QLIB_PROVIDER_URI:-/data/qlib/cn_data}

echo "venv=$VENV ($("$VENV/bin/python" -V 2>&1))"
echo "port=$PORT host=$HOST  startup_ddl=$ENABLE_STARTUP_DDL  doc_mining=$ENABLE_DOC_MINING"
echo "skip_hw_lock=$ALPHA_AGENT_SKIP_HW_LOCK"
echo "auth_disabled=$AUTH_DISABLED  dev_user=$DEV_USER_ID"
echo "pg=${DB_HOST}:${DB_PORT}/${DB_NAME} user=${DB_USER}"
echo "quantdb=${QM_QUANTDB_DATA_DIR} ($(readlink -f "$QM_QUANTDB_DATA_DIR" 2>/dev/null || echo '缺失'))"
echo "internal secret: configured  LLM API key: configured"
echo "qlib_kernels=$QLIB_KERNELS  promote_timeout=${FACTOR_PROMOTE_TIMEOUT_SECONDS}s  promote_concurrency=$FACTOR_PROMOTE_MAX_CONCURRENCY"

exec "$VENV/bin/python" -m uvicorn \
  backend.services.engine.app_factor_mining:app \
  --host "$HOST" --port "$PORT" --access-log
