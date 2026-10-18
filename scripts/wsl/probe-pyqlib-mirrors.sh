#!/bin/bash
# 多镜像探测 pyqlib 0.9.7 cp312 wheel 可用性
set -uo pipefail
PY=/home/qusong/autoalpha-venv312/bin/python

echo "== 现有 pip 进程 =="
ps aux | grep "[p]ip install" | head -n 5 || echo "  (无)"

echo
echo "== pip 缓存里的 pyqlib 残留 =="
find /root/.cache/pip /home/qusong/.cache/pip -iname "*pyqlib*" 2>/dev/null | head -n 10 || true

echo
echo "== 镜像探测（HTTP 状态 + 是否出现 pyqlib）=="
for M in \
  "https://pypi.tuna.tsinghua.edu.cn/simple/pyqlib/" \
  "https://mirrors.aliyun.com/pypi/simple/pyqlib/" \
  "https://mirror.nju.edu.cn/pypi/web/simple/pyqlib/" \
  "https://mirrors.bfsu.edu.cn/pypi/web/simple/pyqlib/" \
  "https://pypi.org/simple/pyqlib/" ; do
  code=$(curl -s -o /tmp/m.html -w "%{http_code}" --max-time 25 "$M")
  cnt=$(grep -o "pyqlib-0\.9\.7-cp312[^\"]*" /tmp/m.html 2>/dev/null | head -n 1)
  printf "  %-52s http=%s  cp312=%s\n" "$M" "$code" "${cnt:-无}"
done
