"""挖掘任务中心 store —— ``rd_agent_mining_tasks``（机构级 P0 / T-FM-01）。

P3 增补：任务行携带**去重指纹**（同用户×同指纹的活跃任务受部分唯一索引
约束，见 :data:`DEDUPE_INDEX`）、**算法版本**与**参数/数据版本快照**
（JSON 文本列）——历史任务的口径不再随 manifest 改版或数据日更被改写。

任务状态此前只在进程内存 + `/tmp/alpha_agent_logs/<task_id>/task_state.json`：
engine 一重启就失忆、任务列表上限 20 条、direction 从不落库。本模块给出
PG 权威的任务记录行，让「每次挖了什么 / 挖出几个因子 / 什么状态」可回看。

纪律（与本仓其它 store 一致 + 项目时间口径）：

- 三列时间全走 `utc_now()` 写入、`to_utc_iso` 输出（TIMESTAMPTZ + Z 后缀）；
- 终态只能走 :meth:`mark_terminal`：它同时维护 ``completed_at``。
  ``update_progress`` 只收 pending/running——从进度通道把任务写成 completed
  会绕过 completed_at 的维护（历史页只能显示「完成时间未知」）；
- 状态白名单在**进 SQL 前**解析：未知状态显式 ValueError，绝不静默查空
  （静默查空会让「挖了但没显示」变成无从定位的失忆）。任务状态与因子状态
  不同名（任务有 cancelled、没有 backtesting），跨域混用靠白名单拦下；
- 活跃任务由进程 ``owner_id`` 持有；进度/排队租约周期续期，
  :meth:`reconcile_orphans` 回收过期的 pending/queued/running 行。
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import timedelta
from collections.abc import Mapping
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from backend.shared.database_manager_v2 import get_session
from backend.shared.secret_redaction import redact_data
from backend.shared.utc_datetime import to_utc_iso, utc_now

logger = logging.getLogger(__name__)

DEFAULT_HISTORY_LIMIT = 50
MAX_HISTORY_LIMIT = 200
MAX_DIRECTION_CHARS = 20000
MIN_TASK_LEASE_SECONDS = 120
MAX_TASK_LEASE_SECONDS = 3600


def _resolve_task_lease_seconds(raw: str | None) -> int:
    try:
        configured = int(raw) if raw is not None else 180
    except ValueError:
        logger.warning("invalid ALPHA_TASK_LEASE_SECONDS; using default")
        return 180
    bounded = min(
        MAX_TASK_LEASE_SECONDS,
        max(MIN_TASK_LEASE_SECONDS, configured),
    )
    if bounded != configured:
        logger.warning("ALPHA_TASK_LEASE_SECONDS outside allowed range; clamped")
    return bounded


TASK_LEASE_SECONDS = _resolve_task_lease_seconds(
    os.getenv("ALPHA_TASK_LEASE_SECONDS")
)
# A fresh identity per backend process. Tasks and heartbeats are owned by the
# process that holds their in-memory execution state; a restart gets a new ID.
TASK_OWNER_ID = str(uuid.uuid4())


class TaskLeaseLostError(RuntimeError):
    """Raised when this process no longer owns an active persisted task row."""

#: 单文档任务明细（T-FM-19b 一文档多方向）的返回上限；真实总数另走
#: :meth:`MiningTaskStore.count_tasks_by_docs`——明细截断时界面靠它报「共 N 个」，
#: 绝不把「最近 20 条」冒充全部。
DOC_TASKS_LIMIT = 20

# 任务状态全集。注意与**因子**状态（pending/backtesting/completed/failed）
# 不是同一套：任务多了 queued/cancelled、没有 backtesting。
# queued = 已达并发上限、等名额的排队任务（launcher 排水后翻 running）；
# 队列驻留由所属进程续租，进程消失后也按租约回收。
TASK_STATUSES = ("pending", "queued", "running", "completed", "failed", "cancelled")
TERMINAL_STATUSES = ("completed", "failed", "cancelled")
_PROGRESS_STATUSES = ("pending", "running")
#: 建行时允许的初始状态：running 只能由心跳/对账到来，终态只能走 mark_terminal。
_INITIAL_STATUSES = ("pending", "queued")

_TS_FIELDS = ("created_at", "updated_at", "completed_at")

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS rd_agent_mining_tasks (
  task_id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  market TEXT NOT NULL DEFAULT 'a_share',
  universe TEXT,
  data_source TEXT,
  direction TEXT NOT NULL DEFAULT '',
  direction_mode TEXT,
  direction_meta TEXT,
  quality_gate_mode TEXT,
  mining_start TEXT,
  mining_end TEXT,
  valid_start TEXT,
  valid_end TEXT,
  datasets TEXT,
  freq TEXT,
  algorithm TEXT,
  loop_n INTEGER,
  -- 去重指纹（P3）：同用户×同指纹的活跃任务只有一个（见部分唯一索引）。
  -- 老行为 NULL（迁移前无指纹）——不参与去重，读取方按「未知」解释。
  fingerprint TEXT,
  -- 提交时刻的算法 manifest 版本（P3 参数快照）：manifest 改版不改写历史任务。
  algorithm_version TEXT,
  -- 参数快照 / 数据版本快照（P3）：JSON 文本，见 launcher 建行处。
  params_snapshot TEXT,
  data_versions TEXT,
  source TEXT NOT NULL DEFAULT 'text',
  doc_id TEXT,
  status TEXT NOT NULL DEFAULT 'pending',
  progress_pct INTEGER NOT NULL DEFAULT 0,
  current_loop INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  factor_count INTEGER NOT NULL DEFAULT 0,
  created_at TIMESTAMPTZ NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL,
  completed_at TIMESTAMPTZ,
  owner_id TEXT,
  lease_expires_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_rd_mining_tasks_user ON rd_agent_mining_tasks (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_rd_mining_tasks_status ON rd_agent_mining_tasks (status);
"""
# 去重唯一索引（P3）**不进上面这段**：本常量会先于「老库补列」执行，老表在
# ADD COLUMN 之前没有 fingerprint 列，索引会把这整段 DDL 打挂（实测：老库上
# 建表语句整批失败，历史页凭空缺任务）。它只由 ensure_tables 在补列之后建。
# 另：常量末尾不要留纯注释片段——`split(';')` 会把它当一条语句交给 asyncpg，
# 注释-only 语句在参数解析里直接 TypeError。


#: 活跃状态（去重指纹的唯一索引谓词与查重查询共用这一份口径）。
#: pending/queued/running = 「这个配置还在跑（或即将跑）」——同指纹再提交
#: 应当复用既有任务而不是再建一个；终态（completed/failed/cancelled）
#: 不拦重跑。
ACTIVE_STATUSES = ("pending", "queued", "running")

#: 去重部分唯一索引名（建索引 / 识别冲突 / 测试断言共用）。
DEDUPE_INDEX = "uq_rd_mining_tasks_active_fingerprint"

#: JSON 列（TEXT 存 JSON 文本）：row_to_dict 统一解析，读取方不用各自 json.loads。
_JSON_FIELDS = ("params_snapshot", "data_versions")


class DuplicateActiveTask(RuntimeError):
    """同用户 × 同指纹的活跃任务已存在（含并发竞态落行失败）。

    ``existing_task_id`` = 既有任务 id——调用方（launcher）据此把本次提交
    **撤回**（不留内存占位、不启动子进程）并复用既有任务回执。
    """

    def __init__(self, existing_task_id: str) -> None:
        super().__init__(f"active task with same fingerprint exists: {existing_task_id}")
        self.existing_task_id = existing_task_id


def _is_dedupe_conflict(exc: IntegrityError) -> bool:
    """IntegrityError 是否撞的**去重索引**（其它唯一冲突不在此列，要原样抛）。"""
    constraint = getattr(getattr(exc, "orig", None), "constraint_name", None)
    if constraint:
        return constraint == DEDUPE_INDEX
    return DEDUPE_INDEX in str(exc)


def parse_json_field(value: Any) -> Any:
    """TEXT 里的 JSON 文本 → 对象；NULL → None；坏文本**原样返回**（不假装没有）。

    坏文本只可能来自手改库（写入端恒为 json.dumps）；原样保留让界面/排查能
    看见坏在哪，静默吞成 None 会把它伪装成「这个任务没有快照」。
    """
    if value is None or not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        logger.warning("任务行 JSON 列解析失败（原样返回）：%.80s", value)
        return value


def clamp_direction(value: str | None) -> str:
    """方向文本入库前的归一：None/空白 → ""；超长截断（防超长文档草稿撑爆行）。"""
    text_value = (value or "").strip()
    return text_value[:MAX_DIRECTION_CHARS]


def resolve_history_filters(
    *,
    market: str | None,
    status: str | None,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    """列表过滤参数的唯一解析点：空白=不过滤、limit/offset 收敛、状态白名单。

    未知状态抛 ``ValueError`` —— 静默查空的代价是「挖了但没显示」无从定位。
    """
    clean_market = (market or "").strip() or None
    clean_status = (status or "").strip() or None
    if clean_status is not None and clean_status not in TASK_STATUSES:
        raise ValueError(
            f"unknown task status: {clean_status!r}; expected one of {TASK_STATUSES}"
        )

    return {
        "market": clean_market,
        "status": clean_status,
        "limit": max(1, min(int(limit), MAX_HISTORY_LIMIT)),
        "offset": max(0, int(offset)),
    }


def _history_where(
    user_id: str, filters: Mapping[str, Any]
) -> tuple[list[str], dict[str, Any]]:
    """历史列表/计数的共用 WHERE（过滤参数须先过 resolve_history_filters）。"""
    where = ["user_id = :user_id"]
    params: dict[str, Any] = {"user_id": user_id}
    if filters["market"] is not None:
        where.append("market = :market")
        params["market"] = filters["market"]
    if filters["status"] is not None:
        where.append("status = :status")
        params["status"] = filters["status"]
    return where, params


def split_datasets(value: Any) -> list[str] | None:
    """``datasets`` 列（逗号分隔 TEXT）→ 列表；空/None → None。

    与 ``QM_MINING_DATASETS`` env 同一种编码（写入端 ``normalize_mining_config``
    产出的登记序元组 join）。接口层返回列表、DB 存 TEXT：历史页与任务详情
    的字段形态一致（都可能是 list|None），前端不用分辨两种形状。
    """
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        return list(value) or None
    parts = [p.strip() for p in str(value).split(",") if p.strip()]
    return parts or None


def row_to_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    """DB 行 → API 字典：三列时间统一 ISO-8601 UTC（带 Z）。

    未知列原样透传（前向兼容）；缺列不补——接口对「没有的值」必须诚实。
    ``datasets`` 由逗号分隔 TEXT 归一为列表（见 :func:`split_datasets`）；
    ``params_snapshot``/``data_versions`` 由 JSON 文本解析为对象（坏文本原样
    保留，见 :func:`parse_json_field`）。
    """
    out = dict(row)
    for field in _TS_FIELDS:
        if field in out:
            out[field] = to_utc_iso(out[field])
    if "datasets" in out:
        out["datasets"] = split_datasets(out["datasets"])
    for field in _JSON_FIELDS:
        if field in out:
            out[field] = parse_json_field(out[field])
    return redact_data(out)


class MiningTaskStore:
    """``rd_agent_mining_tasks`` 的读写。所有写入失败都由调用方吞掉（不拦挖掘主链）。"""

    async def ensure_tables(self) -> None:
        """建表 + 索引 + 老库补列（幂等；engine 启动期调用）。

        ``CREATE TABLE IF NOT EXISTS`` 不会给已存在的表补列——老库（列进
        CREATE 语句之前建的）缺 ``direction_mode``/``direction_meta``/
        ``quality_gate_mode``/``mining_*`` 时，create_task 的 INSERT 引用该列
        会整行失败（记录层失败只告警 → 表现为「历史页凭空缺任务」）。补列走
        ``ADD COLUMN IF NOT EXISTS``（同 doc_store 先例）。
        """
        async with get_session() as session:
            for stmt in [s.strip() for s in _CREATE_TABLE_SQL.split(";") if s.strip()]:
                await session.execute(text(stmt))
            for column in (
                "direction_mode",
                "direction_meta",
                "quality_gate_mode",
                "mining_start",
                "mining_end",
                "valid_start",
                "valid_end",
                "datasets",
                "freq",
                "algorithm",
                # P3：去重指纹 / 算法版本 / 参数快照 / 数据版本快照
                "fingerprint",
                "algorithm_version",
                "params_snapshot",
                "data_versions",
            ):
                await session.execute(
                    text(
                        "ALTER TABLE rd_agent_mining_tasks "
                        f"ADD COLUMN IF NOT EXISTS {column} TEXT"
                    )
                )
            await session.execute(
                text(
                    "ALTER TABLE rd_agent_mining_tasks "
                    "ADD COLUMN IF NOT EXISTS owner_id TEXT"
                )
            )
            await session.execute(
                text(
                    "ALTER TABLE rd_agent_mining_tasks "
                    "ADD COLUMN IF NOT EXISTS lease_expires_at TIMESTAMPTZ"
                )
            )
            await session.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_rd_mining_tasks_lease "
                    "ON rd_agent_mining_tasks (lease_expires_at) "
                    "WHERE status IN ('pending', 'queued', 'running')"
                )
            )
            # 部分唯一索引（老库补建）：老行 fingerprint 全 NULL，不参与谓词，
            # 建索引不会与存量数据冲突。
            await session.execute(
                text(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS {DEDUPE_INDEX} "
                    "ON rd_agent_mining_tasks (user_id, fingerprint) "
                    "WHERE fingerprint IS NOT NULL "
                    f"AND status IN {ACTIVE_STATUSES}"
                )
            )
        logger.info("rd_agent_mining_tasks table ensured")

    async def create_task(
        self,
        *,
        task_id: str,
        user_id: str,
        status: str = "pending",
        market: str = "a_share",
        universe: str = "",
        data_source: str = "",
        direction: str = "",
        direction_mode: str | None = None,
        direction_meta: str | None = None,
        quality_gate_mode: str | None = None,
        mining_start: str = "",
        mining_end: str = "",
        valid_start: str = "",
        valid_end: str = "",
        datasets: str = "",
        freq: str = "",
        algorithm: str = "",
        loop_n: int = 5,
        source: str = "text",
        doc_id: str | None = None,
        fingerprint: str = "",
        algorithm_version: str = "",
        params_snapshot: str = "",
        data_versions: str = "",
    ) -> None:
        """落任务行。task_id 撞键是无操作（重放/重试不该把首次写入覆盖掉）。

        ``status`` 只收初始态（pending/queued）——校验发生在开 session 之前
        （无库环境里也要红/绿分明）。``direction_meta`` 是加权抽样（T-MV-03）
        的复现凭证（JSON 文本），没抽样就是 NULL。``quality_gate_mode``
        （T-MV-05）存**请求意图**：显式 off/soft/hard 才落值，未指定 =
        NULL（入池时由 ``resolve_admission_gate_mode`` 再叠 env 决定生效
        模式——任务行不冻结当时的 env）。``freq`` 同理：显式 ``1min`` 才落值，
        未指定（日线）= NULL。``algorithm`` 由 launcher 传**已解析的 key**
        （不是请求原文）：任务行记录"实际跑了哪个算法"；NULL 只会出现在
        迁移前的老行（当时唯一算法 = rdagent），读取方按此解释。

        **去重（P3）**：``fingerprint`` 非空且撞上「同用户×同指纹的活跃任务」
        的部分唯一索引时，抛 :class:`DuplicateActiveTask`（带既有任务 id）——
        这不是写失败，是**并发竞态下的正确拒绝**，调用方必须撤回本次提交。
        其它写入异常原样上抛（调用方照旧只告警，不拦挖掘主链）。
        """
        if status not in _INITIAL_STATUSES:
            raise ValueError(
                f"create_task 只收初始状态 {_INITIAL_STATUSES}，收到 {status!r}；"
                "running 由心跳/对账到来，终态请走 mark_terminal"
            )
        now = utc_now()
        params = {
            "task_id": task_id,
            "user_id": user_id,
            "status": status,
            "market": market or "a_share",
            "universe": universe or "",
            "data_source": data_source or "",
            "direction": clamp_direction(direction),
            "direction_mode": direction_mode,
            "direction_meta": direction_meta,
            "quality_gate_mode": quality_gate_mode or None,
            # 自定义挖掘配置（P1）：未指定一律 NULL（"没有的值"必须诚实，
            # 前端据此区分「默认窗口」与「显式等于默认的窗口」）
            "mining_start": mining_start or None,
            "mining_end": mining_end or None,
            "valid_start": valid_start or None,
            "valid_end": valid_end or None,
            "datasets": datasets or None,
            # 未指定（日线）→ NULL；显式 1min 才落值
            "freq": freq or None,
            # launcher 传已解析的算法 key（老行 NULL = 迁移前 rdagent）
            "algorithm": algorithm or None,
            "loop_n": int(loop_n),
            "source": source or "text",
            "doc_id": doc_id,
            # P3：空串 → NULL（老行语义一致：没有的值不伪装）
            "fingerprint": fingerprint or None,
            "algorithm_version": algorithm_version or None,
            "params_snapshot": params_snapshot or None,
            "data_versions": data_versions or None,
            "now": now,
            "owner_id": TASK_OWNER_ID,
            "lease_expires_at": now + timedelta(seconds=TASK_LEASE_SECONDS),
        }
        async with get_session() as session:
            try:
                await session.execute(
                    text("""
                        INSERT INTO rd_agent_mining_tasks
                          (task_id, user_id, market, universe, data_source, direction,
                           direction_mode, direction_meta, quality_gate_mode,
                           mining_start, mining_end, valid_start, valid_end, datasets,
                           freq, algorithm, loop_n, source, doc_id, status,
                           fingerprint, algorithm_version, params_snapshot, data_versions,
                           progress_pct, current_loop, created_at, updated_at,
                           owner_id, lease_expires_at)
                        VALUES
                          (:task_id, :user_id, :market, :universe, :data_source, :direction,
                           :direction_mode, :direction_meta, :quality_gate_mode,
                           :mining_start, :mining_end, :valid_start, :valid_end, :datasets,
                           :freq, :algorithm, :loop_n, :source, :doc_id, :status,
                           :fingerprint, :algorithm_version, :params_snapshot, :data_versions,
                           0, 0, :now, :now, :owner_id, :lease_expires_at)
                        ON CONFLICT (task_id) DO NOTHING
                        """),
                    params,
                )
            except IntegrityError as exc:
                if not _is_dedupe_conflict(exc):
                    raise
                existing = await self._find_active_by_fingerprint(
                    session, user_id=user_id, fingerprint=fingerprint
                )
                if existing is None:
                    # 撞了指纹索引却查不到活跃行：唯一可能是在本事务间隙里
                    # 既有任务恰好收终态（索引项消失前的边界）——如实上抛，
                    # 由调用方按普通写失败处理（只告警），不假装去重成功。
                    raise
                raise DuplicateActiveTask(existing) from exc

    @staticmethod
    async def _find_active_by_fingerprint(
        session: Any, *, user_id: str, fingerprint: str
    ) -> str | None:
        """活跃（pending/queued/running）同指纹任务 id；无则 None。"""
        result = await session.execute(
            text(
                "SELECT task_id FROM rd_agent_mining_tasks "
                "WHERE user_id = :user_id AND fingerprint = :fingerprint "
                f"AND status IN {ACTIVE_STATUSES} "
                "ORDER BY created_at DESC LIMIT 1"
            ),
            {"user_id": user_id, "fingerprint": fingerprint},
        )
        row = result.fetchone()
        return row[0] if row else None

    async def find_active_by_fingerprint(
        self, *, user_id: str, fingerprint: str
    ) -> str | None:
        """公开版查重（launcher 提交前的第一道闸）；查库失败上抛，调用方吞。"""
        if not fingerprint:
            return None
        async with get_session() as session:
            return await self._find_active_by_fingerprint(
                session, user_id=user_id, fingerprint=fingerprint
            )

    async def update_progress(
        self,
        task_id: str,
        *,
        status: str = "running",
        progress_pct: int,
        current_loop: int,
    ) -> None:
        """进度心跳。只收 pending/running——终态必须走 :meth:`mark_terminal`。"""
        if status not in _PROGRESS_STATUSES:
            raise ValueError(
                f"update_progress 只收 {_PROGRESS_STATUSES}，收到 {status!r}；"
                "终态请走 mark_terminal（它维护 completed_at）"
            )
        now = utc_now()
        async with get_session() as session:
            result = await session.execute(
                text("""
                    UPDATE rd_agent_mining_tasks
                    SET status = :status, progress_pct = :pct, current_loop = :loop,
                        updated_at = :now, lease_expires_at = :lease_expires_at
                    WHERE task_id = :task_id AND owner_id = :owner_id
                      AND status IN ('pending', 'queued', 'running')
                    RETURNING task_id
                    """),
                {
                    "status": status,
                    "pct": int(progress_pct),
                    "loop": int(current_loop),
                    "now": now,
                    "lease_expires_at": now + timedelta(seconds=TASK_LEASE_SECONDS),
                    "owner_id": TASK_OWNER_ID,
                    "task_id": task_id,
                },
            )
            if result.fetchone() is None:
                row = (
                    await session.execute(
                        text(
                            "SELECT owner_id, status FROM rd_agent_mining_tasks "
                            "WHERE task_id = :task_id"
                        ),
                        {"task_id": task_id},
                    )
                ).first()
                if row is not None and (
                    row[0] != TASK_OWNER_ID or row[1] not in ACTIVE_STATUSES
                ):
                    raise TaskLeaseLostError(
                        f"Mining task {task_id} is no longer owned by this process"
                    )

    async def mark_terminal(
        self,
        task_id: str,
        *,
        status: str,
        error: str | None = None,
        factor_count: int | None = None,
    ) -> None:
        """写终态：同时补 ``completed_at``（error 只在失败/取消时给）。"""
        if status not in TERMINAL_STATUSES:
            raise ValueError(
                f"mark_terminal 只收终态 {TERMINAL_STATUSES}，收到 {status!r}"
            )

        now = utc_now()
        fields: dict[str, Any] = {
            "status": status,
            "error": error,
            "now": now,
            "task_id": task_id,
            "owner_id": TASK_OWNER_ID,
        }
        set_clause = (
            "status = :status, error = :error, completed_at = :now, updated_at = :now, "
            "lease_expires_at = NULL"
        )
        if factor_count is not None:
            set_clause += ", factor_count = :factor_count"
            fields["factor_count"] = int(factor_count)

        async with get_session() as session:
            await session.execute(
                text(
                    f"UPDATE rd_agent_mining_tasks SET {set_clause} "
                    "WHERE task_id = :task_id AND owner_id = :owner_id "
                    "AND status IN ('pending', 'queued', 'running')"
                ),
                fields,
            )

    async def renew_queued_leases(self) -> int:
        """Keep this process's queued and startup-pending tasks alive."""
        now = utc_now()
        async with get_session() as session:
            result = await session.execute(
                text(
                    "UPDATE rd_agent_mining_tasks "
                    "SET updated_at = :now, lease_expires_at = :lease_expires_at "
                    "WHERE owner_id = :owner_id AND status IN ('queued', 'pending')"
                ),
                {
                    "now": now,
                    "lease_expires_at": now + timedelta(seconds=TASK_LEASE_SECONDS),
                    "owner_id": TASK_OWNER_ID,
                },
            )
            return int(result.rowcount or 0)

    async def claim_expired_queued_leases(self, task_ids: list[str]) -> list[str]:
        """Atomically adopt persisted queue entries whose previous owner expired."""
        if not task_ids:
            return []
        now = utc_now()
        async with get_session() as session:
            result = await session.execute(
                text(
                    "UPDATE rd_agent_mining_tasks "
                    "SET owner_id = :owner_id, updated_at = :now, "
                    "    lease_expires_at = :lease_expires_at "
                    "WHERE task_id = ANY(CAST(:task_ids AS text[])) "
                    "AND status = 'queued' "
                    "AND (lease_expires_at < :now "
                    "     OR (lease_expires_at IS NULL "
                    "         AND updated_at < :legacy_stale_before)) "
                    "RETURNING task_id"
                ),
                {
                    "task_ids": task_ids,
                    "owner_id": TASK_OWNER_ID,
                    "now": now,
                    "lease_expires_at": now + timedelta(seconds=TASK_LEASE_SECONDS),
                    "legacy_stale_before": now - timedelta(seconds=TASK_LEASE_SECONDS),
                },
            )
            return [str(row[0]) for row in result.fetchall()]

    async def claim_queued_task_for_start(self, task_id: str) -> bool | None:
        """Fence queue startup by atomically changing this owner's row to pending."""
        now = utc_now()
        async with get_session() as session:
            result = await session.execute(
                text(
                    "UPDATE rd_agent_mining_tasks "
                    "SET status = 'pending', updated_at = :now, "
                    "    lease_expires_at = :lease_expires_at "
                    "WHERE task_id = :task_id AND owner_id = :owner_id "
                    "AND status = 'queued' "
                    "RETURNING task_id"
                ),
                {
                    "task_id": task_id,
                    "owner_id": TASK_OWNER_ID,
                    "now": now,
                    "lease_expires_at": now + timedelta(seconds=TASK_LEASE_SECONDS),
                },
            )
            if result.fetchone() is not None:
                return True
            existing = await session.execute(
                text("SELECT 1 FROM rd_agent_mining_tasks WHERE task_id = :task_id"),
                {"task_id": task_id},
            )
            # None means persistence had already failed during task creation; retain
            # the launcher's documented best-effort DB semantics in that case.
            return False if existing.fetchone() is not None else None

    async def count_factors(self, task_id: str) -> int:
        """该任务已落库的因子数（``metadata_json->>'task_id'`` 口径，与 /factors 端点一致）。"""
        async with get_session(read_only=True) as session:
            return int(
                (
                    await session.execute(
                        text(
                            "SELECT count(*) FROM rd_agent_factors "
                            "WHERE metadata_json->>'task_id' = :task_id"
                        ),
                        {"task_id": task_id},
                    )
                ).scalar_one()
            )

    async def get_task(
        self, task_id: str, *, user_id: str | None = None
    ) -> dict[str, Any] | None:
        """按 id 取行；给 user_id 时按归属收口（查空 = 不存在或不属于该用户）。"""
        query = "SELECT * FROM rd_agent_mining_tasks WHERE task_id = :task_id"
        params: dict[str, Any] = {"task_id": task_id}
        if user_id is not None:
            query += " AND user_id = :user_id"
            params["user_id"] = user_id

        async with get_session(read_only=True) as session:
            row = (await session.execute(text(query), params)).mappings().first()
        return row_to_dict(row) if row else None

    async def list_history(
        self,
        *,
        user_id: str,
        market: str | None = None,
        status: str | None = None,
        limit: int = DEFAULT_HISTORY_LIMIT,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        """用户历史（倒序 + 分页）。过滤参数先经 :func:`resolve_history_filters`。"""
        filters = resolve_history_filters(
            market=market, status=status, limit=limit, offset=offset
        )

        where, params = _history_where(user_id, filters)
        params["limit"] = filters["limit"]
        params["offset"] = filters["offset"]

        query = (
            "SELECT * FROM rd_agent_mining_tasks "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY created_at DESC, task_id DESC "
            "LIMIT :limit OFFSET :offset"
        )
        async with get_session(read_only=True) as session:
            rows = (await session.execute(text(query), params)).mappings().all()
        return [row_to_dict(r) for r in rows]

    async def count_history(
        self,
        *,
        user_id: str,
        market: str | None = None,
        status: str | None = None,
    ) -> int:
        """同过滤条件下的全量行数（分页「共 N 条」）。

        必须是真的 COUNT：拿本页行数当 total，分页器永远显示 ≤ limit 条。
        """
        filters = resolve_history_filters(
            market=market, status=status, limit=1, offset=0
        )
        where, params = _history_where(user_id, filters)
        query = (
            f"SELECT count(*) FROM rd_agent_mining_tasks WHERE {' AND '.join(where)}"
        )
        async with get_session(read_only=True) as session:
            return int((await session.execute(text(query), params)).scalar_one())

    async def count_tasks_by_docs(
        self, *, user_id: str, doc_ids: list[str]
    ) -> dict[str, int]:
        """按文档批量计任务数（文档列表「N 个方向」徽标），**只回非零项**。

        空入参零 SQL 直返：空文档列表不该为一次没有内容的聚合伙付查询往返。
        没出现的 doc_id 语义 = 0 条任务（真零，不是「查不到」）——调用方
        只在查询本身失败时才有「未知」态，两者绝不可混。
        """
        ids = [d for d in doc_ids if d]
        if not ids:
            return {}
        async with get_session(read_only=True) as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT doc_id, count(*) AS n FROM rd_agent_mining_tasks "
                            "WHERE user_id = :user_id AND doc_id = ANY(:doc_ids) "
                            "GROUP BY doc_id"
                        ),
                        {"user_id": user_id, "doc_ids": ids},
                    )
                )
                .mappings()
                .all()
            )
        return {str(r["doc_id"]): int(r["n"]) for r in rows}

    async def count_by_direction(
        self, *, user_id: str, market: str, directions: list[str]
    ) -> dict[str, int]:
        """按方向批量计挖掘史次数（T-MV-03 空白度权重的数据源），**只回非零项**。

        口径：
        - **全状态计数**——failed/cancelled 也是「挖过」，方向的新鲜度由
          尝试次数决定，不是成败；
        - 按 **user × market** 收口——方向词表是市场级的（动量类因子 在 A股
          挖过不代表港股挖过），跨市场不互相抵扣；按用户隔离同
          :meth:`count_tasks_by_docs`（别人的史不参与我的抽样分布）；
        - 没出现的方向语义 = 0 次（真零，不是「查不到」）；空入参零 SQL 直返。
        """
        ids = [d for d in directions if d]
        if not ids:
            return {}
        async with get_session(read_only=True) as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT direction, count(*) AS n FROM rd_agent_mining_tasks "
                            "WHERE user_id = :user_id AND market = :market "
                            "AND direction = ANY(:directions) "
                            "GROUP BY direction"
                        ),
                        {"user_id": user_id, "market": market, "directions": ids},
                    )
                )
                .mappings()
                .all()
            )
        return {str(r["direction"]): int(r["n"]) for r in rows}

    async def list_by_doc(
        self, *, user_id: str, doc_id: str, limit: int = DOC_TASKS_LIMIT
    ) -> list[dict[str, Any]]:
        """某文档关联的挖掘任务（最近优先）——一文档多方向回看的明细面。

        只选展示四列（task_id/status/direction/created_at）：direction 单条
        可达两万字，整行透出会把详情响应撑大一个量级；市场/池等字段任务中心
        页面已有，本处不重复搬运。
        """
        capped = max(1, min(int(limit), MAX_HISTORY_LIMIT))
        async with get_session(read_only=True) as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT task_id, status, direction, created_at "
                            "FROM rd_agent_mining_tasks "
                            "WHERE user_id = :user_id AND doc_id = :doc_id "
                            "ORDER BY created_at DESC, task_id DESC "
                            "LIMIT :limit"
                        ),
                        {"user_id": user_id, "doc_id": doc_id, "limit": capped},
                    )
                )
                .mappings()
                .all()
            )
        return [row_to_dict(r) for r in rows]

    async def reconcile_orphans(self, *, user_id: str | None = None) -> int:
        """回收租约过期的孤儿行；终态行不碰。"""
        now = utc_now()
        lease_expired = now
        query = (
            "UPDATE rd_agent_mining_tasks "
            "SET status = 'failed', "
            "    error = COALESCE(error, 'Task owner lease expired'), "
            "    completed_at = :now, updated_at = :now, lease_expires_at = NULL "
            "WHERE status IN ('pending', 'queued', 'running') "
            "AND (lease_expires_at < :lease_expired "
            "     OR (lease_expires_at IS NULL AND updated_at < :legacy_stale_before))"
        )
        params: dict[str, Any] = {
            "now": now,
            "lease_expired": lease_expired,
            "legacy_stale_before": now - timedelta(seconds=TASK_LEASE_SECONDS),
        }
        if user_id is not None:
            query += " AND user_id = :user_id"
            params["user_id"] = user_id

        async with get_session() as session:
            result = await session.execute(text(query), params)
            flipped = int(result.rowcount or 0)
        if flipped:
            logger.warning(
                "reconcile_orphans flipped %d orphan mining task(s) to failed", flipped
            )
        return flipped


_store: MiningTaskStore | None = None


def get_mining_task_store() -> MiningTaskStore:
    global _store
    if _store is None:
        _store = MiningTaskStore()
    return _store
