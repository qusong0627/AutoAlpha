#!/bin/bash
# 追加安装 psycopg2-binary（backend/shared/database_pool 同步池驱动）
set -euo pipefail

PY=/home/qusong/autoalpha-venv/bin/python
MIRROR=${MIRROR-https://pypi.tuna.tsinghua.edu.cn/simple}

"$PY" -m pip install --upgrade psycopg2-binary -i "$MIRROR" --retries 10 --timeout 60
"$PY" -c "import psycopg2; print('psycopg2', psycopg2.__version__)"
