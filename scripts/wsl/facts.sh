#!/bin/bash
# 一次性核对 AGENTS.md 要写的事实
echo "== venv =="
for V in /home/qusong/autoalpha-venv312 /home/qusong/autoalpha-venv; do
  [ -x "$V/bin/python" ] && echo "  $V -> $("$V/bin/python" -V 2>&1)  qlib=$("$V/bin/python" -c 'import qlib;print(qlib.__version__)' 2>/dev/null || echo no)"
done
echo
echo "== PG =="
systemctl is-active postgresql 2>/dev/null
echo
echo "== /data =="
ls -la /data 2>/dev/null | sed 's/^/  /'
echo
echo "== qlib cache =="
[ -f /data/qlib/cn_data/calendars/day.txt ] && echo "  day.txt $(wc -l < /data/qlib/cn_data/calendars/day.txt) 行" || echo "  未构建"
echo
echo "== 服务 =="
curl -s -m 3 http://127.0.0.1:8001/health | python3 -c "import json,sys;d=json.load(sys.stdin);print('  status',d['status'],'mounted',len(d['mounted']),'auth_disabled',d.get('auth_disabled'))" 2>/dev/null || echo "  后端未运行"
