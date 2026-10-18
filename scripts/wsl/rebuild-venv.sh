#!/bin/bash
# 重建 WSL venv（--copies 模式，避免 bin/ 符号链接间歇性 ENOENT）
set -e

VENV=/home/qusong/autoalpha-venv
if [ -e "$VENV" ]; then
  if [ ! -t 0 ]; then
    echo "FATAL: $VENV 已存在；需在交互终端确认后才可重建。" >&2
    exit 1
  fi
  read -r -p "输入 DELETE $VENV 以确认删除该目录: " CONFIRM_DELETE
  if [ "$CONFIRM_DELETE" != "DELETE $VENV" ]; then
    echo "取消：未删除 Python 环境。" >&2
    exit 1
  fi
  [ "$VENV" = "/home/qusong/autoalpha-venv" ] || {
    echo "FATAL: 拒绝删除未预期的路径：$VENV" >&2
    exit 1
  }
  rm -rf -- "$VENV"
fi
python3 -m venv --copies "$VENV"
echo "CREATED: $VENV"
"$VENV/bin/python" -V

# 稳定性自检：连读 5 次解释器与 pip 入口
for i in 1 2 3 4 5; do
  "$VENV/bin/python" -c "import sys; print('interp ok', sys.version.split()[0])" || echo "interp FAIL #$i"
done
"$VENV/bin/python" -m pip --version
