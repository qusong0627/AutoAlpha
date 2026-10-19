#!/bin/bash
# 诊断 pyqlib 可用性（镜像/官方源、cp314 wheel 是否存在）
set -uo pipefail

PY=/home/qusong/autoalpha-venv/bin/python

echo "== python tag =="
"$PY" -c "import sysconfig,sys; print(sys.version.split()[0], sysconfig.get_platform(), 'cp%d%d' % sys.version_info[:2])"

for IDX in "https://pypi.org/simple" "https://pypi.tuna.tsinghua.edu.cn/simple"; do
  echo
  echo "===== index: $IDX ====="
  "$PY" -m pip index versions pyqlib -i "$IDX" --timeout 40 2>&1 | head -n 6
  echo "--- 匹配本解释器的 wheel ---"
  "$PY" -m pip download pyqlib --no-deps -d /tmp/pyqlib_probe -i "$IDX" \
    --timeout 60 --only-binary=:all: 2>&1 | tail -n 4
done
