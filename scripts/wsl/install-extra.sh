#!/bin/bash
# 追加安装（greenlet = sqlalchemy asyncio 必需；pyjwt = backend/shared/auth.py 顶层依赖）
set -euo pipefail

VENV=/home/qusong/autoalpha-venv
PY="$VENV/bin/python"
MIRROR=${MIRROR-https://pypi.tuna.tsinghua.edu.cn/simple}

"$PY" -V || { echo "venv missing"; exit 1; }

"$PY" -m pip install --upgrade greenlet pyjwt -i "$MIRROR" --retries 10 --timeout 60
echo "--- verify ---"
"$PY" - <<'EOF'
import importlib
for m in ("greenlet", "jwt", "sqlalchemy", "fastapi"):
    mod = importlib.import_module(m)
    print(f"  {m:12s} {getattr(mod, '__version__', 'ok')}")
EOF
