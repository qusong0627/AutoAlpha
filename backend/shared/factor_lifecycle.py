"""因子生命周期状态机 + 滚动 12m 评估判定律 + 台账 IO（T-FE-06，唯一实现）。

来源：``docs/因子挖掘_方法论学习与后期优化_20261011.md`` §1.2——入库后
6 个月盲窗观察（盲窗内评估读数对研究员面不可见）；滚动 12m IC_IR < 0.2
且 3 个月末未回升 → 降权退役（保留算力：不删行、不删物化列、不删 run）。

口径边界（唯一实现，勿在别处重写）：
- 状态机四态 ``experiment → observing → active → downweighted``；**派生为
  默认**（由「有无成功回测证据 + 盲窗是否到期」推得），``metadata_json.
  lifecycle.state`` 只存**显式覆盖**（评估器写 downweighted；人工 restore
  = 清除覆盖）。存量因子零回填即获得正确状态。
- 成功回测证据 = run 台账 completed 行 ∨ ``factor.status='completed'``
  （run 台账 2026-10 才启用，旧流程回测不在台账——靠 status 兜底，否则
  400+ 存量老因子会被误判回「实验」）。
- 盲窗锚点 = 首个 completed run 的 created_at（run 台账是后加的，缺失时
  兜底行 created_at）+ 180 天。
- 三态分流不混：``ok`` / 不可算（``too_few_days``/``series_stale``/
  ``flat_series``/``no_series``）——「不可算」不冒充「差」（不凑 0）。
- 连击（连续 3 个月末弱读数）只认**连续有效弱读数**；任何非弱/不可算
  读数清零连击——降权动作重、恢复靠人工，宁可漏判不可误判。
- 判定律纯函数无 IO 无时钟（now/asof 一律入参）；台账 IO 原子替换
  （tmp + ``os.replace``），坏台账自愈为骨架、绝不炸评估器。

纯标准库实现，便于在脚本/引擎/测试中直接 import。
"""

from __future__ import annotations

import calendar
import json
import math
import os
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from backend.shared.factor_tags import read_metadata
from backend.shared.utc_datetime import as_utc, to_utc_iso

__all__ = [
    "BLIND_DAYS",
    "DECISION_DOWNWEIGHT",
    "DECISION_HOLD",
    "DECISION_OBSERVING_PROTECTED",
    "ICIR_FLOOR",
    "LEDGER_MAX_EVENTS",
    "LEDGER_MAX_READINGS",
    "LEDGER_SCHEMA_VERSION",
    "MATURITY_LABELS",
    "MATURITY_UNKNOWN",
    "MATURITY_VALIDATING",
    "MIN_N_DAYS",
    "MonthReading",
    "READING_FLAT",
    "READING_NO_SERIES",
    "READING_OK",
    "READING_SERIES_STALE",
    "READING_TOO_FEW",
    "STALE_DAYS",
    "STATES",
    "STATE_ACTIVE",
    "STATE_DOWNWEIGHTED",
    "STATE_EXPERIMENT",
    "STATE_OBSERVING",
    "STREAK_MONTHS",
    "WINDOW_DAYS",
    "append_event",
    "blind_until_of",
    "has_completed_backtest",
    "lifecycle_view",
    "load_ledger",
    "maturity_of",
    "month_ends_back",
    "read_month",
    "resolve_state",
    "save_ledger",
    "streak_of",
    "upsert_readings",
    "verdict_of",
]

# ---------------------------------------------------------------- 状态机常量

STATE_EXPERIMENT = "experiment"
STATE_OBSERVING = "observing"
STATE_ACTIVE = "active"
STATE_DOWNWEIGHTED = "downweighted"

#: 合法状态全集（显式覆盖只认这里的值；未知值忽略回落派生）。
STATES: tuple[str, ...] = (
    STATE_EXPERIMENT,
    STATE_OBSERVING,
    STATE_ACTIVE,
    STATE_DOWNWEIGHTED,
)

#: 成熟度展示名（六维标签之一）；「验证中」是展示态不是状态态——只在
#: experiment 且有活跃验证跑动时装饰，不进状态机（防止第二个状态源）。
MATURITY_LABELS: dict[str, str] = {
    STATE_EXPERIMENT: "实验",
    STATE_OBSERVING: "观察",
    STATE_ACTIVE: "在用",
    STATE_DOWNWEIGHTED: "退役",
}
MATURITY_VALIDATING = "验证中"
MATURITY_UNKNOWN = "unknown"

#: 盲窗天数（入库后观察期；§1.2 的「6 个月」）。
BLIND_DAYS = 180

# -------------------------------------------------------------- 评估判定律常量

#: 滚动 12m ICIR 底线（连续低于它才可能触发降权）。
ICIR_FLOOR = 0.2

#: 连续多少个自然月末读数低于底线才裁决降权。
STREAK_MONTHS = 3

#: 滚动窗长（自然日；12 个月）。
WINDOW_DAYS = 365

#: 窗内有效读数下限（≈半年交易日；不足不结论）。
MIN_N_DAYS = 120

#: 序列陈旧门（序列最新日距参考月末超过此自然日数 → 不结论，提示重跑）。
STALE_DAYS = 60

# 读数状态（ok 之外都属于「不可算」，永不冒充「差」）。
READING_OK = "ok"
READING_TOO_FEW = "too_few_days"
READING_SERIES_STALE = "series_stale"
READING_FLAT = "flat_series"
READING_NO_SERIES = "no_series"

# 裁决（verdict_of 输出）。
DECISION_HOLD = "hold"
DECISION_OBSERVING_PROTECTED = "observing_protected"
DECISION_DOWNWEIGHT = "downweight"

# ------------------------------------------------------------------ 台账常量

LEDGER_SCHEMA_VERSION = 1

#: 每因子保留的最近读数期数（跨月累积、剪枝最旧）。
LEDGER_MAX_READINGS = 24

#: 每因子保留的最近状态变更史条数（同读数上限量级）。
LEDGER_MAX_EVENTS = 24

#: ``metadata.lifecycle`` 下属于「评估读数」的键（装配层盲窗内/证据降级时
#: 剥离；持久层原地保留——脱敏只发生在出口）。
_BLIND_READING_KEYS = frozenset({"last_eval", "evidence"})


# ------------------------------------------------------------------ 纯函数


def blind_until_of(
    *,
    created_at: datetime | None,
    first_completed_run_at: datetime | None,
    blind_days: int = BLIND_DAYS,
) -> datetime | None:
    """盲窗终点 = 锚点 + blind_days；无锚点 → None（不虚构）。

    锚点优先取**首个 completed run** 的 created_at（入库即观察的事实起点）；
    run 台账缺失（老因子）兜底行 created_at。naive 输入按 UTC 解释
    （``as_utc`` 的唯一口径）。两者皆 None 时返回 None——没有事实锚点就
    没有盲窗，调用方按「无窗可算」处理（评审 LOW-2：旧实现经 ``as_utc``
    兜底 ``utc_now`` 顺手编了个 now+180d 的伪终点）。
    """
    anchor = (
        first_completed_run_at if first_completed_run_at is not None else created_at
    )
    if anchor is None:
        return None
    return as_utc(anchor) + timedelta(days=blind_days)


def resolve_state(
    *,
    explicit_state: str | None,
    has_completed_run: bool,
    blind_until: datetime | None,
    now: datetime,
) -> str:
    """状态派生：显式覆盖优先，否则按事实推。

    - ``explicit_state`` ∈ STATES → 原样（含人工 restore 后残留的 active）；
    - 无成功回测证据（``has_completed_run``，调用方按
      :func:`has_completed_backtest` 计算）→ experiment；
    - 有且 ``now < blind_until`` → observing；否则 active
      （blind_until 缺失按 active：无窗可算不硬锁观察）。
    """
    if explicit_state in STATES:
        return explicit_state
    if not has_completed_run:
        return STATE_EXPERIMENT
    if blind_until is not None and as_utc(now) < as_utc(blind_until):
        return STATE_OBSERVING
    return STATE_ACTIVE


def has_completed_backtest(
    row: Mapping[str, Any], *, first_completed_run_at: datetime | None = None
) -> bool:
    """是否存在「成功回测」事实（状态派生的 experiment 判据）。

    run 台账 completed 行 ∨ ``row.status == 'completed'``——后者兜底旧流程
    （run 台账 2026-10 才启用，存量因子没有台账行，但 status=completed 即
    已成功回测过；不兜底会把 400+ 老因子误判回「实验」）。
    """
    if first_completed_run_at is not None:
        return True
    return str(row.get("status") or "") == "completed"


def maturity_of(state: str, *, has_active_validation: bool = False) -> str:
    """状态 → 成熟度展示名；experiment + 活跃验证 → 「验证中」（展示态）。"""
    if state == STATE_EXPERIMENT and has_active_validation:
        return MATURITY_VALIDATING
    return MATURITY_LABELS.get(state, MATURITY_UNKNOWN)


@dataclass(frozen=True)
class MonthReading:
    """单个月末的滚动 12m 读数（判断与留痕的最小单元）。"""

    month_end: str
    n_days: int
    icir: float | None
    status: str
    #: 仅 status=ok 时有意义；None = 不可算（不是 False）。
    below_floor: bool | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _to_date(value: date | str | None) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and len(value) >= 10:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def read_month(
    series: dict[str, Any] | None,
    *,
    month_end: date | str,
    window_days: int = WINDOW_DAYS,
    min_n_days: int = MIN_N_DAYS,
    stale_days: int = STALE_DAYS,
    icir_floor: float = ICIR_FLOOR,
) -> MonthReading:
    """序列 → 指定月末的滚动读数（幂等纯函数；只看 ≤ month_end 的数据）。

    输入 ``series`` = ``{"dates": [ISO 串], "ic": [float|None]}``
    （``factor_backtest`` 的 series_json 形状）。判定顺序：陈旧（整段序列
    与参考点脱节）→ 窗内计数 → 可算性（std=0/单值不可结论）。
    """
    end = _to_date(month_end)
    end_s = end.isoformat() if end else str(month_end)
    dates = list((series or {}).get("dates") or [])
    ics = list((series or {}).get("ic") or [])
    if not dates:
        return MonthReading(end_s, 0, None, READING_NO_SERIES, None)

    pairs: list[tuple[date, float]] = []
    for raw_d, raw_v in zip(dates, ics, strict=False):
        if raw_v is None:
            continue
        try:
            day = date.fromisoformat(str(raw_d)[:10])
            value = float(raw_v)
        except (ValueError, TypeError):
            continue
        if not math.isfinite(value):
            continue  # NaN/inf 不是读数（评审 LOW-1：NaN 进样本会传染 ICIR 却仍标 ok）
        pairs.append((day, value))
    if not pairs or end is None:
        return MonthReading(end_s, 0, None, READING_NO_SERIES, None)

    window_start = end - timedelta(days=window_days)
    vals = [v for d, v in pairs if window_start < d <= end]

    # 陈旧闸只认 ≤ month_end 的数据（评审 LOW-1：docstring 与回填确定性都
    # 这么承诺，旧实现却取全序列最大日——序列只有月末之后的数据时不判陈旧）。
    series_max = max((d for d, _ in pairs if d <= end), default=None)
    if series_max is None or (end - series_max).days > stale_days:
        return MonthReading(end_s, len(vals), None, READING_SERIES_STALE, None)
    if len(vals) < min_n_days:
        return MonthReading(end_s, len(vals), None, READING_TOO_FEW, None)

    n = len(vals)
    mean = sum(vals) / n
    if n < 2:
        return MonthReading(end_s, n, None, READING_FLAT, None)
    var = sum((v - mean) ** 2 for v in vals) / (n - 1)
    std = math.sqrt(var)
    if std == 0.0:
        # 常数序列（含全 0——回测数据坏味的迹象）：ICIR 无定义，交人看。
        return MonthReading(end_s, n, None, READING_FLAT, None)
    icir = mean / std
    return MonthReading(end_s, n, icir, READING_OK, icir < icir_floor)


def streak_of(readings: Sequence[MonthReading]) -> int:
    """连击 = 从最新一期往回**连续**「有效且弱」读数个数。

    任一非弱（回升）或不可算读数即断（保守方向：保护因子，不误降权）。
    入参须按 month_end 升序（台账 upsert 保证）。
    """
    streak = 0
    for reading in reversed(list(readings)):
        if reading.status == READING_OK and reading.below_floor:
            streak += 1
        else:
            break
    return streak


def verdict_of(
    readings: Sequence[MonthReading],
    *,
    blind_until: datetime | None,
    needed: int = STREAK_MONTHS,
) -> dict[str, Any]:
    """裁决：hold / observing_protected（盲窗保护）/ downweight。

    ``readings`` 升序；裁决参考点 = 最新一期月末。连击达 ``needed`` 且该
    月末 ≤ 盲窗终点**当日** → 降级为保护（记录不动作）。同日判保护是刻意
    的保守口径（评审 MEDIUM-1）：盲窗可能在该日任意时刻才到期，旧日粒度
    ``<`` 会在窗口真正结束前数小时就放行降权。
    """
    ordered = list(readings)
    streak = streak_of(ordered)
    latest = ordered[-1] if ordered else None
    if streak < needed:
        decision = DECISION_HOLD
    else:
        latest_day = _to_date(latest.month_end) if latest else None
        protected = (
            blind_until is not None
            and latest_day is not None
            and latest_day <= as_utc(blind_until).date()
        )
        decision = DECISION_OBSERVING_PROTECTED if protected else DECISION_DOWNWEIGHT
    return {
        "streak": streak,
        "decision": decision,
        "latest": latest.as_dict() if latest else None,
    }


def month_ends_back(asof: date, count: int) -> list[date]:
    """最近 ``count`` 个**已走完**自然月的月末（升序）。

    asof 恰为月末 → 含当月；否则从上一月末起。跨年/闰月由 calendar 兜。
    """
    if count <= 0:
        return []
    year, month = asof.year, asof.month
    last = date(year, month, calendar.monthrange(year, month)[1])
    if last > asof:
        month -= 1
        if month == 0:
            year, month = year - 1, 12
        last = date(year, month, calendar.monthrange(year, month)[1])
    ends: list[date] = []
    for _ in range(count):
        ends.append(last)
        month -= 1
        if month == 0:
            year, month = year - 1, 12
        last = date(year, month, calendar.monthrange(year, month)[1])
    return ends[::-1]


def lifecycle_view(
    row: Mapping[str, Any],
    *,
    first_completed_run_at: datetime | None,
    now: datetime,
    has_active_validation: bool = False,
) -> dict[str, Any]:
    """因子行 → 生命周期视图（API 装配用；纯函数）。

    返回 ``{state, maturity, blind_until, last_eval}``：
    - ``state`` 走 :func:`resolve_state`（显式覆盖优先）；
    - ``blind_until`` = 盲窗终点（UTC ISO ``Z`` 结尾）；行与 run 都没时间
      锚点时 None；
    - ``last_eval``：评估器最近读数快照（``metadata.lifecycle.last_eval``）；
      **盲窗内（observing）不返回**——OOS 读数对研究员面不可见（§1.2）。
    """
    meta = read_metadata(row)
    life = meta.get("lifecycle")
    life = life if isinstance(life, Mapping) else {}

    created_at = row.get("created_at")
    if first_completed_run_at is None and created_at is None:
        blind_until: datetime | None = None
    else:
        blind_until = blind_until_of(
            created_at=created_at, first_completed_run_at=first_completed_run_at
        )

    state = resolve_state(
        explicit_state=life.get("state"),
        has_completed_run=has_completed_backtest(
            row, first_completed_run_at=first_completed_run_at
        ),
        blind_until=blind_until,
        now=now,
    )
    last_eval = None
    if state != STATE_OBSERVING:
        raw_eval = life.get("last_eval")
        last_eval = dict(raw_eval) if isinstance(raw_eval, Mapping) else None
    return {
        "state": state,
        "maturity": maturity_of(state, has_active_validation=has_active_validation),
        "blind_until": to_utc_iso(blind_until) if blind_until is not None else None,
        "last_eval": last_eval,
    }


def redact_blind_readings(
    metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """元数据脱敏：剥离 ``lifecycle`` 下评估读数键（last_eval/evidence）。

    装配层专用（评审 security-MEDIUM，2026-10-11）：``metadata_json`` 与
    ``lifecycle`` 视图是同源数据的两个出口，只裁视图字段等于没裁——盲窗内
    （observing）或生命周期证据降级时，API 行的 metadata 必须走这里过一遍。
    **返回新 dict、输入不可变**（持久层证据原地保留）；无 lifecycle 键或
    非 Mapping 输入原样搬入新 dict（None → ``{}``）。
    """
    out: dict[str, Any] = dict(metadata) if isinstance(metadata, Mapping) else {}
    life = out.get("lifecycle")
    if isinstance(life, Mapping):
        out["lifecycle"] = {
            key: value for key, value in life.items() if key not in _BLIND_READING_KEYS
        }
    return out


# ------------------------------------------------------------------ 台账 IO


def load_ledger(path: Path | str) -> dict[str, Any]:
    """读台账；缺失/坏 JSON/结构不符一律自愈为空骨架（不抛）。"""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("ledger root not a dict")
    except (OSError, ValueError):
        return {
            "schema_version": LEDGER_SCHEMA_VERSION,
            "updated_at": None,
            "factors": {},
        }
    raw.setdefault("schema_version", LEDGER_SCHEMA_VERSION)
    if not isinstance(raw.get("factors"), dict):
        raw["factors"] = {}
    return raw


def save_ledger(path: Path | str, ledger: dict[str, Any]) -> None:
    """原子落盘：同目录 tmp 写完 ``os.replace``（读者永不见半截 JSON）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(
        json.dumps(ledger, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    os.replace(tmp, target)


def upsert_readings(
    ledger: dict[str, Any],
    factor_id: str,
    readings: Sequence[MonthReading],
    *,
    run_id: str,
    now: datetime,
) -> dict[str, Any]:
    """按 month_end 幂等并入读数（同月末替换、升序、剪最旧到 24 期）。

    返回**新** ledger（入参不变）；每条读数带 run_id/at 追溯来源。
    """
    updated = dict(ledger)
    factors = dict(updated.get("factors") or {})
    entry = dict(factors.get(factor_id) or {})
    by_month: dict[str, dict[str, Any]] = {
        str(r.get("month_end")): dict(r) for r in (entry.get("readings") or [])
    }
    stamp = to_utc_iso(now)
    for reading in readings:
        item = reading.as_dict() if isinstance(reading, MonthReading) else dict(reading)
        item["run_id"] = run_id
        item["at"] = stamp
        by_month[str(item.get("month_end"))] = item
    merged = sorted(by_month.values(), key=lambda r: str(r.get("month_end")))
    entry["readings"] = merged[-LEDGER_MAX_READINGS:]
    entry["run_id"] = run_id
    factors[factor_id] = entry
    updated["factors"] = factors
    updated["updated_at"] = stamp
    return updated


def append_event(
    ledger: dict[str, Any],
    factor_id: str,
    *,
    from_state: str | None,
    to_state: str | None,
    reason: str,
    evidence: Mapping[str, Any] | None = None,
    now: datetime,
) -> dict[str, Any]:
    """状态变更史并入（不可变；最近 :data:`LEDGER_MAX_EVENTS` 条、保序）。

    只在**动作真的发生**时追加（``--apply`` 的降权迁移）；事件与读数在
    entry 里并行不互挤（``readings`` 键原样保留）。
    """
    updated = dict(ledger)
    factors = dict(updated.get("factors") or {})
    entry = dict(factors.get(factor_id) or {})
    events = list(entry.get("events") or [])
    events.append(
        {
            "at": to_utc_iso(now),
            "from": from_state,
            "to": to_state,
            "reason": str(reason),
            "evidence": dict(evidence) if evidence else None,
        }
    )
    entry["events"] = events[-LEDGER_MAX_EVENTS:]
    factors[factor_id] = entry
    updated["factors"] = factors
    updated["updated_at"] = to_utc_iso(now)
    return updated
