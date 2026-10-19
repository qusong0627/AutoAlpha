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
[ ! -L "$SRC" ] || { echo "FATAL: QuantDB 源目录不能是符号链接: $SRC" >&2; exit 1; }
mount_target=$(findmnt -n -o TARGET -T "$SRC" 2>/dev/null || true)
[ "$mount_target" = "/mnt/e" ] || {
  echo "FATAL: $SRC 没有落在预期的 E: 挂载点 /mnt/e（实际挂载点: ${mount_target:-未知}）" >&2
  exit 1
}
echo "  $SRC  ok"

echo
echo "== 建 /data 布局 =="
ensure_real_dir() {
  local path=$1
  if [ -L "$path" ]; then
    echo "FATAL: 写入目录不能是符号链接: $path -> $(readlink "$path")" >&2
    exit 1
  fi
  mkdir -p -- "$path"
  [ -d "$path" ] && [ ! -L "$path" ] || {
    echo "FATAL: 预期真实目录: $path" >&2
    exit 1
  }
}

ensure_real_dir /data

if [ -L "$DST" ]; then
  current_target=$(readlink -e -- "$DST" 2>/dev/null || true)
  expected_target=$(realpath -e -- "$SRC")
  [ "$current_target" = "$expected_target" ] || {
    echo "FATAL: $DST 指向非预期目标 ($(readlink "$DST")); 预期 $SRC。未修改现有链接。" >&2
    exit 1
  }
  echo "  $DST 已正确链接 -> $current_target"
elif [ -e "$DST" ]; then
  echo "FATAL: $DST 已被非链接文件或目录占用；请先检查其内容，未覆盖。" >&2
  exit 1
else
  ln -s -- "$SRC" "$DST"
fi

# 写侧根与日志根放 WSL 本地 ext4：物化器每因子要重写 1600+ 分区，
# 走 9p 会明显拖慢；如需落在 Windows 盘可自行改成软链。
ensure_real_dir /data/quantcustom
ensure_real_dir /data/quantcustom/6_ml_datasets
ensure_real_dir /data/qlib
ensure_real_dir /data/qlib/cn_data
ensure_real_dir /data/alpha_agent_logs

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
