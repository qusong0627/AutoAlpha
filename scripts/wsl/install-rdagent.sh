#!/bin/bash
# 安装 RD-Agent 依赖 + rdagent 包到 3.12 venv。
#
# 用法：
#   bash install-rdagent.sh            # 安装依赖 + 安装 rdagent 包
#   bash install-rdagent.sh --dry-run  # 只预演，打印会改动哪些包
#
# 源码来源（重要）：
#   本镜像（autoalpha，来自 quantmindoss@next）**不含** rd-agent 源码，且
#   quantmindoss 的 rd-agent 只支持 H5 数据面；而本镜像的 rd_agent 代码下发的是
#   ``panel.parquet``（QuantDB 基础面板），rd-agent 必须原生认识它，否则
#   ``scenarios/qlib/experiment/utils.py:get_file_desc`` 直接
#   ``NotImplementedError: file type panel.parquet is not supported``。
#   支持 panel.parquet 的是 ``/mnt/d/git/superalpha/rd-agent``（部署实际使用的那份）。
#   因此优先用 superalpha 的 rd-agent；它不存在时回退到上游 quantmindoss archive
#   （仅限确认其已含 panel.parquet 支持的场景）。
#
#   装完即删临时源码；不写入本仓库、不写入上游仓库。
set -euo pipefail

MODE=${1:-install}
VENV=/home/qusong/autoalpha-venv312
PY="$VENV/bin/python"
SP="$VENV/lib/python3.12/site-packages"
UPSTREAM=/mnt/d/git/quantmindoss
SUPER=/mnt/d/git/superalpha/rd-agent
SRC=/tmp/rd-agent-src
MIRROR=${MIRROR:-https://pypi.tuna.tsinghua.edu.cn/simple}

PIP_PIP=( "$PY" -m pip )
PIP_OPTS=( -i "$MIRROR" --retries 10 --timeout 120 )

"$PY" -V || { echo "venv missing: $VENV"; exit 1; }

echo "== 0. 清理损坏分发 ~itellm（上次中断升级残留）=="
rm -rf "$SP/~itellm" "$SP/~itellm-1.105.1.dist-info"
ls -d "$SP"/*itellm* 2>/dev/null || true

echo
echo "== 1. 选定 rd-agent 源码 =="
RD_DIR=""
if [ -f "$SUPER/pyproject.toml" ]; then
  RD_DIR="$SUPER"
  echo "using superalpha rd-agent: $RD_DIR"
  grep -q "panel.parquet" "$SUPER/rdagent/scenarios/qlib/experiment/utils.py" \
    && echo "  -> 已含 panel.parquet 支持" \
    || echo "  -> 警告：该树未见 panel.parquet 支持，挖掘可能失败"
else
  echo "superalpha rd-agent 不存在，回退上游 quantmindoss@next archive"
  rm -rf "$SRC"; mkdir -p "$SRC"
  git -C "$UPSTREAM" archive next -- rd-agent | tar -x -C "$SRC"
  RD_DIR="$SRC/rd-agent"
fi
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
  [ -d "$SRC" ] && rm -rf "$SRC"
  exit 0
fi

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
echo "== 4. 删除临时源码（superalpha 为只读源，不动）==="
[ -d "$SRC" ] && rm -rf "$SRC"

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

# 关键：文件名必须正好是 panel.parquet（get_file_desc 按文件名识别），
# 否则会落到 NotImplementedError。这一步就是上次挖掘失败的直接原因。
import shutil
import tempfile
from pathlib import Path
from rdagent.scenarios.qlib.experiment.utils import get_file_desc
probe_dir = Path(tempfile.mkdtemp(prefix="qm-parquet-desc-"))
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
EOF
