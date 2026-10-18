#!/bin/bash
# 修正服务写目录属主。默认只预览；传 --apply 才递归修改权限。
set -euo pipefail
OWNER=${OWNER:-qusong:qusong}
if [[ ! "$OWNER" =~ ^[a-zA-Z_][a-zA-Z0-9_-]*:[a-zA-Z_][a-zA-Z0-9_-]*$ ]]; then
  echo "FATAL: OWNER 格式必须是 user:group。" >&2
  exit 1
fi

# /data/rd_agent_kb：RD-Agent CoSTEER 经验记忆（知识库 pickle + filelock），
# 挖掘子进程在第一次 coding 步会 mkdir 它；不预建则 PermissionError 直接挂任务。
# /data/reports：报告档案目录（回测报告 PDF 导出 ensure_report_dir 在此建
# /data/reports/<namespace>/<因子研究>）；不预建则导出接口 500。
TARGETS=(/data/qlib /data/quantcustom /data/alpha_agent_logs /data/rd_agent_kb /data/reports)
for path in "${TARGETS[@]}"; do
  if [ -L "$path" ]; then
    echo "FATAL: 拒绝递归修改符号链接目标：$path" >&2
    exit 1
  fi
done

if [ "${1:-}" = "--apply" ]; then
  mkdir -p /data/qlib/cn_data /data/quantcustom/6_ml_datasets /data/alpha_agent_logs /data/rd_agent_kb /data/reports
  chown -R "$OWNER" "${TARGETS[@]}"
  chmod 755 /data
  chmod 750 /data/qlib /data/qlib/cn_data /data/quantcustom /data/reports
  chmod 700 /data/alpha_agent_logs /data/rd_agent_kb
elif [ "$#" -eq 0 ]; then
  echo "预览：将递归设置属主 $OWNER，日志/记忆目录 0700，其他服务目录 0750。"
  echo "执行修改请显式传 --apply。"
  exit 0
else
  echo "用法：$0 [--apply]" >&2
  exit 2
fi

echo "== 结果 =="
ls -la /data
echo
ls -la /data/qlib /data/quantcustom /data/alpha_agent_logs /data/rd_agent_kb /data/reports

echo
echo "== 写权限探针（以当前用户）=="
whoami
for d in /data/qlib/cn_data /data/quantcustom /data/alpha_agent_logs /data/rd_agent_kb /data/reports; do
  if touch "$d/.probe" 2>/dev/null; then echo "  可写  $d"; rm -f "$d/.probe"; else echo "  不可写 $d"; fi
done
