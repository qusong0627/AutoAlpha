#!/bin/bash
# 装 pyqlib（3.12 环境 + 清华镜像；镜像上确有 cp312 manylinux wheel）
set -euo pipefail
PY=/home/qusong/autoalpha-venv312/bin/python
MIRROR=${MIRROR-https://pypi.tuna.tsinghua.edu.cn/simple}

"$PY" -V || exit 1

echo "== 可用版本（应为多版本，而非 none）=="
"$PY" -m pip index versions pyqlib -i "$MIRROR" 2>&1 | head -n 4

echo
echo "== 安装 =="
"$PY" -m pip install --upgrade "pyqlib==0.9.7" -i "$MIRROR" --retries 10 --timeout 120 2>&1 | tail -n 12

echo
echo "== 验证 =="
"$PY" -c "import qlib; print('qlib', getattr(qlib,'__version__','?'))" 2>&1 | tail -n 3
