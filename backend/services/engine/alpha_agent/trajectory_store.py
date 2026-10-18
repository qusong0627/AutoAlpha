"""T-FM-07 决策轨迹与低奖励定位 —— 只读归档层（无持久化、无写侧）。

数据源（全部既有事实，只读）：

1. 任务日志工作区 ``<log_dir>/<task_id>/Loop_N/``（``launcher.DEFAULT_LOG_DIR``）——
   五环节目录：``direct_exp_gen/{hypothesis generation, experiment generation}``、
   ``coding/coder result``、``running``、``feedback``；存在性 / pkl 数 / 时间 →
   节点状态与耗时（time_info dict 优先，缺则文件 mtime 起止）。
2. ``rd_agent_factors``（``metadata_json->>'task_id'``）—— 因子名 → factor_id →
   ``rd_mined`` 物化清单（``_materialize_manifest.json``）→ 终态结局 + 拒因。
3. ``rd_agent_mining_tasks`` —— 任务级方向/状态（``partial`` 与「任务失败」旗标）。

组装纪律（细案 §4 D1-D5）：

- **绝不猜测**：pkl 归因失败（unpickle 报错 / 结构不认识）→ 整任务降级为
  ``attribution="task_level"``（逐轮因子清单置空，因子挂在任务级由 DB 行给出），
  结构骨架（节点/耗时）照常；不把因子硬分配给某轮。
- **只标位置 + 事实**：低奖励旗标只回答「这个循环发生了什么」（无进入实现的
  因子 / 任务失败止步 / 结局全被拒），不做「因为假设错了」这类因果推断。
- **dict 优先、mtime 兜底**：某环节 time_info 一旦存在 dict，即以其 start/end
  为准（缺 end = 未完成，如实 None）；完全无 dict 才用文件 mtime 包络。
  ``direct_exp_gen`` 一步内含假设 + 实验两个子环节、共用该步 time_info，
  两节点区间相同（同一步的官方计时，不做拆分猜测）。
- **上限即如实**：loops 超过 ``MAX_LOOPS`` 保**尾部**（深轮更近失败点），
  ``truncated=true`` + ``dropped_loops`` 计数；单文件超
  ``shared.safe_pickle.MAX_PICKLE_BYTES`` 或单环节超过
  ``MAX_LOAD_FILES_PER_PHASE`` 个待解析文件 → 按归因失败降级；
  单 glob 模式枚举上限 ``MAX_GLOB_FILES``（病态目录不拖垮请求）。
- **白名单反序列化 + 有界读取**（评审 2026-10-12，security-F1/F4）：pkl 出自
  挖掘子进程工作区（该子进程按设计执行 LLM 生成代码），主进程绝不整权反序列化
  ——机制单源在 ``backend.shared.safe_pickle``（``RestrictedUnpickler`` 按模块根
  白名单拦截，白名单按真样本实测钉死；读取走单次有界读，无 stat→open TOCTOU），
  本模块的 ``default_load_pickle`` 只叠加「告警 + None」降级策略；
  扫描经 ``asyncio.to_thread`` 移出事件循环。全部失败按证据缺失降级，不抛穿。
- ``HypothesisFeedback`` 等 vendor 对象的 ``bool()`` 可能为 False（实测），
  一律以 ``is None`` 判空，禁用真值判断。
- 写侧（局部重写/高奖重组）本批不做——钩子路线见细案 §4 D7。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from collections.abc import Callable

from backend.shared.safe_pickle import PayloadTooLargeError, load_pickle
from backend.shared.utc_datetime import to_utc_iso, utc_now

logger = logging.getLogger(__name__)

#: 五环节（顺序即展示顺序；与 launcher._PHASE_DIR_MAP / ProgressSidebar 词汇同名）
PHASES: tuple[str, ...] = ("hypothesis", "experiment", "coding", "running", "feedback")

#: 环节 → 载荷 pkl glob（相对 Loop 目录；不含 time_info / debug_* / token_cost，
#: 亦不落入 coding/evo_loop_0 的中间调试目录）。running 无独立载荷产物——
#: 执行证据即其 time_info（见 PHASE_TIME_GLOBS），pkl_count 如实为 0。
PHASE_PAYLOAD_GLOBS: dict[str, tuple[str, ...]] = {
    "hypothesis": ("direct_exp_gen/hypothesis generation/**/*.pkl",),
    "experiment": ("direct_exp_gen/experiment generation/**/*.pkl",),
    "coding": ("coding/coder result/**/*.pkl",),
    "running": (),
    "feedback": ("feedback/feedback/**/*.pkl",),
}

#: 环节 → time_info glob（相对 Loop 目录）
PHASE_TIME_GLOBS: dict[str, str] = {
    "hypothesis": "direct_exp_gen/time_info/**/*.pkl",
    "experiment": "direct_exp_gen/time_info/**/*.pkl",
    "coding": "coding/time_info/**/*.pkl",
    "running": "running/time_info/**/*.pkl",
    "feedback": "feedback/time_info/**/*.pkl",
}

#: 无独立载荷产物的环节（running 以 time_info 为执行证据）
_PHASES_WITHOUT_PAYLOAD = frozenset({"running"})

#: 节点状态
STATUS_OK = "ok"
STATUS_PARTIAL = "partial"
STATUS_ABSENT = "absent"

#: 归因模式
ATTRIBUTION_LOOP = "loop"
ATTRIBUTION_TASK_LEVEL = "task_level"

#: 因子结局词表（细案 §4 D3；与 rd_mined 物化清单终态对齐）
OUTCOME_MATERIALIZED = "materialized"
OUTCOME_REJECTED_DUPLICATE = "rejected_duplicate"
OUTCOME_REJECTED_GATE = "rejected_gate"
OUTCOME_SUPERSEDED = "superseded"
OUTCOME_PENDING = "pending"

#: 「全被拒」旗标的拒结局子集（superseded 是顶替不是被拒）
REJECTED_OUTCOMES: tuple[str, ...] = (
    OUTCOME_REJECTED_DUPLICATE,
    OUTCOME_REJECTED_GATE,
)

#: 低奖励定位旗标（只读标注；文案走位置词，禁因果推断）
FLAG_NO_SELECTED = "no_selected"
FLAG_TASK_FAILED_HERE = "task_failed_here"
FLAG_ALL_REJECTED = "all_rejected"
FLAG_TEXTS: dict[str, str] = {
    FLAG_NO_SELECTED: "本循环产出因子均未进入实现",
    FLAG_TASK_FAILED_HERE: "任务失败——本循环为最深到达",
    FLAG_ALL_REJECTED: "本循环产出因子结局均为被拒（重复/门禁）",
}

#: 任务仍活动（数据可能未落全）的状态
PARTIAL_TASK_STATUSES = frozenset({"pending", "running"})

#: 单任务返回循环数上限（保尾部）；超出置 truncated + dropped_loops
MAX_LOOPS = 100
#: 单环节最多解析的文件数（超限按归因失败降级，绝不静默丢名）
MAX_LOAD_FILES_PER_PHASE = 50
#: 单环节最多解析的 time_info 文件数（时间证据，超出不降级）
MAX_TIME_INFO_FILES = 20
#: 单 glob 模式枚举上限（病态目录兜底：截断即告警；解析侧 >50 已先降级）
MAX_GLOB_FILES = 200

#: task_id 形状（launcher 生成；终点防目录穿越的第二道闸）
_SAFE_TASK_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")


# ── 数据模型（frozen；to_dict 即 API 契约）─────────────────────────────


@dataclass(frozen=True)
class PhaseNode:
    """一个环节节点：状态 + 时间 + 载荷 pkl 数。"""

    phase: str
    status: str
    start: datetime | None
    end: datetime | None
    duration_s: float | None
    pkl_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "status": self.status,
            "start": to_utc_iso(self.start),
            "end": to_utc_iso(self.end),
            "duration_s": (
                round(self.duration_s, 3) if self.duration_s is not None else None
            ),
            "pkl_count": self.pkl_count,
        }


@dataclass(frozen=True)
class FactorFate:
    """一个因子的结局：是否进入实现 + 终态结局 + 说明（缺失一律 None）。"""

    name: str
    selected: bool | None
    outcome: str | None
    reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "selected": self.selected,
            "outcome": self.outcome,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class LoopScan:
    """单轮循环的原始扫描结果（IO 产物，未 join DB/清单）。"""

    loop: int
    nodes: tuple[PhaseNode, ...]
    proposed_names: tuple[str, ...]
    coder_names: tuple[str, ...]
    coder_ok: bool
    has_load_error: bool

    def phase(self, phase: str) -> PhaseNode:
        return next(n for n in self.nodes if n.phase == phase)


@dataclass(frozen=True)
class WorkspaceScan:
    """一次工作区扫描：保留的循环 + 截断计数 + 是否出现载荷解析失败。"""

    loops: tuple[LoopScan, ...]
    dropped_loops: int
    total_loops: int
    has_load_error: bool


@dataclass(frozen=True)
class LoopTrajectory:
    """单轮循环的组装结果。"""

    loop: int
    nodes: tuple[PhaseNode, ...]
    factors: tuple[FactorFate, ...]
    flags: tuple[str, ...]

    @property
    def flagged(self) -> bool:
        return bool(self.flags)

    @property
    def flag_reason(self) -> str | None:
        if not self.flags:
            return None
        return "；".join(FLAG_TEXTS.get(f, f) for f in self.flags)

    def to_dict(self) -> dict[str, Any]:
        return {
            "loop": self.loop,
            "nodes": [n.to_dict() for n in self.nodes],
            "factors": [f.to_dict() for f in self.factors],
            "flagged": self.flagged,
            "flags": list(self.flags),
            "flag_reason": self.flag_reason,
        }


@dataclass(frozen=True)
class TaskTrajectory:
    """任务级轨迹载荷（API ``data`` 字段）。"""

    task_id: str
    direction: str | None
    status: str | None
    market: str | None
    partial: bool
    attribution: str
    loops: tuple[LoopTrajectory, ...]
    factors: tuple[FactorFate, ...]
    truncated: bool
    dropped_loops: int
    generated_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "direction": self.direction,
            "status": self.status,
            "market": self.market,
            "partial": bool(self.partial),
            "attribution": self.attribution,
            "loops": [loop.to_dict() for loop in self.loops],
            "factors": [f.to_dict() for f in self.factors],
            "truncated": bool(self.truncated),
            "dropped_loops": int(self.dropped_loops),
            "generated_at": self.generated_at,
        }


# ── pkl 读取（全部 best-effort；失败 = 证据缺失，绝不抛穿）───────────────


def _parse_dt(raw: Any) -> datetime | None:
    """time_info 值 → aware UTC；非法/缺失 → None（不猜时间）。"""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, datetime):
        dt = raw
    else:
        text = str(raw).strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def default_load_pickle(path: Path) -> Any:
    """默认 pkl 读取：受限反序列化（``shared.safe_pickle``）+ 告警降级策略。

    机制（白名单拦截 / 单次有界读取）单源在 ``backend.shared.safe_pickle``
    （评审 2026-10-12，security-F1/F4）；本函数只叠加展示链路的策略：
    读写失败 / 超限 / 白名单拒绝 / 损坏一律 None，调用方按证据缺失处理。
    """
    try:
        return load_pickle(path)
    except OSError as exc:
        logger.warning("[trajectory] pkl 读取失败：%s（%s）", path, exc)
        return None
    except PayloadTooLargeError as exc:
        logger.warning("[trajectory] %s", exc)
        return None
    except Exception as exc:  # noqa: BLE001 —— 归因失败降级，绝不抛穿
        logger.warning("[trajectory] pkl 解析失败（按证据缺失）：%s（%s）", path, exc)
        return None


Loader = Callable[[Path], Any]


def _safe_load(loader: Loader, path: Path) -> Any:
    try:
        return loader(path)
    except Exception as exc:  # noqa: BLE001 —— 注入实现同样不许炸穿扫描
        logger.warning(
            "[trajectory] 注入 loader 抛错（按证据缺失）：%s（%s）", path, exc
        )
        return None


def _factor_name_of(item: Any) -> str | None:
    """从 FactorTask / FactorFBWorkspace 取因子名（鸭子类型，不引 vendor 类）。"""
    name = getattr(item, "factor_name", None)
    if isinstance(name, str) and name.strip():
        return name.strip()
    target = getattr(item, "target_task", None)
    name = getattr(target, "factor_name", None) if target is not None else None
    if isinstance(name, str) and name.strip():
        return name.strip()
    return None


def extract_factor_names(obj: Any) -> tuple[list[str], bool]:
    """提取因子名 → ``(names, ok)``。

    ``ok=False`` = 结构不认识（归因失败候选，绝不猜测）；空列表是合法事实
    （该步提案为空）。兼容 ``list[FactorTask]``（实验环节）与
    ``list[FactorFBWorkspace]``（编码环节，名字在 ``target_task``）。
    """
    if obj is None:
        return [], False
    if isinstance(obj, (list, tuple)):
        items = obj
    else:
        items = [obj]
    names: list[str] = []
    for item in items:
        name = _factor_name_of(item)
        if name is not None and name not in names:
            names.append(name)
    if names:
        return names, True
    if isinstance(obj, (list, tuple)) and len(obj) == 0:
        return [], True
    return [], False


def _mtime_bounds(files: Sequence[Path]) -> tuple[datetime | None, datetime | None]:
    times: list[datetime] = []
    for f in files:
        try:
            times.append(datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc))
        except OSError:
            continue
    if not times:
        return None, None
    return min(times), max(times)


def _dict_bounds(
    files: Sequence[Path], loader: Loader
) -> tuple[datetime | None, datetime | None, bool]:
    """time_info dict 起止：``(start, end, has_dict)``（多文件取最早/最晚）。"""
    starts: list[datetime] = []
    ends: list[datetime] = []
    has_dict = False
    for path in files[:MAX_TIME_INFO_FILES]:
        data = _safe_load(loader, path)
        if not isinstance(data, Mapping):
            continue
        start = _parse_dt(data.get("start_time"))
        end = _parse_dt(data.get("end_time"))
        if start is not None or end is not None:
            has_dict = True
        if start is not None:
            starts.append(start)
        if end is not None:
            ends.append(end)
    return (min(starts) if starts else None, max(ends) if ends else None, has_dict)


def _glob_files(base: Path, pattern: str, *, limit: int = MAX_GLOB_FILES) -> list[Path]:
    """glob 文件枚举（排序输出）；单模式至多 ``limit`` 个（病态目录兜底）。

    枚举上限先于排序生效（OS 序截断），命中且确有余量才告警；解析侧
    ``MAX_LOAD_FILES_PER_PHASE`` 更小的闸先行降级。
    """
    try:
        found: list[Path] = []
        stream = base.glob(pattern)
        for path in stream:
            if not path.is_file():
                continue
            found.append(path)
            if len(found) >= limit:
                if any(more.is_file() for more in stream):
                    logger.warning(
                        "[trajectory] glob 命中枚举上限 %d，截断：%s/%s",
                        limit,
                        base,
                        pattern,
                    )
                break
        found.sort()
        return found
    except OSError as exc:
        logger.warning("[trajectory] glob 失败：%s/%s（%s）", base, pattern, exc)
        return []


def _scan_phase(loop_dir: Path, phase: str, loader: Loader) -> PhaseNode:
    """扫描单环节 → 节点（状态/时间/pkl 数）。"""
    payload_files: list[Path] = []
    for pattern in PHASE_PAYLOAD_GLOBS[phase]:
        payload_files.extend(_glob_files(loop_dir, pattern))
    time_files = _glob_files(loop_dir, PHASE_TIME_GLOBS[phase])

    dict_start, dict_end, has_dict = _dict_bounds(time_files, loader)
    if has_dict:
        start, end = dict_start, dict_end
    else:
        start, end = _mtime_bounds([*payload_files, *time_files])
    duration = (
        (end - start).total_seconds() if start is not None and end is not None else None
    )

    has_payload = bool(payload_files)
    has_evidence = has_payload or bool(time_files)
    if not has_evidence:
        status = STATUS_ABSENT
    else:
        needs_payload = phase not in _PHASES_WITHOUT_PAYLOAD
        status = (
            STATUS_OK
            if end is not None and (has_payload or not needs_payload)
            else STATUS_PARTIAL
        )
    return PhaseNode(
        phase=phase,
        status=status,
        start=start,
        end=end,
        duration_s=duration,
        pkl_count=len(payload_files),
    )


def _iter_loop_dirs(log_dir: Path) -> list[tuple[int, Path]]:
    try:
        entries = [p for p in log_dir.glob("Loop_*") if p.is_dir()]
    except OSError as exc:
        logger.warning("[trajectory] 循环目录扫描失败：%s（%s）", log_dir, exc)
        return []
    found: list[tuple[int, Path]] = []
    for p in entries:
        suffix = p.name[len("Loop_") :]
        if suffix.isdigit():
            found.append((int(suffix), p))
    found.sort(key=lambda item: item[0])
    return found


def _scan_loop(loop_idx: int, loop_dir: Path, loader: Loader) -> LoopScan:
    nodes = tuple(_scan_phase(loop_dir, phase, loader) for phase in PHASES)

    def _names(phase: str) -> tuple[list[str], bool]:
        files: list[Path] = []
        for pattern in PHASE_PAYLOAD_GLOBS[phase]:
            files.extend(_glob_files(loop_dir, pattern))
        if len(files) > MAX_LOAD_FILES_PER_PHASE:
            # 超过解析上限：宁可整体降级（task_level），不静默丢名
            logger.warning(
                "[trajectory] Loop_%d/%s pkl 数 %d 超解析上限 %d，按归因失败降级",
                loop_idx,
                phase,
                len(files),
                MAX_LOAD_FILES_PER_PHASE,
            )
            return [], False
        names: list[str] = []
        ok = True
        for path in files:
            obj = _safe_load(loader, path)
            got, item_ok = extract_factor_names(obj)
            if not item_ok:
                ok = False
                continue
            for name in got:
                if name not in names:
                    names.append(name)
        return names, ok

    proposed, proposed_ok = _names("experiment")
    coder, coder_ok = _names("coding")
    return LoopScan(
        loop=loop_idx,
        nodes=nodes,
        proposed_names=tuple(proposed),
        coder_names=tuple(coder),
        coder_ok=coder_ok,
        has_load_error=not proposed_ok or not coder_ok,
    )


def scan_workspace(
    log_dir: Path | str,
    *,
    max_loops: int = MAX_LOOPS,
    loader: Loader | None = None,
) -> WorkspaceScan:
    """扫描任务工作区 → :class:`WorkspaceScan`（IO 层，纯只读）。

    loops 超 ``max_loops`` 时保**尾部**（深轮更近失败点），丢弃数进
    ``dropped_loops``；任一环节载荷解析失败 → ``has_load_error=True``。
    """
    load = loader or default_load_pickle
    base = Path(log_dir)
    found = _iter_loop_dirs(base)
    total = len(found)
    kept = found[-max_loops:] if max_loops > 0 else []
    loops: list[LoopScan] = []
    for loop_idx, loop_dir in kept:
        try:
            loops.append(_scan_loop(loop_idx, loop_dir, load))
        except Exception as exc:  # noqa: BLE001 —— 单轮异常不拖垮整任务
            logger.warning(
                "[trajectory] Loop_%d 扫描异常（跳过该轮）：%s", loop_idx, exc
            )
    return WorkspaceScan(
        loops=tuple(loops),
        dropped_loops=total - len(kept),
        total_loops=total,
        has_load_error=any(loop.has_load_error for loop in loops),
    )


# ── 结局 join（DB 行 + 物化清单）────────────────────────────────────────


def _load_materialize_manifest() -> dict[str, Any]:
    """rd_mined 物化清单（读失败一律空清单——轨迹是只读展示，不拦任何主链）。"""
    try:
        from backend.scripts.rd_mined_materialize import _lib_root, _load_manifest

        return _load_manifest(_lib_root())
    except Exception as exc:  # noqa: BLE001
        logger.warning("[trajectory] 物化清单读取失败（按空清单）: %s", exc)
        return {}


async def load_task_factors(*, user_id: str, task_id: str) -> list[dict[str, Any]]:
    """任务因子行（按创建序）。

    归属口径：``metadata_json->>'task_id'`` 收口；``user_id`` 历史空值
    （该列加入前写入）照 :func:`_require_owned_factor` 先例放行只读。
    """
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    async with get_session(read_only=True) as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT factor_id, factor_name, status, created_at "
                        "FROM rd_agent_factors "
                        "WHERE metadata_json->>'task_id' = :task_id "
                        "AND (user_id = :user_id OR user_id IS NULL OR user_id = '') "
                        "ORDER BY created_at ASC NULLS FIRST, factor_id ASC"
                    ),
                    {"task_id": str(task_id), "user_id": str(user_id)},
                )
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


def _fmt_corr(value: Any) -> str | None:
    try:
        return f"{float(value):.3f}"
    except (TypeError, ValueError):
        return None


def _duplicate_reason(entry: Mapping[str, Any]) -> str | None:
    against = str(entry.get("corr_against") or "").strip()
    corr = _fmt_corr(entry.get("corr"))
    if against and corr:
        return f"与已有列 {against} 值级重复（corr={corr}）"
    if against:
        return f"与已有列 {against} 值级重复"
    return "值级重复（库内已有高度相关列）"


def _gate_reason(entry: Mapping[str, Any]) -> str | None:
    """门禁拒因：只取 hard+fail 的判定（soft/skipped/pass 不是拒因）。"""
    gates = (entry.get("gates") or {}).get("gates") or []
    messages: list[str] = []
    for gate in gates:
        if not isinstance(gate, Mapping):
            continue
        if str(gate.get("status") or "") != "fail":
            continue
        if str(gate.get("mode") or "") != "hard":
            continue
        text = str(gate.get("message") or gate.get("label") or gate.get("key") or "")
        if text and text not in messages:
            messages.append(text)
    return "；".join(messages) if messages else None


def _superseded_reason(entry: Mapping[str, Any]) -> str | None:
    by = str(entry.get("superseded_by") or "").strip()
    return f"已被 {by} 顶替" if by else "已被新版本顶替"


def _outcome_of(
    factor_id: Any, row_status: Any, manifest: Mapping[str, Any]
) -> tuple[str | None, str | None]:
    """因子终态 → ``(outcome, reason)``；认不出的一律 None（缺失不猜）。"""
    fid = str(factor_id) if factor_id else ""
    entry = manifest.get(fid) if fid else None
    if isinstance(entry, Mapping):
        status = str(entry.get("status") or "")
        if status == OUTCOME_SUPERSEDED or entry.get("superseded_by"):
            return OUTCOME_SUPERSEDED, _superseded_reason(entry)
        if status == OUTCOME_MATERIALIZED:
            return OUTCOME_MATERIALIZED, None
        if status == OUTCOME_REJECTED_DUPLICATE:
            return OUTCOME_REJECTED_DUPLICATE, _duplicate_reason(entry)
        if status == OUTCOME_REJECTED_GATE:
            return OUTCOME_REJECTED_GATE, _gate_reason(entry)
        return None, (f"清单状态：{status}" if status else None)
    status = str(row_status or "").strip()
    if status == OUTCOME_PENDING:
        return OUTCOME_PENDING, None
    if status == "completed":
        return None, "因子已完成（未提交物化）"
    if status == "failed":
        return None, "因子计算失败"
    return None, (f"因子状态：{status}" if status else None)


def _pick_row(
    rows: Sequence[Mapping[str, Any]], manifest: Mapping[str, Any]
) -> Mapping[str, Any]:
    """同名多行时的确定性选择：优先有物化清单条目的行，否则取最新一行。"""
    for row in rows:
        fid = str(row.get("factor_id") or "")
        if fid and fid in manifest:
            return row
    return rows[-1]


# ── 组装（纯函数）──────────────────────────────────────────────────────


def build_trajectory(
    *,
    task: Mapping[str, Any],
    scan: WorkspaceScan,
    db_rows: Sequence[Mapping[str, Any]] = (),
    manifest: Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> TaskTrajectory:
    """扫描结果 + DB 行 + 物化清单 → 任务轨迹（纯函数，不改任何入参）。

    - ``attribution="loop"``：逐轮因子取自该轮 experiment 归因；
      ``selected`` = 因子名出现在该轮 coder result 清单（进入实现）。
    - ``attribution="task_level"``：任一载荷解析失败 → 逐轮因子置空，
      因子清单挂任务级（来自 DB 行，``selected`` 一律 None=未知）。
    同一因子名被多轮重复提出时，全部轮次共享该任务内同名行的结局
    （DB 无轮次字段，不猜轮次归属）；行内结局按 ``_pick_row`` 确定性选择。
    """
    manifest_map = dict(manifest or {})
    task_failed = str(task.get("status") or "") == "failed"
    task_level = scan.has_load_error

    rows_by_name: dict[str, list[Mapping[str, Any]]] = {}
    for row in db_rows:
        name = str(row.get("factor_name") or "").strip()
        if name:
            rows_by_name.setdefault(name, []).append(row)

    def fate_for_name(name: str, *, selected: bool | None) -> FactorFate:
        rows = rows_by_name.get(name) or []
        if not rows:
            return FactorFate(name=name, selected=selected, outcome=None, reason=None)
        row = _pick_row(rows, manifest_map)
        outcome, reason = _outcome_of(
            row.get("factor_id"), row.get("status"), manifest_map
        )
        return FactorFate(name=name, selected=selected, outcome=outcome, reason=reason)

    loops: list[LoopTrajectory] = []
    deepest = scan.loops[-1].loop if scan.loops else None
    for loop_scan in scan.loops:
        if task_level:
            factors: tuple[FactorFate, ...] = ()
        else:
            coder = set(loop_scan.coder_names)
            factors = tuple(
                fate_for_name(
                    name,
                    selected=(None if not loop_scan.coder_ok else name in coder),
                )
                for name in loop_scan.proposed_names
            )
        flags: list[str] = []
        # ① 该 Loop 无 selected 因子（细案 §4 D4 字面口径：有产出且无一进入实现；
        #    selected=None（解析失败）不算「无」——未知不立旗）
        if factors and all(f.selected is False for f in factors):
            flags.append(FLAG_NO_SELECTED)
        if task_failed and loop_scan.loop == deepest:
            flags.append(FLAG_TASK_FAILED_HERE)
        if factors and all(f.outcome in REJECTED_OUTCOMES for f in factors):
            flags.append(FLAG_ALL_REJECTED)
        loops.append(
            LoopTrajectory(
                loop=loop_scan.loop,
                nodes=loop_scan.nodes,
                factors=factors,
                flags=tuple(flags),
            )
        )

    task_factors: tuple[FactorFate, ...] = ()
    if task_level:
        # 任务级清单：按 DB 行序去重（同名多行取 _pick_row 口径），selected 未知
        seen_names: list[str] = []
        for row in db_rows:
            name = str(row.get("factor_name") or "").strip()
            if name and name not in seen_names:
                seen_names.append(name)
        task_factors = tuple(fate_for_name(name, selected=None) for name in seen_names)

    direction = str((task or {}).get("direction") or "").strip() or None
    market = str((task or {}).get("market") or "").strip() or None
    status = str((task or {}).get("status") or "").strip() or None
    return TaskTrajectory(
        task_id=str((task or {}).get("task_id") or ""),
        direction=direction,
        status=status,
        market=market,
        partial=status in PARTIAL_TASK_STATUSES,
        attribution=ATTRIBUTION_TASK_LEVEL if task_level else ATTRIBUTION_LOOP,
        loops=tuple(loops),
        factors=task_factors,
        truncated=scan.dropped_loops > 0,
        dropped_loops=scan.dropped_loops,
        generated_at=to_utc_iso(now or utc_now()),
    )


# ── 取数编排（API 调用）────────────────────────────────────────────────


def resolve_task_log_dir(task_id: str, *, root: Path | str | None = None) -> Path:
    """任务日志目录：``<LOG_TRACE_PATH>/<task_id>``（与 launcher 同源）。

    task_id 形状不合法直接 ``ValueError``（终点防目录穿越的第二道闸——
    第一道是 PG 归属查询）。
    """
    if not _SAFE_TASK_ID_RE.fullmatch(str(task_id)):
        raise ValueError(f"unsafe task_id: {task_id!r}")
    if root is None:
        from backend.services.engine.alpha_agent.launcher import _resolve_log_dir

        root = _resolve_log_dir()
    return Path(root) / str(task_id)


async def load_trajectory(
    *,
    task: Mapping[str, Any],
    log_dir: Path | str | None = None,
    now: datetime | None = None,
) -> TaskTrajectory:
    """取数编排：扫描工作区 + 因子行 + 物化清单 → :func:`build_trajectory`。"""
    task_id = str((task or {}).get("task_id") or "")
    user_id = str((task or {}).get("user_id") or "")
    if log_dir is None:
        log_dir = resolve_task_log_dir(task_id)
    # 扫描是纯同步 IO + 反序列化：挪出事件循环（engine 4 worker 经不起单请求堵）
    scan = await asyncio.to_thread(scan_workspace, log_dir)
    db_rows = await load_task_factors(user_id=user_id, task_id=task_id)
    # 物化清单同样是同步文件 IO：一并挪出事件循环（评审 code-review CR4）
    manifest = await asyncio.to_thread(_load_materialize_manifest)
    return build_trajectory(
        task=task, scan=scan, db_rows=db_rows, manifest=manifest, now=now
    )


__all__ = [
    "ATTRIBUTION_LOOP",
    "ATTRIBUTION_TASK_LEVEL",
    "FLAG_ALL_REJECTED",
    "FLAG_NO_SELECTED",
    "FLAG_TASK_FAILED_HERE",
    "FLAG_TEXTS",
    "MAX_LOOPS",
    "OUTCOME_MATERIALIZED",
    "OUTCOME_PENDING",
    "OUTCOME_REJECTED_DUPLICATE",
    "OUTCOME_REJECTED_GATE",
    "OUTCOME_SUPERSEDED",
    "PHASES",
    "PHASE_PAYLOAD_GLOBS",
    "PHASE_TIME_GLOBS",
    "REJECTED_OUTCOMES",
    "FactorFate",
    "LoopScan",
    "LoopTrajectory",
    "PhaseNode",
    "TaskTrajectory",
    "WorkspaceScan",
    "build_trajectory",
    "default_load_pickle",
    "extract_factor_names",
    "load_task_factors",
    "load_trajectory",
    "resolve_task_log_dir",
    "scan_workspace",
]
