#!/bin/bash
# 对运行中的独立后端做 HTTP 冒烟（从 WSL 内打，避开 Windows 端口转发差异）
BASE=${BASE:-http://127.0.0.1:8001}

echo "== /health =="
curl -s -o /tmp/h.json -w "http=%{http_code}\n" "$BASE/health"
cat /tmp/h.json | python3 -m json.tool 2>/dev/null || cat /tmp/h.json

echo
echo "== 未认证访问受保护路由（应 401）=="
for p in /api/v1/alpha-agent/markets /api/v1/factor-research/catalog /api/v1/admin/training-data/catalog; do
  code=$(curl -s -o /dev/null -w "%{http_code}" "$BASE$p")
  echo "  $code  $p"
done

echo
echo "== 带内部密钥+用户身份（应 200/4xx 而非 401）=="
SECRET=${INTERNAL_CALL_SECRET:?INTERNAL_CALL_SECRET 未设置——脚本不再内置默认值，请先 export 或写入 config/runtime.env}
for p in /api/v1/alpha-agent/markets /api/v1/factor-report/datasets; do
  code=$(curl -s -o /tmp/r.json -w "%{http_code}" \
    -H "X-Internal-Call: $SECRET" -H "X-User-Id: 10000001" -H "X-Tenant-Id: default" \
    "$BASE$p")
  echo "  $code  $p  ->  $(head -c 120 /tmp/r.json)"
done
