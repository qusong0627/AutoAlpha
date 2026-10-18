#!/bin/bash
# 构建 CN 市场 Qlib 缓存（QuantDB parquet -> qlib bin）
#   E:\quantdb 无 .qlib_cache，/data/qlib/cn_data 为空，必须先建一次，
#   否则 resolve_qlib_provider_uri("CN") 会回退到仓库 db/qlib_data 而报
#   "does not contain data for day"。
set -uo pipefail

PY=${VENV:-/home/qusong/autoalpha-venv312}/bin/python
export PYTHONPATH=/mnt/d/git/autoalpha
export QM_QUANTDB_DATA_DIR=${QM_QUANTDB_DATA_DIR:-/data/quantdb}
export QM_QLIB_DATA_DIR=${QM_QLIB_DATA_DIR:-/data/qlib/cn_data}
export QLIB_PROVIDER_URI=${QLIB_PROVIDER_URI:-/data/qlib/cn_data}

echo "== 前置检查 =="
"$PY" -c "import qlib; print('qlib', qlib.__version__)"
echo "quantdb: $(ls -d "$QM_QUANTDB_DATA_DIR" 2>/dev/null || echo MISSING)"
echo "target : $QLIB_PROVIDER_URI"

echo
echo "== 构建 =="
"$PY" /mnt/d/git/autoalpha/scripts/wsl/build_qlib_cache.py "$@"
