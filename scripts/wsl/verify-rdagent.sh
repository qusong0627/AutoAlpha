#!/bin/bash
# 验证 rd-agent 安装 + 仓库内 RD-Agent 接线是否可用。
set -uo pipefail
PY=/home/qusong/autoalpha-venv312/bin/python
ROOT=/mnt/d/git/autoalpha

echo "== A. pip 一致性 =="
"$PY" -m pip check 2>&1 | head -20

echo
echo "== B. rdagent 包与仓库用到的导入路径 =="
PYTHONPATH="$ROOT" "$PY" - <<'EOF'
import importlib

def ok(label, fn):
    try:
        fn()
        print(f"  OK   {label}")
    except Exception as e:
        print(f"  FAIL {label}: {type(e).__name__}: {str(e)[:120]}")

# 包本身
ok("import rdagent", lambda: importlib.import_module("rdagent"))

# 仓库代码真实用到的路径
ok("from rdagent.log import rdagent_logger", lambda: __import__("rdagent.log", fromlist=["rdagent_logger"]))
ok("from rdagent.utils.agent import tpl", lambda: __import__("rdagent.utils.agent.tpl", fromlist=["tpl"]))
ok("from rdagent.core.utils import import_class", lambda: __import__("rdagent.core.utils", fromlist=["import_class"]))
ok("from rdagent.app.qlib_rd_loop.conf import FactorBasePropSetting",
   lambda: __import__("rdagent.app.qlib_rd_loop.conf", fromlist=["FactorBasePropSetting"]))
ok("from rdagent.app.qlib_rd_loop.factor import FactorRDLoop",
   lambda: __import__("rdagent.app.qlib_rd_loop.factor", fromlist=["FactorRDLoop"]))
ok("from rdagent.scenarios.qlib.experiment.workspace import QlibFBWorkspace",
   lambda: __import__("rdagent.scenarios.qlib.experiment.workspace", fromlist=["QlibFBWorkspace"]))

# 仓库侧接线模块（含惰性 rdagent import）
ok("backend.services.engine.rd_agent.stage_prompt",
   lambda: importlib.import_module("backend.services.engine.rd_agent.stage_prompt"))
ok("backend.services.engine.rd_agent.rd_loop_wrapper",
   lambda: importlib.import_module("backend.services.engine.rd_agent.rd_loop_wrapper"))
ok("backend.services.engine.rd_agent.qlib_conf_template",
   lambda: importlib.import_module("backend.services.engine.rd_agent.qlib_conf_template"))

# safe_pickle 白名单里点名的 rdagent 类
from rdagent.components.coder.factor_coder.factor import FactorTask  # noqa
print("  OK   rdagent...factor_coder.factor.FactorTask")
from rdagent.core.experiment import RunningInfo  # noqa
print("  OK   rdagent.core.experiment.RunningInfo")
EOF

echo
echo "== B2. rd-agent 对 panel.parquet 的描述支持（挖掘 Scenario 构造门槛）=="
"$PY" - <<'EOF'
import shutil, tempfile
from pathlib import Path
import pandas as pd
from rdagent.scenarios.qlib.experiment.utils import get_file_desc

d = Path(tempfile.mkdtemp(prefix="qm-parquet-desc-"))
try:
    p = d / "panel.parquet"
    pd.DataFrame({"a": [1]}).to_parquet(p)
    desc = get_file_desc(p)
    print("  OK   get_file_desc(panel.parquet) ->", desc.splitlines()[0][:70])
except Exception as e:
    print("  FAIL", type(e).__name__, e)
finally:
    shutil.rmtree(d, ignore_errors=True)
EOF

echo
echo "== C. 临时源码是否已删 =="
ls -d /tmp/rd-agent-src 2>/dev/null && echo "  STILL THERE" || echo "  已删除"

echo
echo "== D. 本仓库/上游是否被写入 =="
git -C "$ROOT" status --porcelain 2>/dev/null | head -20 || echo "  (autoalpha 非 git 仓库，跳过)"
