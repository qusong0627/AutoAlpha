"""T-FM-01：FactorMiner 论文 110 因子 × A 股实测（容器内运行，依赖 qlib 数据）。

跑什么（同一面板、同一可交易过滤、同一评价口径，两侧对照）：

- **论文侧**：附录 D 全部 110 个 FM-DSL 因子（``factorminer`` 包解析+求值，
  白名单解析器，禁 eval）；
- **仓库侧**：统计派 32 个价量候选（``statistical.candidates``）作基线——
  与生产挖掘完全同一条计算路径。

每个因子算：IC/RankIC（1/5/10/20 日）、BH-FDR（q=0.10，**跨两侧全部
142 因子**一次校正，与引擎同口径）、折间符号一致性（4 折，闸与引擎同值
0.75）、值级去重（论文口径 0.5/0.4 与仓库口径 0.9/0.8 分别跑，
``greedy_select`` 席位递补）、与另一侧全部因子的跨库逐日 Rank 相关
（论文×仓库全矩阵 → 每论文因子 max|ρ| 与最近邻）。另出**算子谱**与
**复杂度分布**（AST 级，T-FM-06 结构性原创闸的输入；T-FE-05 统计派简单性
因子另走字符串级口径 ``statistical.complexity``，本测的 AST 复杂度不进选择分）。

口径（写在明面上，避免与生产报告误比）：

- **暖机实现 = 引擎同款 position 掩码**：取数起点走 ``warmup_start``，但
  qlib 日历从窗口起点取（与引擎 ``calendar=None`` 时一致），实际取数
  ≈ 窗口起点；每个因子按 ``position >= min_history`` 裁掉各 symbol 的
  头部 bar（``panel.groupby(level=1).cumcount()``）——本仓生产挖掘就是
  这个口径，本测不另起炉灶，数字与生产报告可直接对照；
- **未做行业中性化**（生产引擎默认 industry 中性化）：本测两侧同口径=
  同不加，如实标注；
- **不做样本外**：本测是入库前的「可挖性」证据；OOS 闸留给毕业流程；
- **可交易过滤两侧同用** ``tradability.build_mask``（停牌+涨跌停如一字板+
  次新，元数据可用时含 ``is_st`` 涨跌停 5% 宽度）；
- **$vwap 口径自适应**：``vm = amount×1e4/volume``（QuantDB 日线：volume=股、
  amount=万元，单位 元/股）。qlib ``$close`` 是否已复权按缓存实现而异，
  故同时算 ``vm×$factor`` 与 ``vm`` 两个候选，取 vwap∈[low,high] 覆盖率高
  者，两者都低于 0.99 直接退出（单位错会让 34/110 条 vwap 类因子静默失真）；
- **$returns 口径与仓库候选同式**（长面板逐标的 ``close/close.shift(1)−1``，
  即 candidates.py 的 ``_ret``）：停牌缺口复牌首日=整段收益，无合成 0 收益；
  窗算子在宽面值网格上滚动，两侧取自同一面板（同为满行面板时 bar 集合一致）。

输出：``--out``（默认 ``/data/refs/factorminer``）下
``T-FM-01_factor_test_{universe}_{start}_{end}.md/.json``；JSON 走
``serialize.json_safe``（NaN→null，严格 JSON）。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path

_project_root = Path(__file__).resolve().parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from backend.services.engine.alpha_agent import mining_config  # noqa: E402
from backend.services.engine.alpha_agent.factorminer import (  # noqa: E402
    LIBRARY,
    evaluate,
    walk,
)
from backend.services.engine.alpha_agent.factorminer import (  # noqa: E402
    dsl as fm_dsl,
)
from backend.services.engine.alpha_agent.factorminer import (  # noqa: E402
    runtime as fm_runtime,
)
from backend.services.engine.alpha_agent.statistical import (  # noqa: E402
    candidates as house,
)
from backend.services.engine.alpha_agent.statistical import (  # noqa: E402
    dedup as dedup_mod,
)
from backend.services.engine.alpha_agent.statistical import ic_stats  # noqa: E402
from backend.services.engine.alpha_agent.statistical import (  # noqa: E402
    panel as panel_mod,
)
from backend.services.engine.alpha_agent.statistical import stability  # noqa: E402
from backend.services.engine.alpha_agent.statistical import (  # noqa: E402
    tradability,
)
from backend.services.engine.alpha_agent.statistical.serialize import json_safe  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("paper_factor_test")

#: 论文侧去重阈值（附录 A 的 admission 口径：θ=0.5 替换；警 0.4 预对齐）
PAPER_DEDUP_REJECT = 0.5
PAPER_DEDUP_WARN = 0.4
#: 仓库侧去重阈值（与 rd_mined_materialize / 引擎同口径）
HOUSE_DEDUP_REJECT = dedup_mod.DEFAULT_CORR_REJECT
HOUSE_DEDUP_WARN = dedup_mod.DEFAULT_CORR_WARN
#: 折间符号一致性闸（与引擎默认同值，两侧同闸）
MIN_FOLD_SIGN_CONSISTENCY = stability.DEFAULT_MIN_FOLD_SIGN_CONSISTENCY
#: 仓库侧滚动再选择 Top-K（引擎 StatisticalConfig.top_k 默认 5）
HOUSE_RESELECT_TOP_K = 5

_FM_PREFIX = "fm"


def _data_end() -> date | None:
    """QuantDB 交易日历最新日（默认窗口锚点）；取不到返回 None（不假装知道）。"""
    try:
        from backend.services.engine.data_platform.quantdb_hub import (  # noqa: PLC0415
            QuantDBDataHub,
        )

        cal = QuantDBDataHub.get_instance().fetch_calendar()
        if cal is not None and not cal.empty:
            for col in ("trade_date", "date", "time", "cal_date", "TradingDate"):
                if col in cal.columns:
                    return pd.to_datetime(cal[col]).max().date()
    except Exception as exc:  # noqa: BLE001 - 锚点是兜底，不是主链路
        logger.warning("交易日历读取失败，默认窗口锚今天：%s", exc)
    return None


# ── 数据装载 ─────────────────────────────────────────────────────────


# 取数/组帧原语已收敛到 factorminer.runtime（生产链路同一实现）；保留私有
# 别名，既有调用无感。
_normalize_single = fm_runtime.normalize_single


def load_bundle(
    start: str,
    end: str,
    *,
    market: str = "csi300",
    qlib_dir: str = panel_mod.DEFAULT_QLIB_DIR,
    warmup_bars: int,
    loader=None,
) -> tuple[pd.DataFrame, pd.Series, pd.Timestamp]:
    """装载 ``(行情面板, $factor 长序列, 实际取数起点)``。

    loader 可注入（测试），签名与 ``panel.load_price_panel`` 约定一致
    ``(instruments, fields, start, end)``；未注入走真 qlib。取数字段 =
    6 个行情字段 + ``$factor``（vwap 复权判定用）**一次取齐**。
    """
    calendar = panel_mod.qlib_calendar(start, end, qlib_dir=qlib_dir)
    fetch_start = panel_mod.warmup_start(start, calendar, warmup_bars)
    universe = panel_mod.qlib_instruments(market, qlib_dir=qlib_dir)
    if not universe:  # 与 panel.load_price_panel 同款守卫：空池要报「池空」，
        raise ValueError(  # 不能被下游误诊成 vwap 单位问题（qlib_instruments 曾整池静默取空）
            f"标的池为空（market={market}）——检查 --universe 拼写与 qlib 缓存"
        )
    fields = [*panel_mod.PRICE_FIELDS, "$factor"]
    start_text = fetch_start.strftime("%Y-%m-%d")
    if loader is None:
        raw = panel_mod._qlib_features(universe, fields, start_text, end, qlib_dir)
    else:
        raw = loader(universe, fields, start_text, end)
    price = panel_mod.normalize_panel(raw)
    factor = _normalize_single(raw, "$factor")
    return price, factor, fetch_start


def build_features(
    price_full: pd.DataFrame, factor_long: pd.Series
) -> tuple[dict[str, pd.DataFrame], dict]:
    """长面板 → FM-DSL 的 8 个字段宽面 + vwap 口径判定记录。

    实现已收敛到 ``factorminer.runtime.build_field_frames``（实验与生产
    同一实现）；CLI 语义保持：口径不合格 → SystemExit，不带病出数。
    """
    try:
        return fm_runtime.build_field_frames(price_full, factor_long)
    except fm_runtime.VwapConventionError as exc:
        raise SystemExit(str(exc)) from exc


# ── 因子求值（全部对齐到 base_index 的共享索引） ──────────────────────


def evaluate_fm_factors(
    frames: dict[str, pd.DataFrame], base_index: pd.MultiIndex
) -> dict[str, pd.Series]:
    out: dict[str, pd.Series] = {}
    for i, factor in enumerate(LIBRARY, start=1):
        if i % 20 == 1:
            logger.info("  论文因子求值 %d/%d", i, len(LIBRARY))
        wide = evaluate(factor.ast, frames)
        # pandas 3 的 stack 不再吃 dropna 参数（新实现全组合展开，缺格即 NaN；
        # pandas 2 的 dropna=True 丢格）——两种行为在 reindex 后等价，故不传参
        long = wide.stack()
        long.index = long.index.set_names(["trade_date", "symbol"])
        out[f"{_FM_PREFIX}{factor.fid}"] = long.reindex(base_index)
    return out


def evaluate_house_factors(
    panel_full: pd.DataFrame, base_index: pd.MultiIndex
) -> dict[str, pd.Series]:
    out: dict[str, pd.Series] = {}
    for candidate in house.CANDIDATES:
        full = house.compute_panel(panel_full, candidate)
        out[candidate.key] = full.reindex(base_index)
    return out


# ── 评价 ─────────────────────────────────────────────────────────────


def _horizon_entry(h: int, summary: ic_stats.IcSummary | None) -> dict:
    # t 值口径 = Newey–West（T-FE-04 起 ic_stats.summarize_ic 的统一口径）：
    # rank_ic_t 是规范量（喂 p/FDR），naive t 与带宽随行落审计列——与
    # statistical runner 的 metadata 同构，报告间可对照复核（缺审计列就
    # 说不清「这个 t 是哪个口径」）。
    if summary is None:
        return {
            "horizon": h,
            "n_days": 0,
            "ic_mean": float("nan"),
            "rank_ic_mean": float("nan"),
            "icir": float("nan"),
            "rank_ic_t": float("nan"),
            "rank_ic_p": float("nan"),
            "rank_ic_t_naive": float("nan"),
            "rank_ic_t_lags": 0,
            "rank_ic_win_rate": float("nan"),
            "fdr_pass": False,
        }
    return {
        "horizon": h,
        "n_days": summary.n_days,
        "ic_mean": summary.ic_mean,
        "rank_ic_mean": summary.rank_ic_mean,
        "icir": summary.icir,
        "rank_ic_t": summary.rank_ic_t,
        "rank_ic_p": summary.rank_ic_p,
        "rank_ic_t_naive": summary.rank_ic_t_naive,
        "rank_ic_t_lags": summary.rank_ic_t_lags,
        "rank_ic_win_rate": summary.rank_ic_win_rate,
        "fdr_pass": False,
    }


def _best(horizons: list[dict]) -> dict | None:
    valid = [h for h in horizons if np.isfinite(h["rank_ic_mean"])]
    if not valid:
        return None
    return max(valid, key=lambda h: abs(h["rank_ic_mean"]))


def _selection_score(
    best: dict | None, sign_consistency: float, *, eligible: bool
) -> float:
    """|RankIC| × 折间符号一致性（与引擎 ``_selection_score`` 同为稳定性
    加权口径：折结构不足 / 未算稳定性时不加权——没有证据不罚，也不假装
    稳定）。**口径分叉（T-FE-05）**：统计派引擎此后另乘简单性因子（qlib
    中缀式的字符串级复杂度）；本运行器未启用复杂度惩罚——FM 因子的 AST
    复杂度（``factorminer.dsl.complexity``）只进报告/原创性闸，不进选择
    分，与引擎不再逐式相同。"""
    if best is None:
        return float("nan")
    weight = 1.0
    if eligible and np.isfinite(sign_consistency):
        weight = float(sign_consistency)
    return abs(float(best["rank_ic_mean"])) * weight


def _apply_fdr(entries: dict[str, dict], q: float) -> None:
    flat: list[tuple[str, dict]] = [
        (key, h) for key, entry in entries.items() for h in entry["horizons"]
    ]
    flags = ic_stats.bh_fdr([h["rank_ic_p"] for _key, h in flat], q=q)
    for (key, horizon_entry), passed in zip(flat, flags, strict=True):
        horizon_entry["fdr_pass"] = bool(passed)
        if passed:
            entries[key]["fdr_horizons"].append(horizon_entry["horizon"])


def _gate_reasons(entry: dict) -> list[str]:
    """与引擎同构的闸序列：样本内 → FDR → 折间符号一致性。"""
    reasons: list[str] = []
    if entry["best_horizon"] is None:
        reasons.append("样本内有效日不足")
    elif not entry["fdr_horizons"]:
        reasons.append("多重检验未过（BH-FDR）")
    if (
        entry["best_horizon"] is not None
        and entry["stability_eligible"]
        and np.isfinite(entry["fold_sign_consistency"])
        and entry["fold_sign_consistency"] < MIN_FOLD_SIGN_CONSISTENCY
    ):
        n_active = sum(1 for ic in entry["fold_ics"] if np.isfinite(ic))
        matches = int(round(entry["fold_sign_consistency"] * n_active))
        reasons.append(f"折间符号不稳（{matches}/{n_active} 折与全窗同号）")
    return reasons


def _rank_eligible(entries: dict[str, dict]) -> list[str]:
    eligible = [
        e
        for e in entries.values()
        if e["best_horizon"] is not None and not e["reject_reason"]
    ]
    eligible.sort(
        key=lambda e: (e["selection_score"], abs(e["rank_ic_mean"])), reverse=True
    )
    return [e["key"] for e in eligible]


def _dedup_side(
    entries: dict[str, dict],
    values: dict[str, pd.Series],
    *,
    top_k: int,
    reject: float,
    warn: float,
    reselect_top_k: int,
) -> dict:
    """按选择分序做选择期值级去重（席位递补），回填入选/挤出/未进 K 三种终态。"""
    order = _rank_eligible(entries)
    report = dedup_mod.greedy_select(
        order,
        provider=values.__getitem__,
        top_k=top_k,
        reject=reject,
        warn=warn,
    )
    dropped_because = {key: (matched, rho) for key, matched, rho in report.dropped}
    for entry in entries.values():
        if entry["key"] in report.kept:
            entry["selected"] = True
            entry["max_abs_corr"] = report.max_abs_corr(entry["key"])
            entry["corr_warn"] = report.warn_note(entry["key"])
        elif entry["key"] in dropped_because:
            matched, rho = dropped_because[entry["key"]]
            entry["reject_reason"] = (
                f"与 {matched} 高度相关（ρ={rho:.2f}，阈值 {reject:g}）"
            )
        elif not entry["reject_reason"]:
            entry["reject_reason"] = "未入选 Top-K（按选择分排序）"
    return {
        "order": order,
        "top_k": top_k,
        "reject": reject,
        "warn": warn,
        "reselect_top_k": reselect_top_k,
        "kept": list(report.kept),
        "dropped": [[key, matched, rho] for key, matched, rho in report.dropped],
        "warnings": [[a, b, rho] for a, b, rho in report.warnings],
        "n_pairs": report.n_pairs,
    }


def _cross_corr(
    paper_values: dict[str, pd.Series], house_values: dict[str, pd.Series]
) -> dict:
    """论文 × 仓库 全矩阵逐日 Rank 相关（每论文因子 max|ρ| + 全库最强对）。"""
    per_paper: dict[str, dict] = {}
    pairs: list[list] = []
    for pkey, pseries in paper_values.items():
        best_rho, best_key = float("nan"), ""
        for hkey, hseries in house_values.items():
            rho, _n = ic_stats.daily_rank_corr(pseries, hseries)
            if np.isfinite(rho):
                pairs.append([pkey, hkey, float(rho)])
                if not np.isfinite(best_rho) or abs(rho) > abs(best_rho):
                    best_rho, best_key = float(rho), hkey
        per_paper[pkey] = {"rho": best_rho, "against": best_key}
    strongest = sorted(pairs, key=lambda p: abs(p[2]), reverse=True)[:20]
    return {"per_paper": per_paper, "strongest_pairs": strongest}


def _operator_spectrum() -> dict:
    counts: dict[str, int] = {}
    for factor in LIBRARY:
        for node in walk(factor.ast):
            if isinstance(node, fm_dsl.Call):
                counts[node.op] = counts.get(node.op, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def _complexity_distribution() -> dict:
    def stats(values: list[int]) -> dict:
        arr = np.asarray(values, dtype=float)
        return {
            "min": int(arr.min()),
            "median": float(np.median(arr)),
            "mean": float(arr.mean()),
            "max": int(arr.max()),
        }

    return {
        "node_count": stats([f.complexity.node_count for f in LIBRARY]),
        "depth": stats([f.complexity.depth for f in LIBRARY]),
        "max_window": stats([f.complexity.max_window for f in LIBRARY]),
        "min_history": stats([f.min_history for f in LIBRARY]),
    }


# ── 主流程 ───────────────────────────────────────────────────────────


def run_paper_test(
    price_full: pd.DataFrame,
    factor_long: pd.Series,
    *,
    start: str,
    end: str,
    universe: str,
    metadata: pd.DataFrame | None = None,
    horizons: tuple[int, ...] = (1, 5, 10, 20),
    fdr_q: float = 0.10,
    folds: int = stability.DEFAULT_FOLDS,
    min_fold_days: int = stability.DEFAULT_MIN_FOLD_DAYS,
    top_k_paper: int = 20,
    top_k_house: int = 10,
    fetch_start: str = "",
    warmup_bars: int = 0,
) -> dict:
    """全流程计算 → 审计报告 dict（不含 IO；md/json 落盘在 main）。

    ``metadata``：``panel.load_symbol_metadata`` 形状（symbol_digits 索引）；
    给定时与引擎同款接法——``is_st`` 进面板（涨跌停宽度 5%）、
    ``listing_date`` 进次新过滤；None 时两项如实跳过。
    """
    panel = price_full
    listing = None
    if metadata is not None and not metadata.empty:
        if "is_st" in metadata.columns:
            panel = panel.assign(is_st=panel_mod.map_to_panel(panel, metadata["is_st"]))
        if "listing_date" in metadata.columns:
            listing = metadata["listing_date"].dropna()

    frames, vwap_info = build_features(price_full, factor_long)
    logger.info(
        "vwap 口径 = %s，覆盖率 %.4f（×factor %.4f / 不带 %.4f）",
        vwap_info["vwap_convention"],
        vwap_info["vwap_coverage"],
        vwap_info["vwap_coverage_with_factor"],
        vwap_info["vwap_coverage_without_factor"],
    )

    mask, filter_report = tradability.build_mask(
        panel, cfg=tradability.TradabilityConfig(), listing_dates=listing
    )
    dates = panel.index.get_level_values(0)
    in_win = np.asarray(dates >= pd.Timestamp(start)) & np.asarray(
        dates <= pd.Timestamp(end)
    )
    base_index = panel.index[in_win]
    # 引擎同款头部掩码：position = 各 symbol 自面板起的 bar 序号，低于该因子
    # min_history 的行不参与 IC（本测取数不含窗口前暖机段，与生产一致）
    position = panel.groupby(level=1).cumcount().to_numpy()[in_win]
    keep_common = pd.Series(mask.to_numpy()[in_win], index=base_index)

    logger.info(
        "评价窗 %s ~ %s：%d 行 × %d 标的（可交易剔除 %d 行）",
        start,
        end,
        len(base_index),
        int(panel.index[in_win].get_level_values(1).nunique()),
        int((~keep_common).sum()),
    )

    logger.info("论文侧因子求值（%d 个）", len(LIBRARY))
    values: dict[str, pd.Series] = {}
    min_history_by_key: dict[str, int] = {}
    for factor in LIBRARY:
        min_history_by_key[f"{_FM_PREFIX}{factor.fid}"] = factor.min_history
    for key, series in evaluate_fm_factors(frames, base_index).items():
        usable = keep_common & pd.Series(
            position >= min_history_by_key[key], index=base_index
        )
        values[key] = series.where(usable)
    logger.info("仓库侧因子求值（%d 个）", len(house.CANDIDATES))
    for candidate in house.CANDIDATES:
        min_history_by_key[candidate.key] = candidate.min_history
    for key, series in evaluate_house_factors(panel, base_index).items():
        usable = keep_common & pd.Series(
            position >= min_history_by_key[key], index=base_index
        )
        values[key] = series.where(usable)

    # 前向收益在评价窗口内计算（panel 行 == 窗口行），与 factors 行对齐
    fwd = {h: ic_stats.forward_returns(panel[in_win], h) for h in horizons}

    logger.info("逐因子 IC（%d 个因子 × %d 个周期）", len(values), len(horizons))
    entries: dict[str, dict] = {}
    daily_rank: dict[str, pd.Series] = {}
    for key, series in values.items():
        horizon_entries = []
        daily_by_h: dict[int, pd.DataFrame] = {}
        for h in horizons:
            daily = ic_stats.daily_ic(series, fwd[h])
            daily_by_h[h] = daily
            horizon_entries.append(_horizon_entry(h, ic_stats.summarize_ic(daily)))
        best = _best(horizon_entries)
        if best is not None:
            daily_rank[key] = daily_by_h[best["horizon"]]["rank_ic"]
        entries[key] = {
            "key": key,
            "min_history": min_history_by_key[key],
            "horizons": horizon_entries,
            "best_horizon": best["horizon"] if best else None,
            "rank_ic_mean": best["rank_ic_mean"] if best else float("nan"),
            "icir": best["icir"] if best else float("nan"),
            "t_stat": best["rank_ic_t"] if best else float("nan"),
            "p_value": best["rank_ic_p"] if best else float("nan"),
            "rank_ic_win_rate": best["rank_ic_win_rate"] if best else float("nan"),
            "fdr_horizons": [],
            "fold_ics": [],
            "fold_sign_consistency": float("nan"),
            "reselect_rate": float("nan"),
            "reselect_hits": 0,
            "reselect_checkpoints": folds - 1,
            "stability_eligible": False,
            "stability_note": "",
            "selection_score": float("nan"),
            "selected": False,
            "reject_reason": "",
            "max_abs_corr": float("nan"),
            "corr_warn": "",
            "max_abs_corr_vs_house": float("nan"),
            "closest_house": "",
        }

    n_tests = sum(len(e["horizons"]) for e in entries.values())
    logger.info("多重检验（BH-FDR，跨全部 %d 个 因子×周期）", n_tests)
    _apply_fdr(entries, fdr_q)

    paper_entries = {k: e for k, e in entries.items() if k.startswith(_FM_PREFIX)}
    house_entries = {k: e for k, e in entries.items() if not k.startswith(_FM_PREFIX)}

    logger.info("折间符号一致性（%d 折；两侧各自成池，折界同源）", folds)
    stab_by_side = {
        "paper": stability.reselect_statistics(
            {k: daily_rank[k] for k in paper_entries if k in daily_rank},
            folds=folds,
            top_k=top_k_paper,
            min_fold_days=min_fold_days,
        ),
        "house": stability.reselect_statistics(
            {k: daily_rank[k] for k in house_entries if k in daily_rank},
            folds=folds,
            top_k=HOUSE_RESELECT_TOP_K,
            min_fold_days=min_fold_days,
        ),
    }
    for side_entries, stab in (
        (paper_entries, stab_by_side["paper"]),
        (house_entries, stab_by_side["house"]),
    ):
        for key, stat in stab.items():
            side_entries[key].update(
                fold_ics=list(stat.fold_ics),
                fold_sign_consistency=stat.sign_consistency,
                reselect_rate=stat.reselect_rate,
                reselect_hits=stat.reselect_hits,
                stability_eligible=stat.eligible,
                stability_note=stat.note,
            )
    for entry in entries.values():
        entry["selection_score"] = _selection_score(
            _best(entry["horizons"]) if entry["best_horizon"] is not None else None,
            entry["fold_sign_consistency"],
            eligible=entry["stability_eligible"],
        )
        entry["reject_reason"] = "；".join(_gate_reasons(entry))

    paper_values = {k: values[k] for k in paper_entries}
    house_values = {k: values[k] for k in house_entries}
    logger.info(
        "值级去重：论文侧 拒 %g/警 %g K=%d / 仓库侧 拒 %g/警 %g K=%d",
        PAPER_DEDUP_REJECT,
        PAPER_DEDUP_WARN,
        top_k_paper,
        HOUSE_DEDUP_REJECT,
        HOUSE_DEDUP_WARN,
        top_k_house,
    )
    paper_dedup = _dedup_side(
        paper_entries,
        paper_values,
        top_k=top_k_paper,
        reject=PAPER_DEDUP_REJECT,
        warn=PAPER_DEDUP_WARN,
        reselect_top_k=top_k_paper,
    )
    house_dedup = _dedup_side(
        house_entries,
        house_values,
        top_k=top_k_house,
        reject=HOUSE_DEDUP_REJECT,
        warn=HOUSE_DEDUP_WARN,
        reselect_top_k=HOUSE_RESELECT_TOP_K,
    )

    logger.info(
        "跨库相关矩阵（论文 %d × 仓库 %d）", len(paper_values), len(house_values)
    )
    cross = _cross_corr(paper_values, house_values)
    for key, info in cross["per_paper"].items():
        entries[key]["max_abs_corr_vs_house"] = info["rho"]
        entries[key]["closest_house"] = info["against"]

    paper_report = {"dedup": paper_dedup, "factors": _sorted_factors(paper_entries)}
    house_report = {"dedup": house_dedup, "factors": _sorted_factors(house_entries)}
    by_key = {c.key: c for c in house.CANDIDATES}
    for item in house_report["factors"]:
        candidate = by_key[item["key"]]
        item.update(
            name=candidate.name,
            family=candidate.family,
            formulation=candidate.formulation,
            source=candidate.source,
            expression=candidate.expression,
        )
    fid_by_key = {f"{_FM_PREFIX}{f.fid}": f for f in LIBRARY}
    for item in paper_report["factors"]:
        factor = fid_by_key[item["key"]]
        item.update(
            fid=factor.fid,
            expression=factor.expression,
            complexity={
                "node_count": factor.complexity.node_count,
                "op_count": factor.complexity.op_count,
                "depth": factor.complexity.depth,
                "max_window": factor.complexity.max_window,
                "literal_count": factor.complexity.literal_count,
            },
        )

    return {
        "task": "T-FM-01",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window": {
            "start": start,
            "end": end,
            "fetch_start": fetch_start,
            "warmup_bars": warmup_bars,
            "warmup_discipline": "引擎同款 position 掩码（取数不含窗口前暖机段）",
        },
        "universe": universe,
        "n_symbols": int(panel.index[in_win].get_level_values(1).nunique()),
        "panel_rows": int(len(base_index)),
        "horizons": list(horizons),
        "fdr_q": fdr_q,
        "fdr_pool": f"{n_tests} 项（两侧合计 因子×周期；生产报告只校正单侧——过线集不可直接对比）",
        "folds": folds,
        "returns_convention": (
            "长面板逐标的 close/close.shift(1)−1（与仓库候选 _ret 同式；停牌缺口"
            "复牌首日=整段收益，无合成 0 收益）；窗算子在宽面值网格上滚动，两侧取自同一面板"
        ),
        "neutralize": "none（两侧同口径；生产引擎默认 industry，差异如实标注）",
        "oos": "未做（本测为入库前可挖性证据；OOS 闸留给毕业流程）",
        "tradability": {
            "filter": {**json_safe(filter_report), "n_kept": filter_report.n_kept},
            "listing_dates": listing is not None,
            "is_st_widths": "is_st" in panel.columns,
        },
        **vwap_info,
        "paper": paper_report,
        "house": house_report,
        "cross": cross,
        "operator_spectrum": _operator_spectrum(),
        "complexity": _complexity_distribution(),
    }


def _sorted_factors(entries: dict[str, dict]) -> list[dict]:
    """报告排序：选择分降序（NaN 沉底），同分以 |RankIC| 兜底。"""
    return sorted(
        entries.values(),
        key=lambda e: (
            np.isfinite(e["selection_score"]),
            e["selection_score"] if np.isfinite(e["selection_score"]) else -1.0,
            abs(e["rank_ic_mean"]) if np.isfinite(e["rank_ic_mean"]) else -1.0,
        ),
        reverse=True,
    )


# ── 报告渲染 ─────────────────────────────────────────────────────────


def _fmt(value, digits: int = 3, signed: bool = False) -> str:
    if value is None or (isinstance(value, float) and not np.isfinite(value)):
        return "—"
    if isinstance(value, float):
        return f"{value:+.{digits}f}" if signed else f"{value:.{digits}f}"
    return str(value)


def _best_n_days(item: dict) -> str:
    for h in item["horizons"]:
        if h["horizon"] == item["best_horizon"]:
            return str(h["n_days"])
    return "—"


def render_markdown(report: dict) -> str:
    lines: list[str] = []
    add = lines.append
    add("# T-FM-01：FactorMiner 论文 110 因子 × A 股实测报告")
    add("")
    add(f"- 生成: {report['generated_at']}")
    window = report["window"]
    add(
        f"- 评价窗: {window['start']} ~ {window['end']}（取数起点 {window['fetch_start']}，"
        f"{window['warmup_discipline']}）"
    )
    add(
        f"- 标的池: {report['universe']}（{report['n_symbols']} 只）；面板行数 "
        f"{report['panel_rows']:,}；周期 {report['horizons']}；FDR q={report['fdr_q']}；"
        f"{report['folds']} 折"
    )
    add(f"- 中性化: {report['neutralize']}")
    add(f"- 样本外: {report['oos']}")
    add(f"- FDR 校正池: {report['fdr_pool']}")
    add(f"- $returns 口径: {report['returns_convention']}")
    add(
        f"- 可交易过滤: {report['tradability']['filter']}；listing_dates="
        f"{report['tradability']['listing_dates']}；is_st 宽度="
        f"{report['tradability']['is_st_widths']}"
    )
    add(
        f"- $vwap 口径 {report['vwap_convention']}，覆盖率（∈[low,high]）: "
        f"{report['vwap_coverage']:.4f}"
    )
    add("")

    for side, title in (
        ("paper", "论文侧 110 因子"),
        ("house", "仓库侧 32 候选（基线）"),
    ):
        block = report[side]
        dedup = block["dedup"]
        factors = block["factors"]
        add(f"## {title}")
        add("")
        add(
            f"可评估 {sum(1 for f in factors if f['best_horizon'] is not None)}/"
            f"{len(factors)}；FDR 过线 "
            f"{sum(1 for f in factors if f['fdr_horizons'])}；"
            f"去重口径 拒 |ρ|≥{dedup['reject']:g} / 警 {dedup['warn']:g}，"
            f"Top-{dedup['top_k']} 席位递补；入选 {len(dedup['kept'])} 个，"
            f"去重挤出 {len(dedup['dropped'])} 个，评估 {dedup['n_pairs']} 对"
            f"（滚动再选择 Top-{dedup['reselect_top_k']}）。"
        )
        add("")
        # 末列两侧语义不同：论文侧=与仓库全库逐日 Rank 相关的 max|ρ|（跨库
        # 近邻，取绝对值——原始 ρ 有符号，直出会与「拒 |ρ|≥阈值」被误读）；
        # 仓库侧=库内去重的 max|ρ|（去重闸相关量）。
        corr_header = "与仓库 \\|ρ\\|max" if side == "paper" else "库内 \\|ρ\\|max"
        add(
            "| key | 表达式 | 最优h | n_days | RankIC | ICIR | t | p | FDR | 折一致 | "
            f"选择分 | 终态 | {corr_header} |"
        )
        add("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        for item in factors:
            expression = item["expression"]
            if len(expression) > 56:
                expression = expression[:53] + "…"
            fdr = ",".join(str(h) for h in item["fdr_horizons"]) or "—"
            if item["selected"]:
                state = "✓入选"
            elif item["reject_reason"]:
                state = item["reject_reason"].split("；")[0]
            else:
                state = "—"
            if side == "paper":
                vs = item["max_abs_corr_vs_house"]
                corr = abs(vs) if isinstance(vs, float) and np.isfinite(vs) else vs
            else:
                corr = item["max_abs_corr"]
            add(
                f"| {item['key']} | `{expression}` | "
                f"{_fmt(item['best_horizon'], 0)} | {_best_n_days(item)} | "
                f"{_fmt(item['rank_ic_mean'], 4, signed=True)} | {_fmt(item['icir'], 2)} | "
                f"{_fmt(item['t_stat'], 2)} | {_fmt(item['p_value'], 4)} | {fdr} | "
                f"{_fmt(item['fold_sign_consistency'], 2)} | "
                f"{_fmt(item['selection_score'], 4)} | {state} | {_fmt(corr, 2)} |"
            )
        add("")
        if dedup["dropped"]:
            add("去重挤出明细：")
            add("")
            for key, matched, rho in dedup["dropped"]:
                add(f"- {key} ← 与 {matched} 相关 ρ={rho:+.3f}")
            add("")

    cross = report["cross"]
    add("## 跨库相关（论文 × 仓库）")
    add("")
    add("| 论文因子 | 最近仓库候选 | ρ |")
    add("|---|---|---|")
    for pair in cross["strongest_pairs"][:15]:
        add(f"| {pair[0]} | {pair[1]} | {pair[2]:+.3f} |")
    add("")
    add("## 算子谱（论文库用量）")
    add("")
    add("| 算子 | 次数 | 算子 | 次数 |")
    add("|---|---|---|---|")
    spectrum = list(report["operator_spectrum"].items())
    half = (len(spectrum) + 1) // 2
    for i in range(half):
        left = spectrum[i]
        if i + half < len(spectrum):
            right = spectrum[i + half]
            add(f"| {left[0]} | {left[1]} | {right[0]} | {right[1]} |")
        else:
            add(f"| {left[0]} | {left[1]} | | |")
    add("")
    add("## 复杂度分布（论文库）")
    add("")
    for name, stats in report["complexity"].items():
        add(
            f"- {name}: min={stats['min']} median={_fmt(stats['median'], 1)} "
            f"mean={_fmt(stats['mean'], 2)} max={stats['max']}"
        )
    add("")
    return "\n".join(lines)


# ── CLI ──────────────────────────────────────────────────────────────


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--universe", default="csi300", help="qlib 标的池（csi300/all/...）"
    )
    parser.add_argument(
        "--start", default=None, help="评价窗起（默认：锚数据末端前推 4 年）"
    )
    parser.add_argument("--end", default=None, help="评价窗止（默认：数据末端）")
    parser.add_argument("--out", default="/data/refs/factorminer", help="报告输出目录")
    parser.add_argument("--qlib-dir", default=panel_mod.DEFAULT_QLIB_DIR)
    parser.add_argument("--horizons", default="1,5,10,20")
    parser.add_argument("--top-k-paper", type=int, default=20)
    parser.add_argument("--top-k-house", type=int, default=10)
    parser.add_argument("--fdr-q", type=float, default=0.10)
    parser.add_argument("--folds", type=int, default=stability.DEFAULT_FOLDS)
    return parser.parse_args(argv)


def _load_metadata() -> pd.DataFrame | None:
    """QuantDB 元数据（is_st 宽度 / 次新过滤）；取不到返回 None（如实记，不假装）。"""
    try:
        return panel_mod.load_symbol_metadata()
    except Exception as exc:  # noqa: BLE001 - 元数据是增强，不是主链路
        logger.warning("元数据读取失败，跳过 ST 宽度与次新过滤：%s", exc)
        return None


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    data_end = _data_end()
    if args.start and args.end:
        start, end = args.start, args.end
    else:
        mining, _validation = mining_config.default_windows(
            data_end=data_end, today=date.today()
        )
        start = args.start or mining.start.isoformat()
        end = args.end or mining.end.isoformat()
    horizons = tuple(int(h) for h in args.horizons.split(",") if h.strip())

    warmup_bars = max(house.MAX_MIN_HISTORY, max(f.min_history for f in LIBRARY))
    logger.info(
        "T-FM-01 启动：universe=%s window=%s~%s horizons=%s warmup=%d",
        args.universe,
        start,
        end,
        horizons,
        warmup_bars,
    )
    price_full, factor_long, fetch_start = load_bundle(
        start,
        end,
        market=args.universe,
        qlib_dir=args.qlib_dir,
        warmup_bars=warmup_bars,
    )
    logger.info(
        "行情装载完成：%d 行；$factor 覆盖 %d 行",
        len(price_full),
        int(factor_long.notna().sum()),
    )

    report = run_paper_test(
        price_full,
        factor_long,
        start=start,
        end=end,
        universe=args.universe,
        metadata=_load_metadata(),
        horizons=horizons,
        fdr_q=args.fdr_q,
        folds=args.folds,
        top_k_paper=args.top_k_paper,
        top_k_house=args.top_k_house,
        fetch_start=fetch_start.strftime("%Y-%m-%d"),
        warmup_bars=warmup_bars,
    )

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"T-FM-01_factor_test_{args.universe}_{start}_{end}"
    json_path = out_dir / f"{stem}.json"
    md_path = out_dir / f"{stem}.md"
    json_path.write_text(
        json.dumps(json_safe(report), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md_path.write_text(render_markdown(json_safe(report)), encoding="utf-8")
    logger.info("报告已写：%s / %s", json_path, md_path)

    paper_selected = report["paper"]["dedup"]["kept"]
    house_selected = report["house"]["dedup"]["kept"]
    logger.info("论文侧入选 %d：%s", len(paper_selected), paper_selected)
    logger.info("仓库侧入选 %d：%s", len(house_selected), house_selected)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
