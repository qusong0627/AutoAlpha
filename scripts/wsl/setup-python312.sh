#!/bin/bash
# 用 uv 获取独立 CPython 3.12（pyqlib 只有 cp38~cp312 的 wheel，无 sdist）
# 国内网络走 npmmirror 的 python-build-standalone 镜像
set -euo pipefail

VENV312=/home/qusong/autoalpha-venv312
if [ -e "$VENV312" ]; then
  if [ "${1:-}" != "--replace" ]; then
    echo "FATAL: $VENV312 已存在；保留现有环境。确认要重建时用 --replace。" >&2
    exit 1
  fi
  if [ ! -t 0 ]; then
    echo "FATAL: --replace 需要交互式确认，避免无人值守删除 Python 环境。" >&2
    exit 1
  fi
  read -r -p "输入目标路径以确认删除 $VENV312: " CONFIRM_PATH
  if [ "$CONFIRM_PATH" != "$VENV312" ]; then
    echo "取消：确认路径不匹配。" >&2
    exit 1
  fi
  rm -rf -- "$VENV312"
fi

MIRROR=${MIRROR-https://pypi.tuna.tsinghua.edu.cn/simple}
# uv 下载 CPython 的镜像（默认 GitHub，国内常超时）
export UV_PYTHON_INSTALL_MIRROR=${UV_PYTHON_INSTALL_MIRROR:-https://registry.npmmirror.com/-/binary/python-build-standalone}

echo "== 安装 uv =="
/home/qusong/autoalpha-venv/bin/python -m pip install -q --upgrade uv -i "$MIRROR"
UV=$(command -v uv || echo /home/qusong/autoalpha-venv/bin/uv)
"$UV" --version

echo
echo "== 安装 CPython 3.12 =="
"$UV" python install 3.12 2>&1 | tail -n 5
"$UV" python list 2>&1 | head -n 10

echo
echo "== 建 3.12 venv =="
"$UV" venv --python 3.12 --seed "$VENV312" 2>&1 | tail -n 5
"$VENV312/bin/python" -V
