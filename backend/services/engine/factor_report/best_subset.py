"""K 因子最佳子集：按「对组合 Sharpe 的边际贡献」选 10–15 个（T-FE-02）。

细案：``docs/因子挖掘_T-FE-02_K因子最佳子集_细案_20261011.md``。核心口径：

- **收益空间 = 带符号净日收益** ``r_f(t) = dir_f·ls_h(t)/h − cost_f(t)``。
  为什么不能用 ``_series_stats`` 的 ``|ls|/h``：绝对值只服务「均值级净收益闸」，
  均值上等价；但 Sharpe 是二阶矩口径，|ls| 会把每日负值翻正、抹掉下行波动 →
  造出假 Sharpe。选择引擎必须用带符号序列。
- **子集内等权**（论文：<1000 交易日等权 1/N 是稳健默认，不做 Σ⁻¹μ 二次优化）；
  方向在因子级（来自 IC 符号），不在子集内做权重优化。
- **贪心前向 + 1-swap（best-improvement）**：基数约束近似算法，非随机启发式，
  同数据同结果；全程只跑训练窗（train 70% / holdout 30%），留出窗只做披露——
  防「选择过程偷看 OOS」。
- K* = 训练窗 Sharpe argmax、平票取小；train/holdout Sharpe 比 >3 亮过拟合旗标
  （披露而非自动否决——这是研究工具，判断权交用户）。
- **披露纪律**：候选整列缺席（快照无数据）/短历史剔除/比例不可计算（holdout≤0）/
  朴素切分的前向标签窗重叠——一律进 ``notes`` 与 ``universe``，不静默蒸发。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from .optimize import (
    DEFAULT_HOLDING_DAYS,
    DEFAULT_ROUND_TRIP_COST,
    turnover_cost_daily,
)
from .service import dataset_dir, normalize_dataset

log = logging.getLogger(__name__)

K_MIN_DEFAULT = 10
"""K 下限（台账规格：10–15 个）。候选不足时区间向右收缩，见 ``compute_best_subset``。"""

K_MAX_DEFAULT = 15
"""K 上限（台账规格）。"""

TRAIN_FRAC = 0.7
"""时序切分：前 70% 训练（选择只看这里），后 30% 留出（只做披露）。"""

ANNUALIZATION = 252
"""Sharpe 年化系数。"""

MAX_SWAP_PASSES = 8
"""1-swap 局部改进的轮数上限（best-improvement，每轮至少换一员且严格改善）。"""

MIN_TOTAL_DAYS = 120
"""总样本最少天数：低于此不做选择（holdout 窗也要够评估）。"""

MIN_FACTOR_DAYS = 60
"""因子在训练窗的最少有效天：短历史因子从候选中剔除并计数披露。"""

MIN_CANDIDATES = 3
"""最少候选数：低于此返回 ``available: false``。"""

MIN_SHARPE_DAYS = 20
"""单窗口 Sharpe 的最少有效天数（与 optimize.MIN_COMPOSITE_DAYS 同精神）。"""

RELATIVE_SD_FLOOR = 1e-9
"""SD 相对量级下界：sd ≤ 量级×此值视为浮点噪声级常数——见 ``_sharpe``。"""

OVERFIT_ALERT_RATIO = 3.0
"""train/holdout Sharpe 比超过此值亮过拟合旗标（论文的样本内外 3× 判据）。"""

SMALL_SAMPLE_DAYS = 1000
"""低于 1000 交易日时按论文结论提示「等权 1/N 是稳健默认」。"""


def _r4(v: Any) -> float | None:
    """JSON 纪律：非有限/None → None；有限 → round 4 位。"""
    if v is None:
        return None
    f = float(v)
    return round(f, 4) if np.isfinite(f) else None


def _unavailable(reason: str) -> dict[str, Any]:
    return {"available": False, "reason": reason}


def _sharpe(series, annualization: float = ANNUALIZATION) -> float | None:
    """等权序列的 Sharpe：mean/std×√ann（ddof=0，与 optimize.composite_ic_series 同口径）。

    近常数护栏：sd 低于「序列量级×``RELATIVE_SD_FLOOR``」视为浮点噪声级常数
    （如 ULP 级扰动，std≈4e-19）→ None——否则 mean/sd 爆炸出 1e16 级假 Sharpe
    劫持贪心选择。真实净收益序列的 sd/量级 ≳1e-2，护栏留 7 个数量级余量。
    """
    s = np.asarray(series, dtype=np.float64)
    s = s[np.isfinite(s)]
    if s.size < MIN_SHARPE_DAYS:
        return None
    sd = float(s.std())
    if sd <= 0.0 or sd <= float(np.abs(s).max()) * RELATIVE_SD_FLOOR:
        return None
    v = float(s.mean()) / sd * np.sqrt(annualization)
    return v if np.isfinite(v) else None


def _greedy_path(
    R: np.ndarray, k_max: int, annualization: float = ANNUALIZATION
) -> list[int]:
    """贪心前向：每步选「加入后组合 Sharpe 提升最大」的列（边际贡献的字面实现）。

    严格 ``>`` 使平票取先出现者（按列序）——确定性，同数据同结果。
    返回按选择顺序排列的列索引，长度 ``min(k_max, n)``。
    """
    R = np.asarray(R, dtype=np.float64)
    n = R.shape[1]
    sel: list[int] = []
    sel_sum = np.zeros(R.shape[0], dtype=np.float64)
    remaining = list(range(n))
    for _ in range(min(k_max, n)):
        best_j, best_v = None, None
        for j in remaining:
            v = _sharpe((sel_sum + R[:, j]) / (len(sel) + 1), annualization)
            if v is None:
                continue
            if best_v is None or v > best_v:
                best_j, best_v = j, v
        if best_j is None:
            break
        sel.append(best_j)
        remaining.remove(best_j)
        sel_sum = sel_sum + R[:, best_j]
    return sel


def _swap_refine(
    R: np.ndarray,
    sel: list[int],
    *,
    annualization: float = ANNUALIZATION,
    max_passes: int = MAX_SWAP_PASSES,
) -> list[int]:
    """1-swap best-improvement：换入一个候选换出一个成员，train Sharpe 严格提升才接受。

    每轮取全局最优的单换（先列序先胜），最多 ``max_passes`` 轮。确定性、可复现。
    """
    R = np.asarray(R, dtype=np.float64)
    sel = list(sel)
    if not sel:
        return sel
    k = len(sel)
    sel_sum = R[:, sel].sum(axis=1)
    cur = _sharpe(sel_sum / k, annualization)
    for _ in range(max_passes):
        best, best_v = None, cur
        for i in range(k):
            base = sel_sum - R[:, sel[i]]
            for j in range(R.shape[1]):
                if j in sel:
                    continue
                v = _sharpe((base + R[:, j]) / k, annualization)
                if v is None:
                    continue
                if best_v is None or v > best_v + 1e-12:  # 1e-12：防浮点抖动空转
                    best, best_v = (i, j), v
        if best is None:
            break
        i, j = best
        sel_sum = sel_sum - R[:, sel[i]] + R[:, j]
        sel[i] = j
        cur = best_v
    return sel


def _choose_k(curve: list[dict[str, Any]]) -> int | None:
    """K* = train Sharpe 最大的 k；平票取小（严格 > 只认先出现者）；全 None → None。"""
    best_k, best_v = None, None
    for row in curve:
        v = row.get("train_sharpe")
        if v is None:
            continue
        if best_v is None or v > best_v:
            best_k, best_v = int(row["k"]), float(v)
    return best_k


def load_net_returns(
    dataset: str,
    factors: list[str],
    *,
    directions: dict[str, int] | None = None,
    holding_days: int = DEFAULT_HOLDING_DAYS,
    round_trip_cost: float = DEFAULT_ROUND_TRIP_COST,
    series_path: Path | None = None,
) -> pd.DataFrame | None:
    """读序列快照 → 各因子**方向对齐 + 扣费后**的净日收益宽表（index=date）。

    ``series_path`` 显式给出截面文件（portfolio 集成走自己的 dataset_dir 解析，
    便于测试注入）；缺省按 ``dataset`` 经 ``dataset_dir`` 解析。

    降级语义（返回 None，调用方披露原因）：快照文件缺失、缺 ``ls_{holding_days}``
    列、候选一个都不在表里。缺 turnover 列按 0 成本；ls 的 ±inf/NaN 保留为 NaN
    （compute 侧：单因子缺失填 0，全因子缺失的日期行剔除——前向标签未完成的尾日）。
    """
    if series_path is None:
        series_path = (
            dataset_dir(normalize_dataset(dataset)) / "report" / "factor_series.parquet"
        )
    if not series_path.exists():
        return None
    schema_names = set(pq.ParquetFile(series_path).schema_arrow.names)
    ls_col = f"ls_{holding_days}"
    if ls_col not in schema_names:
        return None
    cols = ["factor", "date", ls_col]
    has_turnover = "turnover" in schema_names
    if has_turnover:
        cols.append("turnover")
    df = pq.read_table(series_path, columns=cols).to_pandas()
    df = df[df["factor"].isin(set(factors))]
    if df.empty:
        return None
    # 防御：分位列可能混入 ±inf（早年近零/负价的 forward return），统一打回 NaN
    df[ls_col] = df[ls_col].where(np.isfinite(df[ls_col].astype(float)))
    if has_turnover:
        df["turnover"] = df["turnover"].where(np.isfinite(df["turnover"].astype(float)))
    piv_ls = df.pivot_table(
        index="date", columns="factor", values=ls_col, observed=True
    )
    piv_to = (
        df.pivot_table(index="date", columns="factor", values="turnover", observed=True)
        if has_turnover
        else None
    )
    # pivot_table 默认丢掉「整行全 NaN」的日期：ls 是前向标签，最新 h 个交易日
    # 所有因子都还没有完成的前向窗（实测 rd_mined 尾部 19 行）——两张 pivot 的
    # 行数因此可差 h 天。显式对齐到全量日期：缺处留 NaN（compute 侧剔除全缺行）。
    dates = pd.Index(sorted(df["date"].dropna().unique()))
    piv_ls = piv_ls.reindex(dates)
    if piv_to is not None:
        piv_to = piv_to.reindex(dates)

    # 整列缺席的候选（快照里没有该因子的行）不得静默消失：服务端留痕，
    # 用户面披露由 compute_best_subset 的 requested 参数负责（universe/notes）。
    missing = [name for name in factors if name not in piv_ls.columns]
    if missing:
        log.warning(
            "候选 %d 个在因子序列快照中无数据（未进精选子集）：%s",
            len(missing),
            missing[:10],
        )

    dirs = directions or {}
    out: dict[str, pd.Series] = {}
    for name in factors:
        if name not in piv_ls.columns:
            continue
        sgn = float(dirs.get(name, 1))
        if piv_to is not None and name in piv_to.columns:
            cost = turnover_cost_daily(piv_to[name], holding_days, round_trip_cost)
        else:
            cost = np.zeros(len(piv_ls), dtype=np.float64)
        out[name] = sgn * piv_ls[name].astype(float) / holding_days - cost
    if not out:
        return None
    return pd.DataFrame(out)


def compute_best_subset(
    returns: pd.DataFrame,
    *,
    directions: dict[str, int] | None = None,
    current_rule: tuple[list[str], np.ndarray] | None = None,
    requested: list[str] | None = None,
    k_min: int = K_MIN_DEFAULT,
    k_max: int = K_MAX_DEFAULT,
    train_frac: float = TRAIN_FRAC,
    annualization: float = ANNUALIZATION,
    max_swap_passes: int = MAX_SWAP_PASSES,
    overfit_alert_ratio: float = OVERFIT_ALERT_RATIO,
    holding_days: int = DEFAULT_HOLDING_DAYS,
    round_trip_cost: float = DEFAULT_ROUND_TRIP_COST,
) -> dict[str, Any]:
    """在**已方向对齐 + 扣费**的净日收益宽表上选 K 因子最佳子集。

    ``returns``：index=日期，列=因子名（``load_net_returns`` 的输出）。
    ``current_rule``：现行规则的 ``(names, weights)``（带符号），权重按在场列
    ``|w|`` 归一后合成基准序列（``r`` 已含方向，权重取 |w|）。
    ``requested``：调用方请求的候选全集（快照可能整列缺数据）——给出时，
    缺席者在 ``universe`` 与 ``notes`` 如实披露，不从计数里静默蒸发。
    永不抛异常（除输入类型错误）；不可用情形返回 ``{available: false, reason}``。
    """
    df = returns.sort_index()
    # 全因子皆缺的日期行 = 前向标签未完成/无数据（如最新 h 个交易日），剔除而非
    # 当 0 收益日——否则零收益零波动日会稀释 Sharpe（单因子缺失仍按 0 处理）
    df = df.dropna(how="all")
    total_days = int(df.shape[0])
    if total_days < MIN_TOTAL_DAYS:
        return _unavailable(f"样本天数不足 {MIN_TOTAL_DAYS}（{total_days}）")

    split = int(total_days * train_frac)
    valid_train = df.iloc[:split].notna().sum()
    kept = [c for c in df.columns if int(valid_train.get(c, 0)) >= MIN_FACTOR_DAYS]
    n_excluded = int(df.shape[1]) - len(kept)
    # 请求了但整列不在 returns 里的候选（快照无数据）——与「在表但有效天不足」区分计数
    absent = sorted(set(requested) - set(df.columns)) if requested else []
    if len(kept) < MIN_CANDIDATES:
        return _unavailable(f"候选不足 {MIN_CANDIDATES} 个（{len(kept)}）")

    # K 区间随候选数退化并如实标注（K_min=min(10,n)、K_max=min(15,n)）
    k_hi = min(k_max, len(kept))
    k_lo = min(k_min, k_hi)
    R = df[kept].fillna(0.0).to_numpy(dtype=np.float64)
    R_tr, R_ho = R[:split], R[split:]

    greedy = _greedy_path(R_tr, k_hi, annualization)
    curve: list[dict[str, Any]] = []
    refined: dict[int, list[int]] = {}
    for k in range(k_lo, k_hi + 1):
        sel = _swap_refine(
            R_tr, greedy[:k], annualization=annualization, max_passes=max_swap_passes
        )
        refined[k] = sel
        curve.append(
            {
                "k": k,
                "train_sharpe": _r4(_sharpe(R_tr[:, sel].mean(axis=1), annualization)),
                "holdout_sharpe": _r4(
                    _sharpe(R_ho[:, sel].mean(axis=1), annualization)
                ),
            }
        )

    k_star = _choose_k(curve)
    if k_star is None:
        return _unavailable("训练窗内无法评估组合 Sharpe（有效序列不足）")
    row = curve[k_star - k_lo]
    sel = refined[k_star]
    chosen = {
        "k": k_star,
        "train_sharpe": row["train_sharpe"],
        "holdout_sharpe": row["holdout_sharpe"],
        "full_sharpe": _r4(_sharpe(R[:, sel].mean(axis=1), annualization)),
    }

    eqw_series = R.mean(axis=1)
    benchmarks: dict[str, Any] = {
        "equal_weight_all": {
            "n_factors": len(kept),
            "train_sharpe": _r4(_sharpe(eqw_series[:split], annualization)),
            "holdout_sharpe": _r4(_sharpe(eqw_series[split:], annualization)),
            "full_sharpe": _r4(_sharpe(eqw_series, annualization)),
        },
    }
    cur_block = None
    if current_rule is not None:
        cr_names = list(current_rule[0])
        cr_w = np.asarray(current_rule[1], dtype=np.float64).ravel()
        pos = {name: i for i, name in enumerate(kept)}
        picked = [
            (pos[name], abs(float(wv)))
            for name, wv in zip(cr_names, cr_w, strict=False)
            if name in pos and np.isfinite(float(wv))
        ]
        gross = sum(a for _, a in picked)
        if picked and gross > 0:
            # r 已含方向 → 权重取 |w| 并在在场列上重归一（细案 D7 的符号代数）
            idxs = [i for i, _ in picked]
            a = np.array([v / gross for _, v in picked])
            series = R[:, idxs] @ a
            cur_block = {
                "n_factors": len(idxs),
                "train_sharpe": _r4(_sharpe(series[:split], annualization)),
                "holdout_sharpe": _r4(_sharpe(series[split:], annualization)),
                "full_sharpe": _r4(_sharpe(series, annualization)),
            }
    benchmarks["current_rule"] = cur_block

    train_sharpe = chosen["train_sharpe"]
    holdout_sharpe = chosen["holdout_sharpe"]
    ratio = None
    if train_sharpe is not None and holdout_sharpe is not None and holdout_sharpe > 0:
        ratio = round(train_sharpe / holdout_sharpe, 2)
    overfit = bool(ratio is not None and ratio > overfit_alert_ratio)

    def _beats(block: dict[str, Any] | None):
        if block is None:
            return None
        base = block.get("holdout_sharpe")
        if holdout_sharpe is None or base is None:
            return None
        return bool(holdout_sharpe > base)

    notes: list[str] = []
    if total_days < SMALL_SAMPLE_DAYS:
        notes.append(
            f"有效样本 {total_days} 个交易日 < {SMALL_SAMPLE_DAYS}——按组合优化文献结论"
            "（Ross 2026 等），此规模下等权 1/N 是稳健默认，子集结果仅供参考"
        )
    if n_excluded:
        notes.append(
            f"剔除 {n_excluded} 个训练窗有效天 < {MIN_FACTOR_DAYS} 的短历史因子"
        )
    if absent:
        shown = "、".join(absent[:10]) + ("…" if len(absent) > 10 else "")
        notes.append(
            f"{len(absent)} 个候选在序列快照中无数据（未进 universe）：{shown}"
        )
    if overfit:
        notes.append(
            f"train/holdout Sharpe 比 {ratio} > {overfit_alert_ratio}——过拟合风险高"
            "（样本内外比 >3 判据），谨慎对待"
        )
    if train_sharpe is not None and holdout_sharpe is not None and holdout_sharpe <= 0:
        notes.append(
            f"留出窗（holdout）无正收益（Sharpe {holdout_sharpe}）——train/holdout "
            "比不可计算，过拟合旗标不适用（如实呈现，未否决）"
        )
    if k_lo > k_min or k_hi < k_max:
        notes.append(f"候选数 {len(kept)} 不足，K 区间退化为 [{k_lo}, {k_hi}]")
    # 切分点披露：ls_h 是前向标签，train 末 h 个交易日的标签窗与留出窗重叠
    # （细案 D5 选的朴素切分）——不如实说明就等于宣称了不存在的严格无泄露
    notes.append(
        f"train/holdout 为朴素时序切分（无 purge/embargo）：切分点附近约 h={holding_days} "
        "个训练日的前向标签窗伸入留出窗（固定口径，影响有限）"
    )

    dirs = directions or {}
    factor_items = sorted(
        ({"name": kept[i], "direction": int(dirs.get(kept[i], 1))} for i in sel),
        key=lambda x: x["name"],
    )

    return {
        "available": True,
        "method": "greedy_forward_swap",
        "train_frac": train_frac,
        "k_range": [k_lo, k_hi],
        "k": k_star,
        "chosen": chosen,
        "factors": factor_items,
        "curve": curve,
        "benchmarks": benchmarks,
        "overfit_warning": overfit,
        "sharpe_ratio_train_holdout": ratio,
        "vs_benchmarks": {
            "beats_equal_weight": _beats(benchmarks["equal_weight_all"]),
            "beats_current_rule": _beats(cur_block),
        },
        "n_days": {"train": split, "holdout": total_days - split, "total": total_days},
        "universe": {
            "n_candidates": len(kept),
            "n_excluded_short_history": n_excluded,
            "n_absent_from_series": len(absent),
            "absent_from_series": absent[:10],
        },
        "notes": notes,
        "rule": {
            "holding_days": holding_days,
            "round_trip_cost": round_trip_cost,
            "annualization": annualization,
        },
    }
