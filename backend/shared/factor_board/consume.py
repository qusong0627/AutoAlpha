"""因子概率板 → 决策轮候选池**只读叠加**（P3，细案 §十一）。

交付态=关（`QM_DECISION_BOARD_OVERLAY` 默认为假）；开关关 = 决策轮行为与现状
逐字节一致（包装层直接走原 ``load_pool_doc``，本模块不 import 不执行）。

硬边界（本期，逐条对应细案）：

- **只标注 + 稳定重排**：共振靠前（HH）前置、共振靠后（LL）后置、其余保持文件序；
  **不新增行、不改 score、不改 rank**（`PoolRow` 是 frozen，标注走 dataclasses.replace）。
- **不并名单**：共振标的**绝不**获得新开仓资格——``pool_codes`` / ``l2.pool_not_member``
  的输入集合本模块一字不碰；「直接获得资格」是后续独立评审 + 独立开关的事。
- **无判定 = 不过滤**：板缺失/坏 JSON/池行不在板名单内 → 原样保留（叠加是增益，
  绝不让决策轮因板缺席而改行为）；标签词只说位置（靠前/靠后），不写买卖。
- **时点差透明化**：overlay 携带 ``decision_date`` 与 ``stale_days``（板决策日距池日），
  超 ``OVERLAY_STALE_DAYS`` → ``note`` 记录，由包装层打日志（不静默）。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date as _date
from pathlib import Path

from backend.shared.decision.context import PoolRow
from backend.shared.factor_board import board_io as IO
from backend.shared.factor_board import config as C
from backend.shared.factor_board import sectors as S

log = logging.getLogger("factor_board.consume")

#: 开关名（`env_flags.env_flag` 读取；缺省关）。运维文档与包装层都引用本常量。
OVERLAY_FLAG = "QM_DECISION_BOARD_OVERLAY"

#: 板 decision_date 距池日超过此天数 → note 注记（板链路可能断更）。
OVERLAY_STALE_DAYS = 3

#: 备注标签（展示面纪律：只说位置）。
TAG_LONG = "共振·靠前"
TAG_SHORT = "共振·靠后"

#: 稳定排序类别键（小在前；类内 stable sort 保持文件序）。
_CAT_LONG, _CAT_PLAIN, _CAT_SHORT = 0, 1, 2


@dataclass(frozen=True, slots=True)
class OverlayEntry:
    """池内一只在板名单里的读法（键 = suffix 代码，与 ``PoolRow.code`` 同口径）。"""

    verdict: str
    quadrant: str
    composite: float | None
    model_rank: float | None


@dataclass(frozen=True, slots=True)
class Overlay:
    """板索引 + 时点信息。``entries`` 覆盖板内四个名单（top/bottom/共振多/共振空）行。"""

    entries: Mapping[str, OverlayEntry]
    decision_date: str | None
    stale_days: int | None
    note: str


def load_overlay(
    board_dir: Path | None = None, *, as_of: str | None = None
) -> Overlay | None:
    """读板产物（latest.json）→ 索引；板缺失/坏 JSON → ``None``（缺板不是故障）。

    ``as_of``：池日（'YYYYMMDD' 或 ISO）。给了才计算 ``stale_days``（板决策日距池日，
    可为负——板批的目标日可能晚于池日，属正常时点差，不算陈旧）。
    """
    out_dir = C.resolve_board_dir(board_dir)
    board = IO.load_board(out_dir)
    if board is None:
        return None

    stocks = board.get("stocks") or {}

    def _entries(key: str, verdict: str) -> list[tuple[str, OverlayEntry]]:
        out = []
        for it in stocks.get(key) or []:
            sym = str((it or {}).get("symbol") or "").strip()
            if not sym:
                continue
            out.append(
                (
                    sym,
                    OverlayEntry(
                        verdict=verdict,
                        quadrant=str(it.get("quadrant") or ""),
                        composite=it.get("composite"),
                        model_rank=it.get("model_rank"),
                    ),
                )
            )
        return out

    entries: dict[str, OverlayEntry] = {}
    # 共振名单优先建条目（同名冲突时共振判定胜过中性名单）
    for key, verdict in (
        ("resonant_long", S.VERDICT_LONG),
        ("resonant_short", S.VERDICT_SHORT),
        ("top", S.VERDICT_NEUTRAL),
        ("bottom", S.VERDICT_NEUTRAL),
    ):
        for sym, entry in _entries(key, verdict):
            entries.setdefault(sym, entry)

    decision_date = str((board.get("meta") or {}).get("decision_date") or "").strip()
    decision_date = decision_date or None
    stale_days, note = _staleness(decision_date, as_of)
    return Overlay(
        entries=entries, decision_date=decision_date, stale_days=stale_days, note=note
    )


def _staleness(decision_date: str | None, as_of: str | None) -> tuple[int | None, str]:
    """板决策日 - 池日 → (stale_days, note)。日期解析不动 → (None, "")。"""
    if not decision_date or not as_of:
        return None, ""
    try:
        d0 = _date.fromisoformat(decision_date)
        raw = str(as_of).strip()
        d1 = (
            _date.fromisoformat(raw)
            if "-" in raw
            else _date.fromisoformat(f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}")
        )
    except ValueError:
        return None, ""
    days = (d1 - d0).days
    if days > OVERLAY_STALE_DAYS:
        return days, (
            f"板陈旧：decision_date={decision_date} 距池日 {as_of} 已 {days} 天"
            f"（>{OVERLAY_STALE_DAYS}），叠加按旧证据使用"
        )
    return days, ""


def annotate_pool(
    rows: Sequence[PoolRow], overlay: Overlay | None
) -> tuple[PoolRow, ...]:
    """只标注 + 稳定排序（共振靠前前置、共振靠后后置）；``overlay=None`` → 原样。

    行数、``code``/``name``/``score``/``fusion``/``rank`` 全部不动；被标注行的
    ``remark`` 为「原备注｜标签」或「标签」（原备注为空时）。
    """
    if overlay is None or not rows:
        return tuple(rows)
    tagged: list[tuple[int, PoolRow]] = []
    for row in rows:
        entry = overlay.entries.get(str(row.code or "").strip())
        if entry is None or entry.verdict == S.VERDICT_NEUTRAL:
            tagged.append((_CAT_PLAIN, row))
            continue
        tag = TAG_LONG if entry.verdict == S.VERDICT_LONG else TAG_SHORT
        cat = _CAT_LONG if entry.verdict == S.VERDICT_LONG else _CAT_SHORT
        remark = f"{row.remark}｜{tag}" if row.remark else tag
        tagged.append((cat, replace(row, remark=remark)))
    tagged.sort(key=lambda item: item[0])  # stable：类内保持文件序
    return tuple(row for _, row in tagged)
