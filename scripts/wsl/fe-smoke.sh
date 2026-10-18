#!/bin/bash
# 前端 dev server 冒烟：页面可达 + /api 代理到 8001 + 认证头代填生效
set -uo pipefail
FE=${FE:-http://127.0.0.1:3000}

echo "== 1) 首页 HTML =="
code=$(curl -s -o /tmp/fe.html -w "%{http_code}" --max-time 20 "$FE/")
echo "  http=$code  bytes=$(wc -c < /tmp/fe.html)"
grep -o '<title>[^<]*</title>' /tmp/fe.html || true
grep -o 'src="[^"]*main[^"]*"' /tmp/fe.html || true

echo
echo "== 2) 入口模块能否被 vite 编译（/src/main.tsx）=="
code=$(curl -s -o /tmp/main.js -w "%{http_code}" --max-time 60 "$FE/src/main.tsx")
echo "  http=$code  bytes=$(wc -c < /tmp/main.js)"
head -c 200 /tmp/main.js; echo

echo
echo "== 3) /api 代理（应由 proxy 代填身份头 -> 200，而不是 401）=="
for p in /api/v1/alpha-agent/markets /api/v1/alpha-agent/data-summary /api/v1/factor-report/datasets; do
  code=$(curl -s -o /tmp/api.json -w "%{http_code}" --max-time 60 "$FE$p")
  printf "  %s  %-42s %s\n" "$code" "$p" "$(head -c 110 /tmp/api.json | tr -d '\n')"
done

echo
echo "== 4) 对照：直连后端不带任何头（应 401，证明是代理在起作用）=="
code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 20 "http://127.0.0.1:8001/api/v1/alpha-agent/markets")
echo "  $code  http://127.0.0.1:8001/api/v1/alpha-agent/markets"
