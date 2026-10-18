#!/bin/bash
# 摸清 QuantDB / quantcustom 目录约定，以及 qlib 数据在哪
set -uo pipefail
Q=/mnt/e/quantdb

echo "== 6_ml_datasets =="
ls -la "$Q/6_ml_datasets" 2>&1 | head -n 20

echo
echo "== 1_kline_data =="
ls -la "$Q/1_kline_data" 2>&1 | head -n 10

echo
echo "== _meta =="
ls -la "$Q/_meta" 2>&1 | head -n 15

echo
echo "== 找 qlib bin（calendars/instruments 标志）=="
find /mnt/e -maxdepth 4 -type d -name "calendars" 2>/dev/null | head -n 10
echo "  ---"
find /mnt/e -maxdepth 3 -type d \( -name "cn_data" -o -name "qlib_data" -o -name "qlib" \) 2>/dev/null | head -n 10

echo
echo "== 找 quantcustom =="
find /mnt -maxdepth 4 -type d -name "quantcustom" 2>/dev/null | head -n 5

echo
echo "== parquet 抽样（确认可读）=="
f=$(find "$Q/1_kline_data" -name "*.parquet" 2>/dev/null | head -n 1)
echo "  sample: $f"
[ -n "$f" ] && ls -la "$f"
