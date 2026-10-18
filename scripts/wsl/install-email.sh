#!/bin/bash
# 追加安装 email-validator（pydantic[email]，api 侧 schema 需要）
set -euo pipefail

PY=/home/qusong/autoalpha-venv/bin/python
MIRROR=${MIRROR-https://pypi.tuna.tsinghua.edu.cn/simple}

"$PY" -m pip install --upgrade "pydantic[email]" email-validator -i "$MIRROR" --retries 10 --timeout 60
"$PY" -c "import email_validator; print('email-validator', email_validator.__version__)"
