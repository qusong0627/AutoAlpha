#!/bin/bash
# 追加安装 jinja2（data_snapshot 解析 QuantDB 模板需要）
set -euo pipefail
MIRROR=${MIRROR-https://pypi.tuna.tsinghua.edu.cn/simple}
for PY in /home/qusong/autoalpha-venv312/bin/python /home/qusong/autoalpha-venv/bin/python; do
  [ -x "$PY" ] || continue
  echo "== $PY =="
  "$PY" -m pip install -q --upgrade jinja2 -i "$MIRROR" --retries 10 --timeout 60
  "$PY" -c "import jinja2; print('  jinja2', jinja2.__version__)"
done
