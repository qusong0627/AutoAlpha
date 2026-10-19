#!/bin/bash
# 安装 RD-Agent 依赖 + rdagent 包到 3.12 venv。
#
# 用法：
#   bash install-rdagent.sh            # 安装依赖 + 安装 rdagent 包
#   bash install-rdagent.sh --dry-run  # 只预演，打印会改动哪些包
#
# AutoAlpha 源码来源：
#   从 quantmindoss@next 只读导出 rd-agent，不依赖其他项目的源码目录。
#   panel.parquet 与取数 SDK 的支持由 AutoAlpha RDLoopWrapper._patch_file_desc
#   在运行时提供，安装后的验证也使用同一兼容层。
#
#   装完即删临时源码；不写入本仓库、不写入上游仓库。
set -euo pipefail

MODE=${1:-install}
VENV=/home/qusong/autoalpha-venv312
PY="$VENV/bin/python"
SP="$VENV/lib/python3.12/site-packages"
UPSTREAM=/mnt/d/git/quantmindoss
SRC=$(mktemp -d /tmp/autoalpha-rdagent.XXXXXXXX)
trap 'rm -rf -- "$SRC"' EXIT
MIRROR=${MIRROR:-https://pypi.tuna.tsinghua.edu.cn/simple}

PIP_PIP=( "$PY" -m pip )
PIP_OPTS=( -i "$MIRROR" --retries 10 --timeout 120 )

"$PY" -V || { echo "venv missing: $VENV"; exit 1; }

echo "== 1. 准备 AutoAlpha 的 RD-Agent 源码 =="
git -C "$UPSTREAM" archive next -o "$SRC/rd-agent.tar" -- rd-agent
tar -xf "$SRC/rd-agent.tar" -C "$SRC"
RD_DIR="$SRC/rd-agent"
if [ ! -f "$RD_DIR/pyproject.toml" ]; then
  echo "rd-agent 源码不可用：$RD_DIR/pyproject.toml 不存在"
  exit 1
fi
REQ="$RD_DIR/requirements.txt"

if [ "$MODE" = "--dry-run" ]; then
  echo
  echo "== DRY-RUN：会改动哪些现有包（应为空/仅新增）=="
  "${PIP_PIP[@]}" install --dry-run -r "$REQ" "${PIP_OPTS[@]}" 2>&1 \
    | grep -E "Would uninstall|Attempting uninstall" || echo "  (无现有包被卸载/降级)"
  echo
  echo "== DRY-RUN：会新增哪些包 =="
  "${PIP_PIP[@]}" install --dry-run -r "$REQ" "${PIP_OPTS[@]}" 2>&1 \
    | grep -E "^Would install" || true
  exit 0
fi

echo
echo "== 清理损坏分发 ~itellm（上次中断升级残留）=="
rm -rf "$SP/~itellm" "$SP/~itellm-1.105.1.dist-info"
ls -d "$SP"/*itellm* 2>/dev/null || true

echo
echo "== 2. 安装 rd-agent 依赖 =="
"${PIP_PIP[@]}" install -r "$REQ" "${PIP_OPTS[@]}"
echo "deps rc=$?"

echo
echo "== 3. 安装 rdagent 包 =="
SETUPTOOLS_SCM_PRETEND_VERSION_FOR_RDAGENT=0.1.dev1 \
  "${PIP_PIP[@]}" install --no-cache-dir --root-user-action=ignore \
  --force-reinstall --no-deps "$RD_DIR" "${PIP_OPTS[@]}"
echo "rdagent rc=$?"

echo
echo "== 4. 清理 AutoAlpha 安装临时源码 =="
rm -rf -- "$SRC"

echo
echo "== 5. 验证 =="
"$PY" - <<'EOF'
import importlib
mods = [
    "rdagent",
    "rdagent.core.utils",
    "rdagent.utils.agent.tpl",
    "rdagent.log",  # 仓库用 from rdagent.log import rdagent_logger（属性，非子模块）
    "rdagent.app.qlib_rd_loop.conf",
    "rdagent.app.qlib_rd_loop.factor",
    "rdagent.scenarios.qlib.experiment.workspace",
    "rdagent.scenarios.qlib.experiment.utils",
]
bad = 0
for m in mods:
    try:
        importlib.import_module(m)
        print(f"  OK   {m}")
    except Exception as e:
        bad += 1
        print(f"  FAIL {m}: {type(e).__name__}: {str(e)[:100]}")

# 使用与 AutoAlpha 挖掘任务相同的运行时兼容层验证数据描述。
import shutil
import sys
import tempfile
from pathlib import Path
sys.path.insert(0, "/mnt/d/git/autoalpha")
from backend.services.engine.rd_agent.rd_loop_wrapper import RDLoopWrapper
RDLoopWrapper(market="a_share")._patch_file_desc()
from rdagent.scenarios.qlib.experiment.utils import get_file_desc
probe_dir = Path(tempfile.mkdtemp(prefix="autoalpha-parquet-desc-"))
probe = probe_dir / "panel.parquet"
try:
    import pandas as pd
    pd.DataFrame({"a": [1]}).to_parquet(probe)
    desc = get_file_desc(probe)
    ok = "panel.parquet" in desc
    print(f"  {'OK  ' if ok else 'FAIL'} get_file_desc(panel.parquet) -> {desc.splitlines()[0][:60]}")
    if not ok:
        bad += 1
finally:
    shutil.rmtree(probe_dir, ignore_errors=True)
print("failed:", bad)
raise SystemExit(1 if bad else 0)
EOF
