#!/bin/bash
# 把 E:\quantdb 挂进 WSL 的 /data 布局（对齐容器口径）
#   /data/quantdb      -> /mnt/e/quantdb   （软链，CN 市场 QuantDB 根）
#   /data/quantcustom                      （写侧根：6_ml_datasets/rd_mined、l1_factors）
#   /data/qlib/cn_data                     （qlib bin，需另行构建）
#   /data/alpha_agent_logs                 （挖掘子进程日志根）
#
# 为什么要软链而不是只设 QM_QUANTDB_DATA_DIR：
#   rd_loop_wrapper / minute_qlib_provider / quantdb_hub / feature_source 等多处
#   硬回退到 /data/quantdb，软链能同时满足「env 显式指定」与「容器默认路径」两条读法。
set -euo pipefail

SRC=/mnt/e/quantdb
DST=/data/quantdb

echo "== 源目录检查 =="
[ -d "$SRC" ] || { echo "  源不存在: $SRC"; exit 1; }
echo "  $SRC  ok"

echo
echo "== 建 /data 布局 =="
mkdir -p /data

if [ -L "$DST" ]; then
  echo "  $DST 已是软链 -> $(readlink "$DST")，重建"
  rm -f "$DST"
elif [ -d "$DST" ]; then
  echo "  $DST 已是实体目录，跳过（不覆盖已有数据）"
fi
[ -e "$DST" ] || ln -s "$SRC" "$DST"

# 写侧根与日志根放 WSL 本地 ext4：物化器每因子要重写 1600+ 分区，
# 走 9p 会明显拖慢；如需落在 Windows 盘可自行改成软链。
mkdir -p /data/quantcustom/6_ml_datasets
mkdir -p /data/qlib/cn_data
mkdir -p /data/alpha_agent_logs

echo
echo "== 结果 =="
ls -la /data
echo "  --- 软链目标 ---"
ls -la "$DST/" 2>&1 | head -n 8

echo
echo "== 可读性抽检 =="
f="$DST/1_kline_data/daily_backward/000001.SZ.parquet"
if [ -f "$f" ]; then
  echo "  sample: $f  ($(stat -c%s "$f") bytes)"
else
  echo "  sample 不存在: $f"
fi
df -h /data "$SRC" 2>&1 | sed 's/^/  /'

echo
echo "== 供启动脚本使用的环境变量 =="
cat <<'EOF'
  export QM_QUANTDB_DATA_DIR=/data/quantdb
  export QM_QUANTCUSTOM_DATA_DIR=/data/quantcustom
  export QM_QLIB_DATA_DIR=/data/qlib/cn_data
  export LOG_TRACE_PATH=/data/alpha_agent_logs
EOF
