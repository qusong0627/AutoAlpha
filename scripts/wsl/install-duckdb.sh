#!/bin/bash
# 装 duckdb（QuantDB parquet 读取 / qlib 缓存构建 / factor-research store 需要）
set -euo pipefail
PY=${VENV:-/home/qusong/autoalpha-venv312}/bin/python
MIRROR=${MIRROR-https://pypi.tuna.tsinghua.edu.cn/simple}
"$PY" -m pip install -q --upgrade duckdb -i "$MIRROR" --retries 10 --timeout 60
"$PY" -c "import duckdb; print('duckdb', duckdb.__version__)"
