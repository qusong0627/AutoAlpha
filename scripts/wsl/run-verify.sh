#!/bin/bash
# 运行挂载自检（避免 inline 命令的路径解析抖动）
# VENV 可覆盖解释器（默认 3.12 环境）
VENV=${VENV:-/home/qusong/autoalpha-venv312}
ENABLE_STARTUP_DDL=${ENABLE_STARTUP_DDL:-0} \
exec "$VENV/bin/python" /mnt/d/git/autoalpha/scripts/wsl/verify_mounts.py "$@"
