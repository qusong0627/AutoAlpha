#!/bin/bash
# QuantDB 接入验证：确认后端真的读到了 /data/quantdb
set -uo pipefail

BASE=${BASE:-http://127.0.0.1:8001}
SECRET=${INTERNAL_CALL_SECRET:?INTERNAL_CALL_SECRET 未设置——脚本不再内置默认值，请先 export 或写入 config/runtime.env}
H=(-H "X-Internal-Call: $SECRET" -H "X-User-Id: 10000001" -H "X-Tenant-Id: default")

echo "== 服务在跑吗 =="
curl -s -m 5 "$BASE/health" | python3 -c "import json,sys; d=json.load(sys.stdin); print('  status:',d['status'])" 2>/dev/null || echo "  (未运行)"

echo
echo "== 数据摘要（QuantDB 目录是否解析成功）=="
curl -s "${H[@]}" "$BASE/api/v1/alpha-agent/data-summary" \
  | python3 -m json.tool 2>/dev/null | head -n 40

echo
echo "== 可选数据集 =="
curl -s "${H[@]}" "$BASE/api/v1/alpha-agent/mining/config-options" \
  | python3 -c "
import json,sys
d=json.load(sys.stdin)
data=d.get('data',d)
ds=data.get('datasets') or data.get('dataset_catalog') or []
print('  datasets:', ds if isinstance(ds,list) else list(ds)[:10])
print('  frequencies:', data.get('frequencies'))
" 2>/dev/null || echo "  (解析失败)"

echo
echo "== universes =="
curl -s "${H[@]}" "$BASE/api/v1/alpha-agent/universes" | head -c 300; echo
