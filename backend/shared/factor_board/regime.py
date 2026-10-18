"""因子概率板：regime 分桶权重（P2-c，状态化权重，**只降不翻**）。

- **数据源**：``qm_regime_daily`` 冻结表（``regime_daily_store.load_states_sync``），
  当前状态 = trade_date 当日行；缺失 → 取 ≤trade_date 最近一行并记 ``state_asof``
  （透明化滞后）。**不使用** ``qm:regime:intraday``（盘中值，冻结口径不混用）。
- **长窗**：``REGIME_LOOKBACK`` 个交易日；仅对**入选因子**算逐日方向对齐 IC
  （h=5，窗尾留 5 个未评估日）——分桶谓词复用 ``regime_buckets``（唯一实现），
  **禁用** factor_regime_analysis 的 MA200 标签体系（口径不同）。
- **乘子**（保守双档）：当前状态桶 ``is_weak_bucket``（mean_ic≤0 ∧ n≥15）→
  ``REGIME_WEAK_MULT``；桶存在非弱 → 1.0；桶空/样本不足（含 bear 空桶）→
  1.0 + note=insufficient。**绝不做按 regime 翻方向**（20–250 日样本上翻转 =
  过拟合陷阱）。
- **降级**：DB 不可达 / 表空 → 整块 ``None`` + warning，权重全不动——
  产板**永不因 regime 失败**。
"""

from __future__ import annotations

import logging
import math
from pathlib import Path

import pandas as pd

from backend.shared.factor_board import composite as CP
from backend.shared.factor_board import config as C
from backend.shared.factor_board import selection as SEL
from backend.shared.regime_buckets import bucket_of, is_weak_bucket

log = logging.getLogger("factor_board.regime")


def _iso(dt: str) -> str:
    """dt 分区名 'YYYYMMDD' → ISO 'YYYY-MM-DD'（regime 表键口径）。"""
    return f"{dt[:4]}-{dt[4:6]}-{dt[6:]}"


def build_regime_weights(
    quantdb_root: Path,
    sel: pd.DataFrame,
    *,
    market: str = C.REGIME_MARKET,
) -> tuple[pd.DataFrame, dict | None, list[str]]:
    """对入选表算 regime 乘子 → ``(sel', block | None, warnings)``。

    ``sel'`` 恒有 ``w_raw/regime_mult/regime_ic/regime_n/regime_note`` 列，
    ``w = w_raw × regime_mult``（降级时乘子恒 1.0，列照出——前端无需分支）。
    调用方须保证 ``sel`` 带 ``lib/name/sgn/w``（select_factors 出口）。
    """
    out = sel.copy()
    out["w_raw"] = out["w"].astype(float)
    out["regime_mult"] = 1.0
    out["regime_ic"] = None
    out["regime_n"] = 0
    out["regime_note"] = "unavailable"
    warnings: list[str] = []

    def _degrade(msg: str) -> tuple[pd.DataFrame, None, list[str]]:
        log.warning("regime 降级：%s", msg)
        return out, None, [*warnings, msg]

    try:
        dates_all = CP.list_factor_dates(quantdb_root)
    except FileNotFoundError as exc:
        return _degrade(f"regime：因子库缺失（{exc}）")
    rdates = dates_all[-C.REGIME_LOOKBACK :]
    if len(rdates) < C.REGIME_MIN_DAYS * 2:  # 长窗连两个弱桶样本都凑不出 → 不硬算
        return _degrade(f"regime 长窗不足（{len(rdates)} 日），权重全不动")
    close = CP.load_close_matrix(quantdb_root, rdates)
    if close.empty:
        return _degrade("regime 收盘矩阵为空，权重全不动")
    fwd5 = CP.fwd_ranks(close, rdates, range(0, len(rdates) - 5), 5)
    if not fwd5:
        return _degrade("regime 无可用评估日，权重全不动")

    since_iso, until_iso = _iso(rdates[0]), _iso(rdates[-1])
    try:
        from backend.shared.regime_daily_store import load_states_sync

        states = load_states_sync(market, since=since_iso, until=until_iso)
    except Exception as exc:  # noqa: BLE001 — DB 不可达不拦产板
        return _degrade(f"regime 状态表不可达（{exc}），权重全不动")
    if not states:
        return _degrade(
            f"regime 状态表空（{market} {since_iso}~{until_iso}），权重全不动"
        )
    state = states.get(until_iso)
    if state is None:  # 当日缺行 → 取 ≤trade_date 最近一行（透明化滞后）
        asof = max(states)
        state = states[asof]
        lag_days = int((pd.Timestamp(until_iso) - pd.Timestamp(asof)).days)
    else:
        asof, lag_days = until_iso, 0

    cand = sel[["lib", "name"]].drop_duplicates()
    acc, _, days_by_lib = SEL.compute_ic_table(
        quantdb_root, cand, list(fwd5.keys()), fwd5
    )
    sgn_map = {(r.lib, r.name): int(r.sgn) for r in sel.itertuples()}
    points_by_key: dict[tuple[str, str], list[tuple[str, float]]] = {}
    for lib, n in zip(cand["lib"], cand["name"], strict=True):
        days = days_by_lib.get(lib, [])
        vals = acc[(lib, n)]
        if len(days) != len(vals):  # 结构不变量（append 成对），破坏了不许静默
            return _degrade(f"regime IC 日配对长度不一致（{lib}/{n}），权重全不动")
        sgn = sgn_map[(lib, n)]
        points_by_key[(lib, n)] = [
            (_iso(dt), sgn * float(v))
            for dt, v in zip(days, vals, strict=True)
            if v is not None and math.isfinite(float(v))
        ]

    mults: dict[tuple[str, str], float] = {}
    notes: dict[tuple[str, str], str] = {}
    stats: dict[tuple[str, str], dict] = {}
    for key, pts in points_by_key.items():
        b = bucket_of(pts, states, state)
        stats[key] = b
        n_days = int(b.get("n_days") or 0)
        if is_weak_bucket(b):  # 唯一弱桶谓词（mean_ic≤0 ∧ n≥15）
            mults[key], notes[key] = C.REGIME_WEAK_MULT, "weak"
        elif n_days < C.REGIME_MIN_DAYS:
            mults[key], notes[key] = 1.0, "insufficient"
        else:
            mults[key], notes[key] = 1.0, "ok"

    keys_by_group = {
        grp: [(r.lib, r.name) for r in g.itertuples()]
        for grp, g in sel.groupby("group")
    }
    by_family: dict[str, dict] = {}
    for grp, keys in keys_by_group.items():
        per_day: dict[str, list[float]] = {}
        for k in keys:
            for d, v in points_by_key.get(k, []):
                per_day.setdefault(d, []).append(v)
        fam_points = sorted((d, sum(vs) / len(vs)) for d, vs in per_day.items())
        fb = bucket_of(fam_points, states, state)
        by_family[str(grp)] = {
            "state_ic": fb["mean_ic"],
            "n_days": int(fb["n_days"]),
            "mult": min(mults[k] for k in keys),
        }

    weak_keys = [k for k, m in mults.items() if m != 1.0]
    block = {
        "market": market,
        "state": state,
        "state_asof": asof,
        "state_lag_days": lag_days,
        "lookback_days": len(rdates),
        "eval_days": len(fwd5),
        "bucket_n": sum(1 for s in states.values() if s == state),
        "weak_count": len(weak_keys),
        "adjusted_count": int(sum(1 for m in mults.values() if m != 1.0)),
        "weak_mult": C.REGIME_WEAK_MULT,
        "by_family": by_family,
    }
    if weak_keys:
        msg = (
            f"regime：{len(weak_keys)} 个因子在当前状态 {state} 为弱桶，"
            f"权重 ×{C.REGIME_WEAK_MULT}"
        )
        warnings.append(msg)
        log.warning("%s：%s", msg, [f"{lib}/{n}" for lib, n in weak_keys])

    out["regime_mult"] = [mults[(r.lib, r.name)] for r in sel.itertuples()]
    out["regime_ic"] = [stats[(r.lib, r.name)]["mean_ic"] for r in sel.itertuples()]
    out["regime_n"] = [int(stats[(r.lib, r.name)]["n_days"]) for r in sel.itertuples()]
    out["regime_note"] = [notes[(r.lib, r.name)] for r in sel.itertuples()]
    out["w"] = out["w_raw"].astype(float) * out["regime_mult"].astype(float)
    return out, block, warnings
