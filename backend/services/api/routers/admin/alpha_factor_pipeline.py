"""AlphaAgent 因子 → 训练特征自动流水线

将 AlphaAgent 挖到的因子一键推广到训练特征集：
1. 从 rd_agent_factors 读取因子
2. 提取 Qlib 表达式
3. 用 Qlib 计算因子值
4. 写入用户自定义数据集 (data/quantcustom/6_ml_datasets/l1_factors)
5. 注册到特征目录 (qm_feature_*)
"""

import asyncio
import hashlib
import json
import logging
import math
import multiprocessing
import os
import re
import threading
import uuid
from concurrent.futures import BrokenExecutor, ProcessPoolExecutor
from typing import Annotated, Any, Optional

import numpy as np
import pandas as pd
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text

from backend.services.api.user_app.middleware.auth import require_admin
from backend.shared.database_manager_v2 import get_session

logger = logging.getLogger(__name__)
router = APIRouter(
    tags=["AlphaFactorPipeline"],
    dependencies=[Depends(require_admin)],  # 路由器级认证兜底，新增端点默认受保护
)

# ── 路径配置 ──
# 因子挖掘/历史补全属用户产出，统一写用户自定义数据集
# （data/quantcustom/6_ml_datasets/l1_factors），不写官方 QuantDB，也不再写
# db/feature_snapshots/model_features_*.parquet 旧入口。
# Qlib 目录统一走 qlib_paths 解析（固定目录优先）
try:
    from backend.shared.qlib_paths import resolve_qlib_provider_uri
    QLIB_PROVIDER_URI = resolve_qlib_provider_uri("CN")
except Exception:
    QLIB_PROVIDER_URI = "/data/qlib/cn_data"


class PromoteRequest(BaseModel):
    factor_ids: list[Annotated[str, Field(min_length=1, max_length=128)]] = Field(
        default_factory=list, max_length=100
    )
    auto_train: bool = False


class PromoteExpressionFactor(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    expression: str = Field(min_length=1, max_length=2048)


class PromoteExpressionRequest(BaseModel):
    """直接用表达式推广因子（不依赖 rd_agent_factors 表中的 ID）。"""
    factors: list[PromoteExpressionFactor] = Field(max_length=20)
    auto_train: bool = False


class PromoteResponse(BaseModel):
    success: bool
    promoted: list[dict[str, Any]]
    errors: list[dict[str, Any]]
    auto_train_triggered: bool = False


AUTO_TRAIN_UNAVAILABLE_MESSAGE = (
    "因子已推广，但当前独立部署未接入训练 API；请手动启动训练。"
)


# ═══════════════════════════════════════════════════════════════════
# 因子表达式提取
# ═══════════════════════════════════════════════════════════════════


def _extract_qlib_expression(factor_code: str, metadata: dict) -> str | None:
    """从因子代码或 metadata 中提取 Qlib 表达式。"""
    # 1. 优先从 metadata 取
    formulation = metadata.get("formulation", "")
    if formulation and _is_qlib_expression(formulation):
        return formulation.strip()

    # 2. 只从源码文本中提取字面量表达式。禁止 exec 因子源码：此 GET 接口
    # 会读取存量记录，exec 会让一次只读请求变成任意代码执行。
    m = re.search(r'return\s+["\'](.+?)["\']', factor_code)
    if m and _is_qlib_expression(m.group(1)):
        return m.group(1).strip()

    return None


# 表达式最终仍交给 qlib 的 eval(parse_field(...))。因此这里采用严格的词法
# 白名单：禁止字符串、下标、属性访问、关键字、反斜杠等 Python 语法；函数名
# 必须是已知的 qlib/FM 算子。表达式长度、token 数和嵌套深度同时设上限。
_QLIB_EXPR_TOKEN = re.compile(
    r"\s*(\$[A-Za-z][A-Za-z0-9_]*|[A-Za-z][A-Za-z0-9_]*|"
    r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?|"
    r"&&|\|\||>=|<=|==|!=|[()+*/<>=,!?:-])"
)
_QLIB_EXPR_FUNCTIONS = frozenset(
    {
        # FactorMiner DSL operators and their qlib-export equivalents.
        "Abs", "Add", "And", "Corr", "Cov", "CsRank", "Delay", "Delta",
        "Div", "EMA", "Eq", "Greater", "If", "IfElse", "Kurt", "Less",
        "Log", "Max", "Max2", "Mean", "Med", "Min", "Min2", "Mul", "Neg",
        "Or", "Power", "Rank", "Ref", "Resi", "Rsquare", "Sign", "SignedPower",
        "Skew", "Slope", "SMA", "Std", "Sub", "Sum", "TsArgMax", "TsMax",
        "TsMin", "TsRank", "WMA",
        # Common Qlib operators retained for existing statistical factors.
        "Count", "DELAY", "FILTER", "IdxMax", "IdxMin", "MACD", "Prod",
        "REGBETA", "REGRESI", "RSI", "SUMIF", "SumIf",
    }
)


def _is_qlib_expression(expr: str) -> bool:
    """只接受不含 Python 执行能力的 Qlib 表达式子集。"""
    if not isinstance(expr, str) or not expr or len(expr) > 2048:
        return False

    tokens: list[str] = []
    position = 0
    while position < len(expr):
        match = _QLIB_EXPR_TOKEN.match(expr, position)
        if match is None:
            if not expr[position:].strip():
                break
            return False
        tokens.append(match.group(1))
        if len(tokens) > 512:
            return False
        position = match.end()

    if not tokens:
        return False

    depth = 0
    for index, token in enumerate(tokens):
        if token == "(":
            depth += 1
            if depth > 64:
                return False
        elif token == ")":
            depth -= 1
            if depth < 0:
                return False
        elif token[0].isalpha():
            # Bare identifiers (including __builtins__, globals, and attributes)
            # are never expression values. Only explicit allowlisted calls pass.
            if (
                token not in _QLIB_EXPR_FUNCTIONS
                or index + 1 >= len(tokens)
                or tokens[index + 1] != "("
            ):
                return False
    return depth == 0 and any(token.startswith("$") for token in tokens)


def _sanitize_feature_key(name: str) -> str:
    """将因子名转为合法且稳定的 feature_key，避免有损清洗造成覆盖。"""
    normalized = (name or "").strip()
    key = re.sub(r"[^a-zA-Z0-9_]", "_", normalized).lower().strip("_")
    key = re.sub(r"_+", "_", key)
    prefix = "alpha_"

    # Preserve legacy keys for ordinary short ASCII names. Punctuation, whitespace,
    # non-ASCII names, underscore normalization, and truncation are lossy; append a
    # stable digest so distinct names cannot silently upsert into the same feature.
    lossy = key != normalized.lower() or len(prefix) + len(key) > 80
    suffix = ""
    if lossy:
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
        suffix = f"_{digest}"
    if not key:
        key = "factor"
        if not suffix:
            digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]
            suffix = f"_{digest}"

    max_key_length = 80 - len(prefix) - len(suffix)
    key = key[:max_key_length].rstrip("_") or "factor"
    return f"{prefix}{key}{suffix}"


def _env_int(name: str, default: int, *, max_value: int) -> int:
    """读正整数环境变量并施加代码级上限；非法/缺失/非正数回落 default。"""
    try:
        value = int(str(os.getenv(name, "")).strip())
    except (TypeError, ValueError):
        return default
    if value <= 0:
        return default
    if value > max_value:
        logger.warning("%s 超过代码上限 %d，已截断", name, max_value)
        return max_value
    return value


def _env_float(name: str, default: float, *, max_value: float) -> float:
    """读有限正浮点环境变量并施加上限；非法/缺失/非正数回落 default。"""
    try:
        value = float(str(os.getenv(name, "")).strip())
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value) or value <= 0:
        return default
    if value > max_value:
        logger.warning("%s 超过代码上限 %.0f，已截断", name, max_value)
        return max_value
    return value


# ═══════════════════════════════════════════════════════════════════
# Qlib 表达式计算
# ═══════════════════════════════════════════════════════════════════


_qlib_initialized = False


def _ensure_qlib_init():
    """确保 Qlib 已初始化（惰性初始化，只执行一次）。"""
    global _qlib_initialized
    if _qlib_initialized:
        return
    try:
        import qlib
        from qlib.config import C
        # kernels = joblib worker 上限。qlib 默认取可用 CPU 数（本机 36 核），
        # 一次重计算就会派生二十多个进程并继承监听 socket，把端口和内存一起拖住。
        # 用 QLIB_KERNELS 钉住（默认 4）。
        qlib.init(
            provider_uri=QLIB_PROVIDER_URI,
            region="cn",
            kernels=_env_int("QLIB_KERNELS", 4, max_value=4),
        )
        _qlib_initialized = True
        logger.info(
            "Qlib initialized with provider_uri=%s kernels=%s",
            QLIB_PROVIDER_URI,
            C.get_kernels("day"),
        )
    except Exception as e:
        logger.error("Failed to init Qlib: %s", e)
        raise RuntimeError(f"Qlib 初始化失败: {e}") from e


def compute_factor_via_qlib(expression: str, feature_name: str) -> pd.DataFrame:
    """用 Qlib 引擎计算因子表达式，返回 MultiIndex(date, instrument) DataFrame。"""
    # Qlib's parser evaluates expressions. Keep validation at this execution sink
    # as well as at HTTP boundaries so internal callers cannot bypass the DSL.
    if not _is_qlib_expression(expression):
        raise ValueError("非法 Qlib 表达式")
    _ensure_qlib_init()
    from qlib.data import D

    instruments = D.instruments(market="csi300")
    df = D.features(instruments, [expression], freq="day")
    if df.empty:
        raise RuntimeError(f"Qlib 返回空数据，表达式可能无效: {expression}")
    df.columns = [feature_name]
    return df


# ── P0-4：把重计算移出 API 进程 ─────────────────────────────────────────
# 演进过程（实测数据，别退回旧版）：
#   v0 同步调用：一次请求占死事件循环 6 分钟以上；进程收到 SIGTERM 也无法退出
#               （uvicorn 优雅退出依赖事件循环），只能 kill -9；并派生 34 个
#               继承监听 socket 的 joblib worker，端口都释放不掉。
#   v1 线程池  ：永久卡死消失、进程数有界，但 qlib 的重活里有大量 C 层操作
#               （DataFrame 的 pickle/unpickle 等）会长时间持有 GIL —— 实测重请求
#               期间 /health 连续两次 12s 探活全部超时。**GIL 不是线程能隔离的。**
#   v2 进程池（当前）：计算与落盘都在子进程，父进程只收一个小 dict。
#   用 spawn 而非 fork：fork 会让子进程继承父进程的 asyncio/asyncpg 连接状态，
#   子进程退出时的资源清理可能破坏父进程的数据库连接。
_PROMOTE_MAX_CONCURRENCY = _env_int(
    "FACTOR_PROMOTE_MAX_CONCURRENCY", 2, max_value=4
)
_PROMOTE_TIMEOUT_SECONDS = _env_float(
    "FACTOR_PROMOTE_TIMEOUT_SECONDS", 180.0, max_value=1800.0
)
_PROMOTE_EXECUTOR: Optional[ProcessPoolExecutor] = None
_PROMOTE_RESET_LOCK = threading.Lock()
_PROMOTE_WORKER_ENV_KEYS = (
    "PATH", "HOME", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "TZ",
    "QLIB_PROVIDER_URI", "QLIB_KERNELS", "QM_QUANTDB_DATA_DIR",
    "QM_QUANTCUSTOM_DATA_DIR", "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS",
    "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS",
)


def _sanitize_promote_worker_environment() -> None:
    """Drop service credentials from Qlib worker processes before task execution."""
    safe = {
        key: os.environ[key]
        for key in _PROMOTE_WORKER_ENV_KEYS
        if os.environ.get(key)
    }
    os.environ.clear()
    os.environ.update(safe)


def _promote_in_child(expression: str, feature_name: str) -> dict:
    """在**子进程**里算因子并落盘，只回传小结果（绝不回传 DataFrame）。

    只回传 {"non_null": int} 是刻意的：父进程反序列化一个几 MB 的 DataFrame
    同样会长时间持有 GIL，等于把刚隔离出去的问题又搬回来。
    """
    factor_df = compute_factor_via_qlib(expression, feature_name)
    non_null = merge_factor_into_parquet(factor_df, feature_name)
    return {"non_null": int(non_null)}


def _promote_executor() -> ProcessPoolExecutor:
    """惰性建池：只在父进程建。spawn 子进程 import 本模块时不会触发。"""
    global _PROMOTE_EXECUTOR
    if _PROMOTE_EXECUTOR is None:
        _PROMOTE_EXECUTOR = ProcessPoolExecutor(
            max_workers=_PROMOTE_MAX_CONCURRENCY,
            mp_context=multiprocessing.get_context("spawn"),
            initializer=_sanitize_promote_worker_environment,
        )
    return _PROMOTE_EXECUTOR


def _terminate_promote_executor(executor: ProcessPoolExecutor) -> None:
    """Terminate and shut down a failed pool; this blocking cleanup runs off-loop."""
    with _PROMOTE_RESET_LOCK:
        # ProcessPoolExecutor has no public per-task hard-cancel API. A timed-out pool
        # is untrusted, so terminate every worker to avoid orphaned computations.
        worker_map = getattr(executor, "_processes", None) or {}
        workers = list(worker_map.values())
        for process in workers:
            try:
                if process.is_alive():
                    process.terminate()
            except Exception:  # noqa: BLE001 - 清理单个 worker 失败不影响其余 worker
                logger.warning("终止超时因子 worker 失败", exc_info=True)
        for process in workers:
            try:
                process.join(timeout=1)
                if process.is_alive() and hasattr(process, "kill"):
                    process.kill()
                    process.join(timeout=1)
            except Exception:  # noqa: BLE001 - best-effort 清理
                logger.warning("回收超时因子 worker 失败", exc_info=True)
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except Exception:  # noqa: BLE001 - best-effort shutdown
            logger.warning("关闭失效因子 worker 池失败", exc_info=True)


async def _reset_promote_executor(executor: ProcessPoolExecutor) -> None:
    """Detach only the failed pool, then terminate it without blocking the event loop."""
    global _PROMOTE_EXECUTOR
    if _PROMOTE_EXECUTOR is executor:
        _PROMOTE_EXECUTOR = None
    await asyncio.to_thread(_terminate_promote_executor, executor)


_PROMOTE_SLOTS = asyncio.Semaphore(_PROMOTE_MAX_CONCURRENCY)


async def _watch_promote_future_deadline(future: Any, executor: ProcessPoolExecutor) -> None:
    """Enforce the compute deadline even if the HTTP request is cancelled."""
    await asyncio.sleep(_PROMOTE_TIMEOUT_SECONDS)
    if not future.done() and _PROMOTE_EXECUTOR is executor:
        logger.warning("因子计算超过硬超时；后台重置对应 worker 池")
        await _reset_promote_executor(executor)


async def _promote_offloop(expression: str, feature_name: str) -> dict:
    """在子进程里算因子并落盘，带硬超时；父进程只处理小结果。"""
    try:
        await asyncio.wait_for(_PROMOTE_SLOTS.acquire(), timeout=0.01)
    except asyncio.TimeoutError as exc:
        raise HTTPException(
            status_code=429,
            detail="因子计算任务已达并发上限，请稍后重试。",
        ) from exc

    future = None
    timeout_watchdog: asyncio.Task[None] | None = None
    loop = asyncio.get_running_loop()
    try:
        executor = _promote_executor()
        try:
            future = executor.submit(_promote_in_child, expression, feature_name)
        except BrokenExecutor as exc:
            await _reset_promote_executor(executor)
            raise HTTPException(
                status_code=503,
                detail="因子计算 worker 池不可用，已重置，请稍后重试。",
            ) from exc
        timeout_watchdog = asyncio.create_task(
            _watch_promote_future_deadline(future, executor),
            name="factor-promotion-hard-timeout",
        )
        wrapped_future = asyncio.wrap_future(future)
        wrapped_future.add_done_callback(
            lambda completed: completed.exception()
            if not completed.cancelled()
            else None
        )
        try:
            return await asyncio.wait_for(
                asyncio.shield(wrapped_future), timeout=_PROMOTE_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError as exc:
            await _reset_promote_executor(executor)
            raise HTTPException(
                status_code=504,
                detail=(
                    f"因子计算超时（>{_PROMOTE_TIMEOUT_SECONDS:.0f}s）：表达式过重或数据未就绪。"
                    f"超时 worker 已终止，并发上限 {_PROMOTE_MAX_CONCURRENCY}。"
                ),
            ) from exc
        except BrokenExecutor as exc:
            await _reset_promote_executor(executor)
            raise HTTPException(
                status_code=503,
                detail="因子计算 worker 异常退出，进程池已重置，请稍后重试。",
            ) from exc
    finally:
        if future is None:
            _PROMOTE_SLOTS.release()
        else:
            # Request cancellation only cancels its shield; a running ProcessPool job
            # cannot be cancelled that way. Hold the capacity slot until the
            # underlying process future finishes. The watchdog survives disconnects
            # and still enforces the hard deadline.
            def release_slot_when_done(_completed_future) -> None:
                def release_on_loop() -> None:
                    _PROMOTE_SLOTS.release()
                    if timeout_watchdog is not None and not timeout_watchdog.done():
                        timeout_watchdog.cancel()

                try:
                    loop.call_soon_threadsafe(release_on_loop)
                except RuntimeError:
                    # The event loop is already closing; no requests can acquire it.
                    pass

            future.add_done_callback(release_slot_when_done)


def _warm_child() -> bool:
    """子进程预热：把 qlib 初始化提前做掉。

    否则第一次 promote 请求会把 spawn + import 整条依赖链 + qlib.init 的开销
    一起算进自己的超时预算（实测冷启动 >15s）。
    """
    _ensure_qlib_init()
    return True


def warm_promote_executor() -> None:
    """启动期调用（不阻塞、失败不致命）：提前 spawn 子进程并初始化 qlib。"""
    try:
        _promote_executor().submit(_warm_child)
    except Exception:  # noqa: BLE001 - 预热失败只影响首次请求耗时
        logger.warning("promote 进程池预热失败", exc_info=True)


# ═══════════════════════════════════════════════════════════════════
# 用户自定义数据集写入
# ═══════════════════════════════════════════════════════════════════


def merge_factor_into_parquet(factor_df: pd.DataFrame, feature_name: str) -> int:
    """将因子列写入用户自定义数据集，返回非空值数。

    因子挖掘与历史补全是用户产出，落 ``data/quantcustom/6_ml_datasets/l1_factors``
    （CUSTOM 市场），不写官方 QuantDB ``6_ml_datasets/l1_factors``。仅追加/覆盖单列，
    按 6 位代码对齐 symbol；分区不存在时按 (symbol,date) 新建，之后经后台
    「字段发现 + 草稿发布」注册为可用训练源。
    """
    from backend.shared.feature_source import merge_factor_into_source

    return merge_factor_into_source(
        factor_df,
        feature_name,
        source="l1_factors",
        market="CUSTOM",
        create_missing=True,
    )


# ═══════════════════════════════════════════════════════════════════
# 特征目录注册
# ═══════════════════════════════════════════════════════════════════


async def register_feature_in_catalog(
    feature_key: str,
    feature_name: str,
    formula: str,
    category_id: str = "alpha_agent",
) -> None:
    """将新特征注册到 qm_feature_* 目录表。"""
    async with get_session() as session:
        # 1. 确保 alpha_agent 分类存在
        await session.execute(text("""
            INSERT INTO qm_feature_category (category_id, category_name, sort_order, description)
            VALUES (:cat_id, :cat_name, 99, 'AlphaAgent 自动挖掘的因子')
            ON CONFLICT (category_id) DO NOTHING
        """), {"cat_id": category_id, "cat_name": "AlphaAgent 挖掘因子"})

        # 2. 插入特征定义
        feature_id = str(uuid.uuid4())
        await session.execute(text("""
            INSERT INTO qm_feature_definition (feature_id, feature_key, feature_name, formula, category_id)
            VALUES (:fid, :fkey, :fname, :formula, :cat_id)
            ON CONFLICT (feature_key) DO UPDATE SET
                feature_name = EXCLUDED.feature_name,
                formula = EXCLUDED.formula,
                updated_at = now()
        """), {
            "fid": feature_id,
            "fkey": feature_key,
            "fname": feature_name,
            "formula": formula,
            "cat_id": category_id,
        })

        # 3. 获取当前活跃版本
        version_row = await session.execute(text("""
            SELECT version_id FROM qm_feature_set_version
            WHERE status = 'active'
            ORDER BY effective_at DESC, created_at DESC
            LIMIT 1
        """))
        version = version_row.mappings().first()
        if not version:
            logger.warning("No active feature set version found, skipping set_item registration")
            return

        version_id = version["version_id"]

        # This legacy table allocates integer IDs with MAX(id) + 1 and order numbers
        # with MAX(order_no) + 1. Serialize both reads/writes so concurrent
        # promotions cannot choose the same primary key or order slot.
        await session.execute(
            text(
                "SELECT pg_advisory_xact_lock("
                "hashtext('autoalpha_feature_set_item'), hashtext('allocate_id_and_order'))"
            )
        )

        # 4. 获取当前最大 order_no
        max_order_row = await session.execute(text("""
            SELECT COALESCE(MAX(order_no), 0) + 1 AS next_order
            FROM qm_feature_set_item
            WHERE version_id = :vid
        """), {"vid": version_id})
        next_order = max_order_row.scalar() or 999

        # 5. Insert only after the advisory lock made MAX(id) allocation exclusive.
        max_id_row = await session.execute(
            text("SELECT COALESCE(MAX(id), 0) + 1 FROM qm_feature_set_item")
        )
        next_id = max_id_row.scalar() or 1

        await session.execute(text("""
            INSERT INTO qm_feature_set_item (id, version_id, category_id, feature_key, order_no, enabled)
            VALUES (:id, :vid, :cat_id, :fkey, :order_no, true)
            ON CONFLICT (version_id, feature_key) DO UPDATE SET
                enabled = true,
                order_no = EXCLUDED.order_no
        """), {
            "id": next_id,
            "vid": version_id,
            "cat_id": category_id,
            "fkey": feature_key,
            "order_no": next_order,
        })

        # 6. 更新版本特征计数
        await session.execute(text("""
            UPDATE qm_feature_set_version
            SET feature_count = (
                SELECT COUNT(*) FROM qm_feature_set_item
                WHERE version_id = :vid AND enabled = true
            )
            WHERE version_id = :vid
        """), {"vid": version_id})

    logger.info("Registered feature %s (%s) in catalog", feature_key, feature_name)


# ═══════════════════════════════════════════════════════════════════
# API 端点
# ═══════════════════════════════════════════════════════════════════


@router.post("/promote", response_model=PromoteResponse)
async def promote_factors(req: PromoteRequest):
    """将 AlphaAgent 因子推广到训练特征集。

    流程：
    1. 从 rd_agent_factors 读取因子
    2. 提取 Qlib 表达式
    3. 计算因子值并写入用户自定义数据集
    4. 注册到特征目录
    5. 可选：自动触发训练
    """
    if not req.factor_ids:
        raise HTTPException(status_code=400, detail="factor_ids 不能为空")

    # 读取因子
    async with get_session(read_only=True) as session:
        placeholders = ", ".join(f":fid_{i}" for i in range(len(req.factor_ids)))
        params = {f"fid_{i}": fid for i, fid in enumerate(req.factor_ids)}
        rows = await session.execute(text(f"""
            SELECT factor_id, factor_name, factor_code, metadata_json, ic_value, sharpe_ratio
            FROM rd_agent_factors
            WHERE factor_id IN ({placeholders})
        """), params)
        factors = [dict(r) for r in rows.mappings().all()]

    if not factors:
        raise HTTPException(status_code=404, detail="未找到指定的因子")

    promoted = []
    errors = []

    for factor in factors:
        factor_id = factor["factor_id"]
        factor_name = factor["factor_name"]
        factor_code = factor.get("factor_code", "")
        metadata = factor.get("metadata_json", {})
        if isinstance(metadata, str):
            try:
                metadata = json.loads(metadata)
            except Exception:
                metadata = {}

        try:
            # 1. 提取 Qlib 表达式
            expression = _extract_qlib_expression(factor_code, metadata)
            if not expression:
                errors.append({
                    "factor_id": factor_id,
                    "factor_name": factor_name,
                    "error": "无法提取 Qlib 表达式（因子可能是纯 pandas 代码）",
                })
                continue

            # 2. 生成 feature_key
            feature_key = _sanitize_feature_key(factor_name)

            # 3+4. 计算因子值并写入用户自定义数据集（都在子进程内完成）
            _promote_result = await _promote_offloop(expression, feature_key)
            non_null = _promote_result["non_null"]

            # 5. 注册到特征目录
            await register_feature_in_catalog(
                feature_key=feature_key,
                feature_name=factor_name,
                formula=expression,
            )

            promoted.append({
                "factor_id": factor_id,
                "factor_name": factor_name,
                "feature_key": feature_key,
                "expression": expression,
                "non_null_values": non_null,
                "ic_value": factor.get("ic_value"),
                "sharpe_ratio": factor.get("sharpe_ratio"),
            })

        except HTTPException:
            # 504（超时）/ 400 / 404 必须原样上抛：被下面笼统的 except 吞掉后，
            # 客户端只会看到 200 + "推广失败"，分不清"超时"还是"因子代码错"。
            raise
        except Exception:
            logger.exception("Failed to promote factor %s", factor_name)
            errors.append({
                "factor_id": factor_id,
                "factor_name": factor_name,
                "error": "因子推广失败，请检查因子代码",
            })

    # 可选：自动触发训练
    auto_train_triggered = False
    if req.auto_train and promoted:
        # The standalone app does not mount a training router. Report the missing
        # action explicitly instead of claiming a training job was started.
        errors.append({
            "kind": "auto_train",
            "error": AUTO_TRAIN_UNAVAILABLE_MESSAGE,
        })

    return PromoteResponse(
        success=len(promoted) > 0,
        promoted=promoted,
        errors=errors,
        auto_train_triggered=auto_train_triggered,
    )


@router.get("/extractable")
async def list_extractable_factors(
    limit: int = Query(default=50, ge=1, le=200),
    status: str | None = "completed",
):
    """列出可提取 Qlib 表达式的因子（已完成回测且有代码）。"""
    async with get_session(read_only=True) as session:
        conditions = ["factor_code IS NOT NULL", "factor_code != ''"]
        params: dict[str, Any] = {"limit": limit}
        if status:
            conditions.append("status = :status")
            params["status"] = status

        where = " AND ".join(conditions)
        rows = await session.execute(text(f"""
            SELECT factor_id, factor_name, factor_code, metadata_json,
                   ic_value, sharpe_ratio, annual_return, created_at
            FROM rd_agent_factors
            WHERE {where}
            ORDER BY created_at DESC
            LIMIT :limit
        """), params)

        results = []
        for r in rows.mappings().all():
            item = dict(r)
            metadata = item.get("metadata_json", {})
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except Exception:
                    metadata = {}

            # 尝试提取表达式
            expression = _extract_qlib_expression(item.get("factor_code", ""), metadata)
            item["extractable"] = expression is not None
            item["qlib_expression"] = expression
            item.pop("factor_code", None)
            item.pop("metadata_json", None)
            results.append(item)

    return {"success": True, "data": results, "total": len(results)}


@router.post("/promote-by-expression", response_model=PromoteResponse)
async def promote_factors_by_expression(req: PromoteExpressionRequest):
    """直接用因子名 + Qlib 表达式推广到训练特征集（不依赖 rd_agent_factors 表）。"""
    if not req.factors:
        raise HTTPException(status_code=400, detail="factors 不能为空")

    promoted = []
    errors = []

    for item in req.factors:
        factor_name = item.name.strip()
        expression = item.expression.strip()

        if not factor_name or not expression:
            errors.append({"factor_name": factor_name, "error": "name 和 expression 不能为空"})
            continue

        if not _is_qlib_expression(expression):
            errors.append({"factor_name": factor_name, "error": f"不是合法的 Qlib 表达式: {expression[:100]}"})
            continue

        try:
            feature_key = _sanitize_feature_key(factor_name)
            # 计算 + 落盘都在子进程完成，这里只拿到一个小结果
            _promote_result = await _promote_offloop(expression, feature_key)
            non_null = _promote_result["non_null"]
            await register_feature_in_catalog(
                feature_key=feature_key,
                feature_name=factor_name,
                formula=expression,
            )
            promoted.append({
                "factor_name": factor_name,
                "feature_key": feature_key,
                "expression": expression,
                "non_null_values": non_null,
            })
        except HTTPException:
            # 同上：504 必须原样上抛，不能被吞成 200。
            raise
        except Exception:
            logger.exception("Failed to promote factor %s", factor_name)
            errors.append({"factor_name": factor_name, "error": "因子推广失败"})

    if req.auto_train and promoted:
        errors.append({
            "kind": "auto_train",
            "error": AUTO_TRAIN_UNAVAILABLE_MESSAGE,
        })

    return PromoteResponse(
        success=len(promoted) > 0,
        promoted=promoted,
        errors=errors,
        auto_train_triggered=False,
    )
