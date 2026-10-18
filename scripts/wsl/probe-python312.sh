#!/bin/bash
# 探测可用的 Python 3.12 获取途径
set -uo pipefail
echo "== apt 候选 =="
apt-cache policy python3.12 python3.13 2>&1 | head -n 20

echo
echo "== 系统已有解释器 =="
ls -1 /usr/bin/python3.* 2>/dev/null

echo
echo "== uv 是否可用 =="
command -v uv || echo "(uv 未安装)"

echo
echo "== pipx / pyenv =="
command -v pipx || echo "(无 pipx)"
command -v pyenv || echo "(无 pyenv)"
