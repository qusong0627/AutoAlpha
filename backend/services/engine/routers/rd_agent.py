"""RD-Agent 因子管理 REST API"""

import asyncio
import json
import logging
import math
import os
import signal
import stat
import sys
from datetime import date
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request

from backend.services.engine.auth_context import (
    assert_identity_not_spoofed,
    get_authenticated_identity,
)
from backend.services.engine.qlib_app.services.rd_agent_persistence import (
    RDAgentFactorPersistence,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/rd-agent", tags=["RD-Agent"])
persistence = RDAgentFactorPersistence()

# 跟踪在跑的轻量回测，防止重复触发
_running_backtests: set[str] = set()
_MAX_LIGHTWEIGHT_BACKTESTS = 1


async def _require_owned_factor(factor_id: str, request: Request) -> dict:
    """返回因子，若不属于当前认证用户则 404（不泄露因子是否存在）。"""
    auth_user_id, _ = get_authenticated_identity(request)
    factor = await persistence.get_factor(factor_id)
    if not factor:
        raise HTTPException(status_code=404, detail=f"Factor {factor_id} not found")
    if factor.get("user_id") != auth_user_id:
        raise HTTPException(status_code=404, detail=f"Factor {factor_id} not found")
    return factor


@router.get("/factors")
async def list_factors(
    request: Request,
    user_id: str | None = Query(None, description="已废弃：身份取自 JWT，仅用于防伪校验"),
    status: str | None = Query(None, description="按状态过滤: pending/backtesting/completed/failed"),
    limit: int = Query(50, ge=1, le=200),
):
    """列出当前用户已生成的因子"""
    auth_user_id, auth_tenant_id = get_authenticated_identity(request)
    assert_identity_not_spoofed(
        auth_user_id=auth_user_id,
        auth_tenant_id=auth_tenant_id,
        provided_user_id=user_id,
    )
    factors = await persistence.list_factors(user_id=auth_user_id, status=status, limit=limit)
    return {"code": 200, "data": {"factors": factors, "total": len(factors)}}


@router.get("/factors/{factor_id}")
async def get_factor(factor_id: str, request: Request):
    """获取单个因子详情"""
    factor = await _require_owned_factor(factor_id, request)
    return {"code": 200, "data": factor}


@router.post("/factors/{factor_id}/backtest")
async def backtest_factor(
    factor_id: str,
    request: Request,
    start_date: date | None = None,
    end_date: date | None = None,
):
    """对 RD-Agent 因子发起轻量验证（异步跑 IC/夏普，回填到因子表）"""
    factor = await _require_owned_factor(factor_id, request)

    if start_date and end_date and start_date > end_date:
        raise HTTPException(status_code=422, detail="开始日期不能晚于结束日期")

    if not factor.get("factor_code"):
        raise HTTPException(status_code=400, detail="因子代码为空，无法回测")

    if factor_id in _running_backtests:
        return {
            "code": 200,
            "data": {
                "factor_id": factor_id,
                "status": "backtesting",
                "message": "回测已在进行中",
            },
        }
    if len(_running_backtests) >= _MAX_LIGHTWEIGHT_BACKTESTS:
        raise HTTPException(
            status_code=429,
            detail="轻量回测正在运行，请等待当前任务完成后重试。",
        )

    _running_backtests.add(factor_id)
    try:
        await persistence.update_factor_metrics(factor_id, status="backtesting")
    except Exception:
        _running_backtests.discard(factor_id)
        raise

    # 后台异步执行，避免接口阻塞
    asyncio.create_task(
        _run_lightweight_backtest(
            factor_id,
            factor.get("factor_code") or "",
            start_date.isoformat() if start_date else None,
            end_date.isoformat() if end_date else None,
        )
    )

    return {
        "code": 200,
        "data": {
            "factor_id": factor_id,
            "status": "backtesting",
            "message": f"快速验证已触发: {factor.get('factor_name')}",
        },
    }


async def _run_lightweight_backtest(
    factor_id: str,
    factor_code: str,
    start_date: str | None,
    end_date: str | None,
) -> None:
    """Run untrusted factor code in an isolated process and persist its metrics."""
    try:
        metrics = await _calculate_lightweight_metrics_sandboxed(
            factor_code, start_date, end_date
        )

        await persistence.update_factor_metrics(
            factor_id,
            status="completed",
            ic_value=metrics["ic_value"],
            sharpe_ratio=metrics["sharpe_ratio"],
            annual_return=metrics["annual_return"],
        )
        logger.info(
            "[rd-backtest] %s done ic=%.4f sharpe=%s",
            factor_id,
            metrics["ic_value"],
            f"{metrics['sharpe_ratio']:.3f}"
            if metrics["sharpe_ratio"] is not None else "N/A",
        )

    except Exception as exc:
        # The child executes untrusted code. Its exception text may contain
        # readable file contents, so log only the exception class.
        logger.error("[rd-backtest] %s failed (%s)", factor_id, type(exc).__name__)
        try:
            await persistence.update_factor_metrics(
                factor_id,
                status="failed",
                metadata={"backtest_error": f"隔离回测失败 ({type(exc).__name__})"},
            )
        except Exception:
            pass
    finally:
        _running_backtests.discard(factor_id)


async def _terminate_lightweight_process_group(process) -> None:
    """Stop the sandbox session even if its leader exited before descendants."""
    pgid = process.pid  # create_subprocess_exec uses start_new_session=True
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
    except OSError:
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass

    await asyncio.sleep(0.1)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
    except OSError:
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
    if process.returncode is None:
        await process.wait()


async def _calculate_lightweight_metrics_sandboxed(
    factor_code: str,
    start_date: str | None,
    end_date: str | None,
) -> dict[str, float | None]:
    """Run a stored Factor class with no credentials/network and bounded result IO."""
    if not isinstance(factor_code, str) or not factor_code.strip():
        raise ValueError("因子代码为空")
    if len(factor_code.encode("utf-8")) > 256 * 1024:
        raise ValueError("因子代码超过 256 KiB 限制")

    # Use the same fail-closed bubblewrap policy as the other factor execution paths.
    from backend.services.engine.routers.alpha_agent import (
        _factor_child_env,
        _factor_sandbox_command,
        _new_factor_workspace,
        _remove_factor_workspace,
    )

    workspace = _new_factor_workspace(prefix="rd_lightbacktest_")
    factor_path = Path(workspace) / "factor.py"
    result_path = Path(workspace) / "metrics.json"
    child_script = f"""
import json, os, resource
resource.setrlimit(resource.RLIMIT_CPU, (240, 240))
resource.setrlimit(resource.RLIMIT_FSIZE, (32 * 1024 * 1024, 32 * 1024 * 1024))
if hasattr(resource, "RLIMIT_NPROC"):
    resource.setrlimit(resource.RLIMIT_NPROC, (128, 128))
if hasattr(os, "sched_getaffinity") and hasattr(os, "sched_setaffinity"):
    allowed_cpus = os.sched_getaffinity(0)
    if allowed_cpus:
        os.sched_setaffinity(0, {{min(allowed_cpus)}})
os.chdir({workspace!r})
import numpy as np
import pandas as pd
import qlib
from qlib.data import D

provider_uri = os.environ.get("QLIB_PROVIDER_URI")
if not provider_uri:
    raise RuntimeError("Qlib provider is not configured")
qlib.init(provider_uri=provider_uri, region="cn", kernels=1)

# __file__ 必须显式给：因子代码用 dirname(abspath(__file__)) 定位自己的目录，
# 而 exec 的命名空间默认没有它。这里因子源码本来就是从这个真实文件读的，
# 直接指回去，语义与挖掘侧一致。
namespace = {{"__file__": {str(factor_path)!r}}}
with open({str(factor_path)!r}, "r", encoding="utf-8") as source:
    untrusted_source = source.read(256 * 1024 + 1)
if len(untrusted_source.encode("utf-8")) > 256 * 1024:
    raise RuntimeError("Factor source exceeds limit")
exec(compile(untrusted_source, "<rd-agent-factor>", "exec"), namespace)
factor_cls = next((value for value in namespace.values()
                   if isinstance(value, type) and value.__module__ == "builtins"
                   and (getattr(value, "name", None)
                        or value.__name__.lower().endswith("factor"))), None)
if factor_cls is None:
    raise RuntimeError("Factor class not found")

end = {(end_date or "2024-12-31")!r}
start = {(start_date or "2024-01-01")!r}
instruments = D.instruments(market="csi300")
fields = ["$open", "$high", "$low", "$close", "$volume", "$factor"]
df = D.features(instruments, fields, start_time=start, end_time=end, freq="day")
if df.empty:
    raise RuntimeError("Qlib returned no data")

factor_inst = factor_cls()
ic_list, ret_list = [], []
for code in df.index.get_level_values(0).unique()[:50]:
    sub = df.xs(code, level=0).copy()
    if len(sub) < 30:
        continue
    try:
        fv = factor_inst(sub)
        fv_col = fv.iloc[:, 0] if hasattr(fv, "iloc") else pd.Series(fv)
    except Exception:
        continue
    fwd_ret = sub["$close"].pct_change(5).shift(-5)
    paired = pd.concat([fv_col, fwd_ret], axis=1).dropna()
    if len(paired) < 10:
        continue
    ic = paired.iloc[:, 0].corr(paired.iloc[:, 1])
    if np.isfinite(ic):
        ic_list.append(float(ic))
    cutoff = fv_col.quantile(0.7)
    longs = fwd_ret[fv_col >= cutoff].dropna()
    if len(longs) > 0:
        ret_list.append(float(longs.mean()))

if not ic_list:
    raise RuntimeError("No valid IC values")
ic_mean = float(np.mean(ic_list))
sharpe = (float(np.mean(ret_list) / (np.std(ret_list) + 1e-8) * np.sqrt(252))
          if ret_list else None)
annual_return = float(np.mean(ret_list) * 252) if ret_list else None
with open({str(result_path)!r}, "x", encoding="utf-8") as output:
    json.dump({{"ic_value": ic_mean, "sharpe_ratio": sharpe,
               "annual_return": annual_return}}, output, allow_nan=False)
"""

    process = None
    try:
        factor_path.write_text(factor_code, encoding="utf-8")
        child_env = _factor_child_env()
        # One Qlib worker is intentional; keep numerical runtimes from creating
        # a CPU-count-sized thread pool inside the bounded process sandbox.
        for variable in (
            "OMP_NUM_THREADS",
            "OPENBLAS_NUM_THREADS",
            "MKL_NUM_THREADS",
            "NUMEXPR_NUM_THREADS",
        ):
            child_env[variable] = "1"
        command = _factor_sandbox_command(
            [sys.executable, "-c", child_script], child_env, [workspace]
        )
        process = await asyncio.create_subprocess_exec(
            *command,
            env=child_env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        await asyncio.wait_for(process.wait(), timeout=300)
        await _terminate_lightweight_process_group(process)
        if process.returncode != 0:
            raise RuntimeError("隔离回测子进程失败")

        workspace_path = Path(workspace)
        if workspace_path.is_symlink() or not workspace_path.is_dir():
            raise RuntimeError("隔离回测工作目录无效")
        result_fd = os.open(
            result_path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            result_stat = os.fstat(result_fd)
            if not stat.S_ISREG(result_stat.st_mode) or result_stat.st_size > 16 * 1024:
                raise RuntimeError("隔离回测结果文件无效")
            raw_result = os.read(result_fd, 16 * 1024 + 1)
        finally:
            os.close(result_fd)
        if len(raw_result) > 16 * 1024:
            raise RuntimeError("隔离回测结果过大")
        payload = json.loads(raw_result)
        if not isinstance(payload, dict):
            raise RuntimeError("隔离回测结果结构无效")

        ic_value = payload.get("ic_value")
        sharpe_ratio = payload.get("sharpe_ratio")
        annual_return = payload.get("annual_return")
        if not isinstance(ic_value, (int, float)) or not math.isfinite(ic_value):
            raise RuntimeError("隔离回测 IC 结果无效")
        for value in (sharpe_ratio, annual_return):
            if value is not None and (
                not isinstance(value, (int, float)) or not math.isfinite(value)
            ):
                raise RuntimeError("隔离回测收益指标无效")
        return {
            "ic_value": float(ic_value),
            "sharpe_ratio": float(sharpe_ratio) if sharpe_ratio is not None else None,
            "annual_return": float(annual_return) if annual_return is not None else None,
        }
    except BaseException:
        if process is not None:
            try:
                await _terminate_lightweight_process_group(process)
            except Exception as exc:
                logger.warning(
                    "lightweight backtest process cleanup failed (%s)",
                    type(exc).__name__,
                )
        raise
    finally:
        _remove_factor_workspace(workspace)


@router.get("/stats")
async def get_stats(request: Request):
    """当前用户的 RD-Agent 因子统计信息"""
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    auth_user_id, _ = get_authenticated_identity(request)

    async with get_session(read_only=True) as session:
        rows = await session.execute(text("""
            SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE status = 'completed') AS completed,
                COUNT(*) FILTER (WHERE status = 'pending') AS pending,
                COUNT(*) FILTER (WHERE status = 'backtesting') AS backtesting,
                COUNT(*) FILTER (WHERE status = 'failed') AS failed,
                AVG(ic_value) FILTER (WHERE ic_value IS NOT NULL) AS avg_ic,
                AVG(sharpe_ratio) FILTER (WHERE sharpe_ratio IS NOT NULL) AS avg_sharpe,
                MAX(ic_value) AS best_ic,
                MAX(sharpe_ratio) AS best_sharpe
            FROM rd_agent_factors
            WHERE user_id = :user_id
        """), {"user_id": auth_user_id})
        row = rows.mappings().first()

    if not row:
        return {"code": 200, "data": {}}

    data = dict(row)
    for key in ("avg_ic", "best_ic", "avg_sharpe", "best_sharpe"):
        if data.get(key) is not None:
            data[key] = round(float(data[key]), 4)
    return {"code": 200, "data": data}
