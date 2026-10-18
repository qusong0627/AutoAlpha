"""落库后自动验证（T-MV-09）的共享启动器——LLM 派 / 统计派 runner 同一份。

两个 runner（``scripts/alpha_agent/run_rd_agent.py``、``run_statistical_mining.py``）
收尾时都经这里把 ``backend/scripts/mining_factor_validate.py`` 拉起为**后台
子进程**：不阻塞挖掘收尾、失败只告警（可事后用同一脚本手工补跑；验证器自带
全局 flock 串行化，两个 runner 并发触发也不会互踩）。

口径（唯一实现，两个 runner 不许各自复读）：

- ``persisted <= 0``：没落库任何因子 → 不启动（无对象可验）；
- 开关 ``env_flag``（默认 ``RD_AGENT_AUTO_VALIDATE``）：**缺省开**；关请显式写
  ``false``/``0``——读法走全仓唯一实现 ``backend.shared.env_flags``（只有小写
  ``true`` 判开，``1``/``yes``/``on``/空串一律判关，不许第二种读法）；
- 验证器脚本缺失 → 告警并返回 False，**不抛**：自动链失败不阻断挖掘结果；
- ``h5_path`` 给了且**真实存在**才附 ``--h5-path``（统计派无 h5，绝不附空
  路径骗验证器）；验证器对 expression 类因子在 h5 阶段（衰减/PIT）如实降级。

返回值 = 是否真的拉起（统计派把它写进 result.json 的 ``validation_spawned``，
让上层知道验证是自动挂的还是需要手工补）。
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

from backend.shared.env_flags import env_flag as _env_flag


def spawn_validation(
    *,
    task_id: str,
    market: str,
    log_dir: str | Path,
    persisted: int,
    h5_path: str = "",
    env_flag: str = "RD_AGENT_AUTO_VALIDATE",
    project_root: str | Path | None = None,
    log: logging.Logger | None = None,
) -> bool:
    """把 T-MV-09 验证器拉起为后台子进程 → 是否真的拉起。

    ``log_dir``：验证输出 ``validate.log`` 落在这里（append 模式，重跑保留
    历史）；``project_root`` 仅测试注入用，默认按本文件位置回推仓库根。
    """
    logger = log or logging.getLogger(__name__)
    if persisted <= 0:
        return False
    if not _env_flag(env_flag, default=True):
        logger.info("自动验证已关闭（%s 未设为 true）", env_flag)
        return False
    root = (
        Path(project_root)
        if project_root is not None
        else Path(__file__).resolve().parents[4]
    )
    script = root / "backend" / "scripts" / "mining_factor_validate.py"
    if not script.is_file():
        logger.warning("验证器脚本不存在，跳过自动验证：%s", script)
        return False
    cmd = [
        sys.executable,
        str(script),
        "--task-id",
        str(task_id),
        "--market",
        str(market),
    ]
    if h5_path and Path(h5_path).exists():
        cmd += ["--h5-path", str(h5_path)]
    log_file = Path(log_dir) / "validate.log"
    try:
        with open(log_file, "ab") as handle:
            subprocess.Popen(
                cmd,
                cwd=str(root),
                stdout=handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        logger.info("自动验证已启动（后台）：log=%s", log_file)
        return True
    except Exception as exc:  # noqa: BLE001 - 自动链路失败不阻断挖掘结果
        logger.warning("自动验证启动失败：%s", exc)
        return False
