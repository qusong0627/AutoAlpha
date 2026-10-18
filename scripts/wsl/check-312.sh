#!/bin/bash
# 检查 3.12 venv 安装进度
set -uo pipefail
PY=/home/qusong/autoalpha-venv312/bin/python
echo "python: $("$PY" -V 2>&1)"
echo "packages: $("$PY" -m pip list 2>/dev/null | wc -l)"
for m in qlib pandas numpy fastapi sqlalchemy asyncpg psycopg2 yaml jwt passlib; do
  "$PY" -c "import $m; print('  ok  $m', getattr($m,'__version__',''))" 2>/dev/null || echo "  MISSING  $m"
done
