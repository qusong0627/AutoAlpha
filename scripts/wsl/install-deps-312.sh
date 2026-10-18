#!/bin/bash
# 使用带哈希的锁文件安装 Python 3.12 / Linux x86_64 依赖。
set -euo pipefail

PY=/home/qusong/autoalpha-venv312/bin/python
LOCK=/mnt/d/git/autoalpha/requirements-factor-mining.lock
MIRROR=${MIRROR:-https://pypi.tuna.tsinghua.edu.cn/simple}

if ! "$PY" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)'; then
  echo "FATAL: 依赖锁定目标是 Python 3.12，当前解释器：$("$PY" -V 2>&1)" >&2
  exit 1
fi
[ -f "$LOCK" ] || { echo "FATAL: 锁文件不存在：$LOCK" >&2; exit 1; }

echo "== 使用 lock + SHA256 hashes 安装依赖 =="
"$PY" -m pip install --require-hashes -r "$LOCK" \
  -i "$MIRROR" --extra-index-url https://pypi.org/simple \
  --retries 10 --timeout 120

echo
echo "== 验证 =="
"$PY" - <<'EOF'
import importlib
for m in ("qlib", "pandas", "numpy", "fastapi", "sqlalchemy", "asyncpg", "psycopg2", "yaml", "jwt", "passlib"):
    try:
        mod = importlib.import_module(m)
        print(f"  {m:12s} {getattr(mod, '__version__', 'ok')}")
    except Exception as e:
        print(f"  {m:12s} FAILED {type(e).__name__}: {e}")
EOF
