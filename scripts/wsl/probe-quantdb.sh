#!/bin/bash
# 检查 WSL 侧 E: 挂载与 /data 布局
set -uo pipefail

echo "== /mnt 列表 =="
ls -la /mnt/ 2>&1 | head -n 15

echo
echo "== /mnt/e 是否可访问 =="
if [ -d /mnt/e ]; then
  ls /mnt/e 2>&1 | head -n 10
else
  echo "  /mnt/e 不存在"
fi

echo
echo "== E:/quantdb 结构（前 2 层）=="
ls -la /mnt/e/quantdb 2>&1 | head -n 20

echo
echo "== 找 qlib 数据目录 =="
for d in /mnt/e/quantdb/qlib /mnt/e/quantdb/qlib_data /mnt/e/quantdb/cn_data /mnt/e/qlib /mnt/e/qlib_data; do
  [ -e "$d" ] && echo "  命中: $d" || true
done
find /mnt/e/quantdb -maxdepth 2 -type d -name "calendars" 2>/dev/null | head -n 5

echo
echo "== /data 现状 =="
ls -la /data 2>&1 | head -n 10

echo
echo "== 挂载参数（/mnt/e）=="
findmnt -no TARGET,SOURCE,FSTYPE,OPTIONS /mnt/e 2>&1 || mount | grep /mnt/e | head -n 3

echo
echo "== 读写探针 =="
if touch /mnt/e/quantdb/.wsl_write_probe 2>/dev/null; then
  echo "  可写: /mnt/e/quantdb"
  rm -f /mnt/e/quantdb/.wsl_write_probe
else
  echo "  不可写: /mnt/e/quantdb"
fi
