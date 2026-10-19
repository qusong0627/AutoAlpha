#!/bin/bash
# 追加安装 passlib（api 侧 require_admin 密码校验）
set -euo pipefail

PY=/home/qusong/autoalpha-venv/bin/python
MIRROR=${MIRROR-https://pypi.tuna.tsinghua.edu.cn/simple}

"$PY" -m pip install --upgrade "passlib[bcrypt]" -i "$MIRROR" --retries 10 --timeout 60
"$PY" -c "import passlib; print('passlib', passlib.__version__)"
