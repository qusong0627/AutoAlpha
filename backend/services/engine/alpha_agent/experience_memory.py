"""T-FM-04 LLM 侧经验记忆/禁区 —— 纯派生层（无新持久化）。

数据源（全部既有事实，只读）：

1. ``rd_agent_mining_tasks`` —— 本用户 × 本市场任务史（direction/status/factor_count）；
2. rd_mined 物化清单 ``_materialize_manifest.json`` —— ``rejected_gate``（含结构化
   门禁判定）与 ``rejected_duplicate``（含 corr/corr_against）机器可读拒因实物；
3. T-FM-01 报告 JSON（``data/refs/factorminer/T-FM-01_factor_test_*.json`` 最新一份，
   存在才读）—— 统计派 ``reject_reason`` 词表与值级去重命中对（静态数据）；
4. ``rd_agent_factors`` —— ``factor_id → metadata_json.task_id``（归并方向），
   只归因本用户名下的因子。

产物三步（与池注入通道同构，见 ``mining_plugins.pool_service`` 先例）：

- :func:`build_memory` 组装（纯函数，可注入四源，测试零 DB）；
- :func:`render_block` 渲染注入段（``【历史经验·禁区】`` 开头，≤60 行）；
- :func:`prepare_injection` 原子写 ``<log_dir>/experience_context.md`` 并返回路径，
  launcher 经 env ``QMF_EXPERIENCE_CONTEXT_PATH`` 交给 ``rd_loop_wrapper``
  读文件追加进提示词（每任务落盘 = 注入内容可复现的审计凭证）。

纪律：

- 开关 ``QM_EXPERIENCE_MEMORY_ENABLED``（默认开，kill 写 ``false``；读法唯一走
  ``shared/env_flags.env_flag``——词表只有小写 ``true``，设 ``1``/``yes`` 均视为关）；
- 任何失败只告警不拦主链（``prepare_injection`` 返回 None = 不注入）；
- 归一桶词表 = 细案附录 B 12 桶 + ``other`` 兜底——未知原生串绝不静默丢弃；
- 对不上方向的查重命中归 ``(未归因)`` 桶如实展示，不硬编造映射；
- 多用户共享记忆：不做（本用户 × 本市场，与 direction_sampling 史口径一致；
  他人/paper 静态因子一律 ``(未归因)``）；
- 纯文本增益层：本模块不改变任何闸判决、不写任何因子状态。
"""

from __future__ import annotations

import json
import logging
import os
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from backend.shared.env_flags import env_flag
from backend.shared.utc_datetime import to_utc_iso, utc_now

logger = logging.getLogger(__name__)

#: 开关（默认开；只有小写 ``true`` 是开——见 shared/env_flags 口径）
ENV_FLAG = "QM_EXPERIENCE_MEMORY_ENABLED"

#: 注入文件名（任务日志目录下；launcher 经 env 传绝对路径）
CONTEXT_FILENAME = "experience_context.md"

#: 查重命中对不上任何本用户任务时的方向桶
UNATTRIBUTED = "(未归因)"
#: 任务 direction 为空串时的展示名（空串不上屏）
NO_DIRECTION = "(未填方向)"

#: 渲染段硬上限（行）与每节 top-N（细案 D4）
RENDER_MAX_LINES = 60
RENDER_TOP = 8
#: 每桶样例列/方向上限
SAMPLE_CAP = 3
#: 方向展示名截断（首行；超出追加省略号）
LABEL_MAX_CHARS = 48

#: 90 日饱和窗（dup_hits_90d 的口径）
DUP_WINDOW_DAYS = 90

RENDER_TITLE = "【历史经验·禁区】"
DISCIPLINE_LINE = (
    "以上为事实统计，方向选择权在你；命中已知饱和方向的提案需要说明差异化理由。"
)

#: 拒因归一 bucket → 中文标签（附录 B 词表 + other 兜底；前后端同一出处）
BUCKET_LABELS: dict[str, str] = {
    "dup_library": "值级重复（库内已有高度相关列）",
    "gate:fidelity": "扰动保真度不足",
    "gate:rank_drift": "排序结构日间漂移过大",
    "gate:pool_pct": "池内相对位置过靠后",
    "gate:turnover": "换手过高（成本侵蚀）",
    "gate:net_return": "扣成本后无收益",
    "gate:incremental": "对库冗余（独立信息不足）",
    "stat:dedup": "统计派·值级去重被挤",
    "stat:fdr": "统计派·多重检验未过（BH-FDR）",
    "stat:fold": "统计派·折间符号不稳",
    "empty_factor": "因子值为空",
    "coding_error": "因子代码错误",
    "other": "其他（未映射的原生拒因）",
}

#: 门禁结构化 key → bucket（manifest rejected_gate 的 gates[].key；优先于串匹配）
GATE_KEY_BUCKETS: dict[str, str] = {
    "pfs_floor": "gate:fidelity",
    "rre_floor": "gate:rank_drift",
    "ic_pool_pct": "gate:pool_pct",
    "turnover_cap": "gate:turnover",
    "net_return_floor": "gate:net_return",
    "incremental_ic_ir_floor": "gate:incremental",
}

#: 原生串子串 → bucket（附录 B；顺序敏感——先长后短、先具体后宽泛）
_RAW_RULES: tuple[tuple[str, str], ...] = (
    ("扰动保真度不足", "gate:fidelity"),
    ("排序结构日间漂移过大", "gate:rank_drift"),
    ("池内相对位置过靠后", "gate:pool_pct"),
    ("换手过高", "gate:turnover"),
    ("成本侵蚀", "gate:turnover"),
    ("扣成本后无收益", "gate:net_return"),
    ("对库冗余", "gate:incremental"),
    ("独立信息不足", "gate:incremental"),
    ("高度相关", "stat:dedup"),
    ("多重检验未过", "stat:fdr"),
    ("折间符号不稳", "stat:fold"),
    ("FactorEmptyError", "empty_factor"),
    ("CoderError", "coding_error"),
)

#: 参与组装的 manifest 条目状态（其余状态不进任何统计）
_CONSUMED_STATUSES = ("rejected_duplicate", "rejected_gate", "materialized")

_SOURCE_LABELS = {"manifest": "物化清单", "report": "统计派报告"}
_SOURCE_ORDER = ("manifest", "report")


# ── 归一（附录 B）──────────────────────────────────────────────────────


def normalize_bucket(raw: str | None, *, gate_key: str | None = None) -> str:
    """原生拒因串/门禁 key → 稳定 bucket。

    门禁结构化 ``key`` 优先（message 串里即使顺带提到别的门禁词也不误归）；
    其余按附录 B 子串表匹配；未知一律 ``other``（绝不静默丢弃）。
    """
    if gate_key and gate_key in GATE_KEY_BUCKETS:
        return GATE_KEY_BUCKETS[gate_key]
    text = str(raw or "").strip()
    for needle, bucket in _RAW_RULES:
        if needle in text:
            return bucket
    return "other"


# ── 数据模型 ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ReasonBucket:
    """一个拒因桶的聚合：次数 + 最近 + 样例（方向/列）+ 来源标注。"""

    bucket: str
    label: str
    count: int
    last_at: str | None
    sample_dirs: tuple[str, ...] = ()
    sample_columns: tuple[str, ...] = ()
    sources: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "bucket": self.bucket,
            "label": self.label,
            "count": self.count,
            "last_at": self.last_at,
            "sample_dirs": list(self.sample_dirs),
            "sample_columns": list(self.sample_columns),
            "sources": list(self.sources),
        }


@dataclass(frozen=True)
class SaturatedDirection:
    """饱和方向：值级查重命中聚合（T-FE-10 硬饱和的输入）。"""

    direction: str
    label: str
    dup_hits: int
    dup_hits_90d: int
    top_opponent: str | None
    materialized_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "label": self.label,
            "dup_hits": self.dup_hits,
            "dup_hits_90d": self.dup_hits_90d,
            "top_opponent": self.top_opponent,
            "materialized_count": self.materialized_count,
        }


@dataclass(frozen=True)
class DirectionStat:
    """方向粒度任务史（本用户 × 本市场）。"""

    direction: str
    label: str
    tasks: int
    completed: int
    factors: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "label": self.label,
            "tasks": self.tasks,
            "completed": self.completed,
            "factors": self.factors,
        }


@dataclass(frozen=True)
class ExperienceMemory:
    """一次组装的全量读数（API 直出 + 渲染注入段共用同一份）。"""

    available: bool
    market: str
    generated_at: str | None
    reason_buckets: tuple[ReasonBucket, ...] = ()
    saturated_directions: tuple[SaturatedDirection, ...] = ()
    direction_stats: tuple[DirectionStat, ...] = ()
    sources: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "available": bool(self.available),
            "market": self.market,
            "generated_at": self.generated_at,
            "reason_buckets": [b.to_dict() for b in self.reason_buckets],
            "saturated_directions": [s.to_dict() for s in self.saturated_directions],
            "direction_stats": [d.to_dict() for d in self.direction_stats],
            "sources": dict(self.sources),
        }


# ── 组装（纯函数）──────────────────────────────────────────────────────


def _parse_at(raw: Any) -> datetime | None:
    """ISO 串 → aware UTC；非法/缺失 → None（不猜时间）。naive 视为 UTC
    （写入侧唯一口径是 aware UTC；这里是只读兜底，不是放行 naive）。"""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _short_label(text: str) -> str:
    """首行 + 截断的展示名（长假设文本不进表格/提示词原文）。"""
    first = next((ln.strip() for ln in str(text).splitlines() if ln.strip()), "")
    if len(first) > LABEL_MAX_CHARS:
        return first[:LABEL_MAX_CHARS] + "…"
    return first


class _BucketAcc:
    """桶累加器（build_memory 内部用；不导出）。"""

    __slots__ = ("count", "last_raw", "last_dt", "dirs", "columns", "sources")

    def __init__(self) -> None:
        self.count = 0
        self.last_raw: str | None = None
        self.last_dt: datetime | None = None
        self.dirs: list[str] = []
        self.columns: list[str] = []
        self.sources: set[str] = set()

    def add(
        self,
        *,
        at: Any,
        column: str | None,
        direction: str | None,
        source: str,
    ) -> None:
        self.count += 1
        self.sources.add(source)
        dt = _parse_at(at)
        if dt is not None and (self.last_dt is None or dt > self.last_dt):
            self.last_dt = dt
            self.last_raw = str(at)
        if column and column not in self.columns and len(self.columns) < SAMPLE_CAP:
            self.columns.append(column)
        # 方向样例过短标签：真数据 direction 是整段多行假设，原样进样例会把
        # 渲染段撕成任意多行（行数上限失真）；原始值在 direction_stats 可查。
        if direction and len(self.dirs) < SAMPLE_CAP:
            label = _short_label(direction)
            if label and label not in self.dirs:
                self.dirs.append(label)


def build_memory(
    *,
    market: str,
    tasks: Sequence[Mapping[str, Any]] = (),
    manifest: Mapping[str, Any] | None = None,
    factor_task_ids: Mapping[str, str] | None = None,
    task_directions: Mapping[str, str] | None = None,
    report: Mapping[str, Any] | None = None,
    report_path: str | None = None,
    now: datetime | None = None,
) -> ExperienceMemory:
    """四源组装（纯函数；只读输入，不改任何入参）。

    - ``tasks``：``rd_agent_mining_tasks`` 行（direction/status/factor_count）；
    - ``manifest``：物化清单 factor_id → entry；
    - ``factor_task_ids`` / ``task_directions``：factor_id → task_id → direction
      （只含本用户 × 本市场；查不到 → ``(未归因)``）；
    - ``report``：T-FM-01 报告 JSON（``paper``/``house`` 两节）。
    """
    manifest = dict(manifest or {})
    factor_task_ids = {str(k): str(v) for k, v in (factor_task_ids or {}).items()}
    task_directions = {str(k): str(v) for k, v in (task_directions or {}).items()}
    now_dt = now or utc_now()
    cutoff = now_dt - timedelta(days=DUP_WINDOW_DAYS)

    def direction_of(key: str) -> str | None:
        task_id = factor_task_ids.get(str(key))
        if not task_id:
            return None
        value = (task_directions.get(str(task_id)) or "").strip()
        return value or None

    # ── 拒因聚类 ──
    acc: dict[str, _BucketAcc] = {}

    def add_bucket(
        bucket: str, *, at: Any, column: str | None, direction: str | None, source: str
    ) -> None:
        slot = acc.setdefault(bucket, _BucketAcc())
        slot.add(at=at, column=column, direction=direction, source=source)

    consumed = 0
    for fid in sorted(manifest):
        entry = manifest.get(fid) or {}
        status = str(entry.get("status") or "")
        if status not in _CONSUMED_STATUSES:
            continue
        consumed += 1
        column = str(entry.get("column") or fid)
        direction = direction_of(fid)
        if status == "rejected_duplicate":
            add_bucket(
                "dup_library",
                at=entry.get("at"),
                column=column,
                direction=direction,
                source="manifest",
            )
        elif status == "rejected_gate":
            gates = ((entry.get("gates") or {}).get("gates")) or []
            for gate in gates:
                if not isinstance(gate, Mapping):
                    continue
                # 只有 hard+fail 是「拒因」；soft fail / pass / skipped 不算
                if str(gate.get("status") or "") != "fail":
                    continue
                if str(gate.get("mode") or "") != "hard":
                    continue
                add_bucket(
                    normalize_bucket(
                        str(gate.get("message") or ""),
                        gate_key=str(gate.get("key") or ""),
                    ),
                    at=entry.get("at"),
                    column=column,
                    direction=direction,
                    source="manifest",
                )

    report_at = report.get("generated_at") if report else None
    if report:
        for side in ("paper", "house"):
            section = report.get(side) or {}
            if not isinstance(section, Mapping):
                continue
            for factor in section.get("factors") or []:
                if not isinstance(factor, Mapping):
                    continue
                raw = str(factor.get("reject_reason") or "")
                segments = [s.strip() for s in raw.replace(";", "；").split("；")]
                key = str(factor.get("key") or "")
                for seg in segments:
                    if not seg:
                        continue
                    add_bucket(
                        normalize_bucket(seg),
                        at=report_at,
                        column=key or None,
                        direction=direction_of(key),
                        source="report",
                    )

    reason_buckets = tuple(
        ReasonBucket(
            bucket=name,
            label=BUCKET_LABELS.get(name, name),
            count=slot.count,
            last_at=slot.last_raw,
            sample_dirs=tuple(slot.dirs),
            sample_columns=tuple(slot.columns),
            sources=tuple(s for s in _SOURCE_ORDER if s in slot.sources),
        )
        for name, slot in sorted(acc.items(), key=lambda kv: (-kv[1].count, kv[0]))
    )

    # ── 饱和方向（值级查重命中聚合）──
    # rows: (direction, opponent, at_raw|None)
    rows: list[tuple[str, str, Any]] = []
    materialized: Counter[str] = Counter()
    for fid in sorted(manifest):
        entry = manifest.get(fid) or {}
        status = str(entry.get("status") or "")
        if status == "rejected_duplicate":
            rows.append(
                (
                    direction_of(fid) or UNATTRIBUTED,
                    str(entry.get("corr_against") or ""),
                    entry.get("at"),
                )
            )
        elif status == "materialized":
            materialized[direction_of(fid) or UNATTRIBUTED] += 1
    if report:
        for side in ("paper", "house"):
            section = report.get(side) or {}
            if not isinstance(section, Mapping):
                continue
            dropped = (section.get("dedup") or {}).get("dropped") or []
            for pair in dropped:
                if not isinstance(pair, Sequence) or isinstance(pair, (str, bytes)):
                    continue
                key = str(pair[0]) if len(pair) > 0 else ""
                opponent = str(pair[1]) if len(pair) > 1 else ""
                rows.append((direction_of(key) or UNATTRIBUTED, opponent, None))

    sat_acc: dict[str, dict[str, Any]] = {}
    for direction, opponent, at in rows:
        slot = sat_acc.setdefault(
            direction, {"hits": 0, "hits90": 0, "opponents": Counter()}
        )
        slot["hits"] += 1
        dt = _parse_at(at)
        if dt is not None and dt >= cutoff:
            slot["hits90"] += 1
        if opponent:
            slot["opponents"][opponent] += 1

    saturated: list[SaturatedDirection] = []
    for direction, slot in sat_acc.items():
        opponents: Counter[str] = slot["opponents"]
        top_opponent = (
            min(opponents.items(), key=lambda kv: (-kv[1], kv[0]))[0]
            if opponents
            else None
        )
        saturated.append(
            SaturatedDirection(
                direction=direction,
                label=_short_label(direction) or direction,
                dup_hits=int(slot["hits"]),
                dup_hits_90d=int(slot["hits90"]),
                top_opponent=top_opponent,
                materialized_count=int(materialized.get(direction, 0)),
            )
        )
    saturated.sort(key=lambda s: (-s.dup_hits, -s.dup_hits_90d, s.direction))

    # ── 方向任务史 ──
    stats_acc: dict[str, dict[str, int]] = {}
    for task in tasks:
        raw = str((task or {}).get("direction") or "").strip()
        key = raw or NO_DIRECTION
        slot = stats_acc.setdefault(key, {"tasks": 0, "completed": 0, "factors": 0})
        slot["tasks"] += 1
        if str((task or {}).get("status") or "") == "completed":
            slot["completed"] += 1
        try:
            slot["factors"] += int((task or {}).get("factor_count") or 0)
        except (TypeError, ValueError):
            pass  # 计数坏值按 0 计入（不改行、不炸组装）
    direction_stats = tuple(
        DirectionStat(
            direction=key,
            label=_short_label(key) or key,
            tasks=slot["tasks"],
            completed=slot["completed"],
            factors=slot["factors"],
        )
        for key, slot in sorted(
            stats_acc.items(), key=lambda kv: (-kv[1]["tasks"], kv[0])
        )
    )

    has_report = bool(report)
    available = bool(tasks) or consumed > 0 or has_report
    return ExperienceMemory(
        available=available,
        market=str(market or ""),
        generated_at=to_utc_iso(now_dt),
        reason_buckets=reason_buckets,
        saturated_directions=tuple(saturated),
        direction_stats=direction_stats,
        sources={
            "tasks": len(tasks),
            "manifest_entries": consumed,
            # 只回文件名：展示面只需要「有无报告」真值判断，绝对路径出网
            # 等于泄露宿主/容器目录结构（评审 security-F5）
            "report": os.path.basename(report_path) if report_path else None,
            "report_generated_at": str(report_at) if report_at else None,
        },
    )


# ── 渲染（注入段）─────────────────────────────────────────────────────


def _cap_lines(lines: list[str], max_lines: int) -> str:
    """行数硬上限：超限截断并在段内标注（不静默）。"""
    if max_lines < 1:
        max_lines = 1
    if len(lines) > max_lines:
        lines = lines[: max_lines - 1] + [
            f"（已截断：超出 {max_lines} 行上限，其余行未列）"
        ]
    return "\n".join(lines)


def _sources_cn(sources: tuple[str, ...]) -> str:
    return "+".join(_SOURCE_LABELS.get(s, s) for s in sources) or "—"


def render_block(memory: ExperienceMemory, *, max_lines: int = RENDER_MAX_LINES) -> str:
    """渲染注入段：``【历史经验·禁区】`` 开头、纪律句结尾、≤ ``max_lines`` 行。

    超 top-8 的条目按 count 序截断并标注「已截断」；空源 → 最小段。
    """
    if not memory.available:
        lines = [
            RENDER_TITLE,
            f"（{memory.market or '—'} · 暂无历史统计）",
            "",
            "本用户 · 本市场暂无可用历史统计（任务史 / 物化清单 / 统计派报告均为空）。",
            "",
            DISCIPLINE_LINE,
        ]
        return _cap_lines(lines, max_lines)

    lines = [
        RENDER_TITLE,
        f"（{memory.market or '—'} · 生成于 {memory.generated_at or '—'}；"
        f"来源：任务史 {memory.sources.get('tasks', 0)} 条 · "
        f"物化清单 {memory.sources.get('manifest_entries', 0)} 条 · "
        f"统计派报告 {'有' if memory.sources.get('report') else '无'}）",
        "",
        f"拒因聚类（Top {min(max(len(memory.reason_buckets), 1), RENDER_TOP)}）：",
    ]
    if not memory.reason_buckets:
        lines.append("- （—）")
    for bucket in memory.reason_buckets[:RENDER_TOP]:
        cols = "、".join(bucket.sample_columns) or "—"
        dirs_txt = (
            f"；方向 {'、'.join(bucket.sample_dirs)}" if bucket.sample_dirs else ""
        )
        lines.append(
            f"- {bucket.label}（{bucket.bucket}）×{bucket.count}；"
            f"最近 {bucket.last_at or '—'}；样例列 {cols}{dirs_txt}"
            f"（来源：{_sources_cn(bucket.sources)}）"
        )
    lines.append("")
    lines.append(
        f"饱和方向（Top {min(max(len(memory.saturated_directions), 1), RENDER_TOP)}，"
        "统计派值级查重命中）："
    )
    if not memory.saturated_directions:
        lines.append("- （—）")
    for sat in memory.saturated_directions[:RENDER_TOP]:
        lines.append(
            f"- {sat.label}：命中 {sat.dup_hits}（近 90 日 {sat.dup_hits_90d}）；"
            f"已物化 {sat.materialized_count}；最强对手 {sat.top_opponent or '—'}"
        )
    lines.append("")
    dropped_buckets = max(0, len(memory.reason_buckets) - RENDER_TOP)
    dropped_dirs = max(0, len(memory.saturated_directions) - RENDER_TOP)
    if dropped_buckets or dropped_dirs:
        lines.append(
            f"（已截断：另有 {dropped_buckets} 个拒因桶、{dropped_dirs} 个饱和方向未列）"
        )
    lines.append(DISCIPLINE_LINE)
    return _cap_lines(lines, max_lines)


# ── 取数（DB / 文件面；组装全部走 build_memory）─────────────────────────


def _candidate_ref_dirs() -> tuple[Path, ...]:
    """统计派报告目录候选（与 shared/quantdb_paths 同纪律：容器挂载点优先）。

    容器里 ``/app/data`` 是空壳桩目录——真实数据在 ``./data:/data`` 挂载点，
    只认仓库根会把报告源在容器内静默丢掉（真数据 smoke 抓到过）。
    """
    return (
        Path("/data/refs/factorminer"),  # docker ./data:/data 挂载点
        Path(__file__).resolve().parents[4]
        / "data"
        / "refs"
        / "factorminer",  # 仓库根（host 符号链接）
    )


def _refs_dir() -> Path:
    """统计派报告目录：首个「存在且非空」的候选；全落空返回仓库根候选（报错可定位）。"""
    candidates = _candidate_ref_dirs()
    for cand in candidates:
        try:
            if cand.is_dir() and any(cand.iterdir()):
                return cand
        except OSError:  # 权限不足等：视作不可用，继续回退
            continue
    return candidates[-1]


def _load_manifest_safe() -> dict[str, Any]:
    """rd_mined 物化清单（读失败一律空清单——记忆是增益层，不拦任何主链）。"""
    try:
        from backend.scripts.rd_mined_materialize import (
            _lib_root,
            _load_manifest,
        )

        return _load_manifest(_lib_root())
    except Exception as exc:  # noqa: BLE001
        logger.warning("[experience-memory] 物化清单读取失败（按空清单）: %s", exc)
        return {}


def _load_latest_report() -> tuple[dict[str, Any] | None, str | None]:
    """最新一份 T-FM-01 报告（静态数据，存在才读；损坏 → None + 告警）。"""
    try:
        candidates = sorted(
            _refs_dir().glob("T-FM-01_factor_test_*.json"),
            key=lambda p: p.stat().st_mtime,
        )
    except OSError as exc:
        logger.warning("[experience-memory] 报告目录扫描失败: %s", exc)
        return None, None
    if not candidates:
        return None, None
    path = candidates[-1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("[experience-memory] 统计派报告损坏，跳过：%s（%s）", path, exc)
        return None, None
    if not isinstance(data, dict):
        logger.warning("[experience-memory] 统计派报告顶层非对象，跳过：%s", path)
        return None, None
    return data, str(path)


async def _query_factor_task_ids(
    factor_ids: Sequence[str], *, user_id: str
) -> dict[str, str]:
    """factor_id → task_id（**仅本用户名下**；他人因子读不出 → 归 (未归因)）。"""
    ids = [str(x) for x in factor_ids if x]
    if not ids:
        return {}
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    async with get_session(read_only=True) as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT factor_id, metadata_json->>'task_id' AS task_id "
                        "FROM rd_agent_factors "
                        "WHERE factor_id = ANY(:ids) AND user_id = :user_id"
                    ),
                    {"ids": ids, "user_id": str(user_id)},
                )
            )
            .mappings()
            .all()
        )
    return {
        str(r["factor_id"]): str(r["task_id"])
        for r in rows
        if r["factor_id"] and r["task_id"]
    }


async def _query_task_directions(
    task_ids: Sequence[str], *, user_id: str, market: str
) -> dict[str, str]:
    """task_id → direction（本用户 × 本市场；查空 = 不归因）。"""
    ids = [str(x) for x in task_ids if x]
    if not ids:
        return {}
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    async with get_session(read_only=True) as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT task_id, direction FROM rd_agent_mining_tasks "
                        "WHERE task_id = ANY(:ids) AND user_id = :user_id "
                        "AND market = :market"
                    ),
                    {"ids": ids, "user_id": str(user_id), "market": str(market)},
                )
            )
            .mappings()
            .all()
        )
    return {str(r["task_id"]): str(r["direction"] or "") for r in rows}


async def load_memory(*, user_id: str, market: str) -> ExperienceMemory:
    """四源取数 → :func:`build_memory`（API 与注入共用同一组装）。"""
    from backend.services.engine.alpha_agent.task_store import (
        MAX_HISTORY_LIMIT,
        get_mining_task_store,
    )

    tasks = await get_mining_task_store().list_history(
        user_id=str(user_id),
        market=str(market) or None,
        limit=MAX_HISTORY_LIMIT,
    )
    manifest = _load_manifest_safe()
    ids = sorted(
        fid
        for fid, entry in manifest.items()
        if str((entry or {}).get("status") or "") in _CONSUMED_STATUSES
    )
    factor_task_ids = await _query_factor_task_ids(ids, user_id=str(user_id))
    task_directions = await _query_task_directions(
        sorted(set(factor_task_ids.values())),
        user_id=str(user_id),
        market=str(market),
    )
    report, report_path = _load_latest_report()
    return build_memory(
        market=str(market),
        tasks=tasks,
        manifest=manifest,
        factor_task_ids=factor_task_ids,
        task_directions=task_directions,
        report=report,
        report_path=report_path,
    )


# ── 注入文件（launcher 调用）──────────────────────────────────────────


async def prepare_injection(
    *,
    user_id: str,
    market: str,
    task_id: str | None = None,
    log_dir: Path | str,
) -> Path | None:
    """组装 + 渲染 + 原子写 ``<log_dir>/experience_context.md``，返回文件路径。

    返回 None = 不注入（开关关闭 / 四源全空 / 任何失败——注入是增益层，
    绝不允许拦住一次挖矿）。``task_id`` 现仅留痕用，与 pool_service 签名对称。
    """
    if not env_flag(ENV_FLAG, default=True):
        return None
    try:
        memory = await load_memory(user_id=str(user_id), market=str(market or ""))
        if not memory.available:
            return None
        text = render_block(memory)
        path = Path(log_dir) / CONTEXT_FILENAME
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text(text + "\n", encoding="utf-8")
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)
        logger.info(
            "[experience-memory] 注入段已落盘（task=%s，桶 %d / 饱和方向 %d）→ %s",
            str(task_id or "")[:8] or "-",
            len(memory.reason_buckets),
            len(memory.saturated_directions),
            path,
        )
        return path
    except Exception as exc:  # noqa: BLE001 — 注入失败不拦挖掘
        logger.warning("[experience-memory] 注入准备失败（不拦挖掘）: %s", exc)
        return None


__all__ = [
    "BUCKET_LABELS",
    "CONTEXT_FILENAME",
    "DISCIPLINE_LINE",
    "ENV_FLAG",
    "ExperienceMemory",
    "NO_DIRECTION",
    "RENDER_MAX_LINES",
    "RENDER_TITLE",
    "ReasonBucket",
    "SaturatedDirection",
    "UNATTRIBUTED",
    "build_memory",
    "load_memory",
    "normalize_bucket",
    "prepare_injection",
    "render_block",
]
