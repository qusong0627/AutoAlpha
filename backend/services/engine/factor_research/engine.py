"""因子研究模块 —— 行情类 / 估值类因子计算引擎（纯 pandas，无服务依赖）。

设计要点：
  1. 因子只在「月度采样日 + 最新日」上取值（IC/回测/持仓均为月频），
     因此重窗口运算（如 252 日最大回撤）按采样日逐日切片计算即可；
  2. 所有原始值随后统一走 ``rank_to_score``（截面 pct rank → 正态分位，按方向调号），
     与 factor-lib-demo 的口径一致；
  3. 价格用 daily_forward 前复权价；amount 为万元（未复权，仅用于换手/流动性类，
     与名义市值同基准，无需复权）。
"""

from __future__ import annotations

import math
from statistics import NormalDist

import numpy as np
import pandas as pd

_NORM = NormalDist()

# 样本日切片时用到的历史窗口（交易日）
W_MAX = 252
_EPS = 1e-12


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _logret(close: pd.DataFrame) -> pd.DataFrame:
    return np.log(close / close.shift(1))


def _rolling_corr(
    a: pd.DataFrame, b: pd.DataFrame, w: int, mp: int | None = None
) -> pd.DataFrame:
    """滚动相关系数（ddof=1 等价实现，避免 rolling.corr 的成对缺失处理过慢）。"""
    mp = mp or max(2, w // 2)
    ma, mb = a.rolling(w, min_periods=mp).mean(), b.rolling(w, min_periods=mp).mean()
    cov = (a * b).rolling(w, min_periods=mp).mean() - ma * mb
    sa = a.rolling(w, min_periods=mp).std()
    sb = b.rolling(w, min_periods=mp).std()
    return cov / (sa * sb + _EPS)


def _mdd_at_dates(
    close: pd.DataFrame, dates: list[pd.Timestamp], w: int = W_MAX
) -> pd.DataFrame:
    """按采样日计算窗口内最大回撤（返回正数，0 表示期间无回撤）。"""
    pos = {d: i for i, d in enumerate(close.index)}
    out = {}
    for d in dates:
        i = pos.get(d)
        if i is None or i < 20:
            out[d] = pd.Series(np.nan, index=close.columns)
            continue
        win = close.iloc[max(0, i - w + 1) : i + 1]
        dd = 1.0 - win / win.cummax()
        out[d] = dd.max()
    return pd.DataFrame(out).T


def _industry_mean(panel: pd.DataFrame, ind: pd.Series) -> pd.DataFrame:
    """按行业对宽表按日求均值（返回同日同结构）。"""
    t = panel.T
    grp = t.groupby(ind.reindex(t.index).fillna("其他"))
    return grp.transform("mean").T


def _industry_median(panel: pd.DataFrame, ind: pd.Series) -> pd.DataFrame:
    t = panel.T
    grp = t.groupby(ind.reindex(t.index).fillna("其他"))
    return grp.transform("median").T


_A = [
    -3.969683028665376e01,
    2.209460984245205e02,
    -2.759285104469687e02,
    1.383577518672690e02,
    -3.066479806614716e01,
    2.506628277459239e00,
]
_B = [
    -5.447609879822406e01,
    1.615858368580409e02,
    -1.556989798598866e02,
    6.680131188771972e01,
    -1.328068155288572e01,
]
_C = [
    -7.784894002430293e-03,
    -3.223964580411365e-01,
    -2.400758277161838e00,
    -2.549732539343734e00,
    4.374664141464968e00,
    2.938163982698783e00,
]
_D = [
    7.784695709041462e-03,
    3.224671290700398e-01,
    2.445134137142996e00,
    3.754408661907416e00,
]


def _norm_ppf_np(p: np.ndarray) -> np.ndarray:
    """正态分布分位函数（Acklam 近似，numpy 向量化；避免引入 scipy 依赖）。"""
    p = np.clip(p, 1e-12, 1 - 1e-12)
    out = np.empty_like(p, dtype=float)
    lo = p < 0.02425
    hi = p > 1 - 0.02425
    mid = ~(lo | hi)
    if lo.any():
        q = np.sqrt(-2 * np.log(p[lo]))
        out[lo] = (
            ((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4]) * q + _C[5]
        ) / ((((_D[0] * q + _D[1]) * q + _D[2]) * q + _D[3]) * q + 1)
    if hi.any():
        q = np.sqrt(-2 * np.log(1 - p[hi]))
        out[hi] = -(
            ((((_C[0] * q + _C[1]) * q + _C[2]) * q + _C[3]) * q + _C[4]) * q + _C[5]
        ) / ((((_D[0] * q + _D[1]) * q + _D[2]) * q + _D[3]) * q + 1)
    if mid.any():
        q = p[mid] - 0.5
        rr = q * q
        out[mid] = (
            (
                ((((_A[0] * rr + _A[1]) * rr + _A[2]) * rr + _A[3]) * rr + _A[4]) * rr
                + _A[5]
            )
            * q
            / (
                ((((_B[0] * rr + _B[1]) * rr + _B[2]) * rr + _B[3]) * rr + _B[4]) * rr
                + 1
            )
        )
    return out


def rank_to_score(
    raw: pd.DataFrame, direction: int, universe: pd.DataFrame | None = None
) -> pd.DataFrame:
    """截面原始值 → 打分：pct rank → 正态分位（±4 截断），direction=1 保留、-1 取负。

    返回与 raw 同形（仅采样日行）；越大约好（做多首选分高者）。
    """
    r = raw if universe is None else raw.where(universe.reindex_like(raw).fillna(False))
    n = r.notna().sum(axis=1)
    pct = (
        r.rank(axis=1, method="average", na_option="keep")
        .sub(0.5)
        .div(n.replace(0, np.nan), axis=0)
    )
    v = _norm_ppf_np(pct.to_numpy(dtype=float))
    score = pd.DataFrame(np.clip(v, -4, 4), index=raw.index, columns=raw.columns)
    return score * direction


# ---------------------------------------------------------------------------
# 行情类因子（市场交易信息 26 个）
# ---------------------------------------------------------------------------
def compute_trade_factors(
    daily: dict[str, pd.DataFrame],
    val: dict[str, pd.DataFrame],
    instr_df: pd.DataFrame,
    dates: list[pd.Timestamp],
) -> dict[str, pd.DataFrame]:
    """返回 {factor_code: 原始值宽表（采样日 × symbol）}。"""
    close = daily["close"].where(daily["close"] > 0)  # 零/负价（数据瑕疵，约 4‰）→ NaN
    high = daily["high"].where(daily["high"] > 0, close)
    low = daily["low"].where(daily["low"] > 0, close)
    amount = daily["amount"].clip(lower=0)  # 万元；负值按 0 处理
    r = _logret(close)
    idx = close.index
    # 换手率 = 成交额(万元→元) / 流通市值(元)
    float_mv = val["float_mv"].reindex(index=idx, columns=close.columns)
    turnover = ((amount * 1e4) / (float_mv + _EPS)).replace([np.inf, -np.inf], np.nan)
    amt_pos = amount  # 已在段首 clamp ≥0
    out: dict[str, pd.DataFrame] = {}

    # --- 动量/反转 ---
    cum = {w: r.rolling(w, min_periods=int(w * 0.8)).sum() for w in (5, 20, 60)}
    mom12 = None
    for lag in range(21, 252):
        wgt = 0.5 ** (lag / 126.0)
        term = r.shift(lag) * wgt
        mom12 = term if mom12 is None else mom12.add(term, fill_value=0)
    out["MOM12_1"] = mom12 / sum(0.5 ** (lag / 126.0) for lag in range(21, 252))
    out["REV1M"] = r.rolling(21, min_periods=17).sum()
    out["REV5D"] = cum[5]
    out["MOM60"] = cum[60]
    out["MOM20"] = cum[20]
    ind = instr_df.set_index("symbol")["industry"]
    out["RELRET60"] = cum[60].sub(cum[60].median(axis=1), axis=0)
    out["RELRETIND"] = cum[60] - _industry_mean(cum[60], ind)

    # --- 波动 ---
    out["DASTD"] = r.rolling(252, min_periods=200).std() * math.sqrt(252)
    # 下行波动率：只对负收益日计算（负收益约占窗口一半，min_periods 需按实际计）
    neg = r.where(r < 0)
    cnt = neg.notna().rolling(252, min_periods=60).sum()
    s1 = neg.rolling(252, min_periods=60).sum()
    s2 = (neg**2).rolling(252, min_periods=60).sum()
    mean_neg = s1 / cnt
    var_neg = (s2 / cnt - mean_neg**2).clip(lower=0)
    out["DOWNVOL"] = (np.sqrt(var_neg) * math.sqrt(252)).where(cnt >= 60)
    out["MAXDD1Y"] = _mdd_at_dates(close, dates)
    out["RETSKEW"] = r.rolling(252, min_periods=200).skew()
    out["RETKURT"] = r.rolling(252, min_periods=200).kurt()
    out["BIGDOWN"] = (r < -0.05).rolling(60, min_periods=45).mean()

    # --- 流动性 ---
    out["STOM"] = np.log(turnover.rolling(21, min_periods=15).sum() + _EPS)
    out["AMOUNT20"] = np.log(amount.rolling(20, min_periods=15).mean() + _EPS)
    out["TURN20"] = turnover.rolling(20, min_periods=15).mean()
    out["AMTVOL"] = amount.rolling(60, min_periods=45).std() / (
        amount.rolling(60, min_periods=45).mean() + _EPS
    )
    out["TURNVOL"] = turnover.rolling(60, min_periods=45).std()

    # --- Beta（对 cap-weighted 全市场组合，252 日；回归斜率 = cov(r, m)/var(m)）---
    total_mv = val["total_mv"].reindex(index=idx, columns=close.columns)
    w = total_mv.shift(1).where(r.notna())
    r_filled = r.where(r.notna(), 0.0)
    mkt = (r_filled * w.fillna(0)).sum(axis=1) / (w.fillna(0).sum(axis=1) + _EPS)
    mkt_df = pd.DataFrame(
        np.repeat(mkt.values[:, None], close.shape[1], axis=1),
        index=idx,
        columns=close.columns,
    )
    ma, mm = (
        r.rolling(252, min_periods=200).mean(),
        mkt_df.rolling(252, min_periods=200).mean(),
    )
    cov = (r * mkt_df).rolling(252, min_periods=200).mean() - ma * mm
    var_m = (mkt_df**2).rolling(252, min_periods=200).mean() - mm**2
    out["BETA"] = cov / (var_m + _EPS)

    # --- 均值回复 ---
    ma60 = close.rolling(60, min_periods=45).mean()
    sd60 = close.rolling(60, min_periods=45).std()
    out["PRICEZ"] = (close - ma60) / (sd60 + _EPS)
    out["MA20BIAS"] = close / (close.rolling(20, min_periods=15).mean() + _EPS) - 1
    hi60 = high.rolling(60, min_periods=45).max()
    lo60 = low.rolling(60, min_periods=45).min()
    out["HLPOS"] = (close - lo60) / (hi60 - lo60 + _EPS)

    # --- 价量 / 拥挤度 ---
    out["PVCORR"] = _rolling_corr(r, np.log(amt_pos + _EPS), 60, mp=45)
    # 上涨日成交额占比：分子只统计上涨日（窗口内约一半天数有效，min_periods 取小）
    up_amt = amount.where(r > 0)
    out["UPVOLRATIO"] = up_amt.rolling(60, min_periods=5).sum() / (
        amount.rolling(60, min_periods=45).sum() + _EPS
    )
    out["ABTURN"] = (
        turnover.rolling(5, min_periods=4).mean()
        / (turnover.rolling(60, min_periods=45).mean() + _EPS)
        - 1
    )

    return {k: v.reindex(dates) for k, v in out.items()}


# ---------------------------------------------------------------------------
# 估值类因子（11 个 + LNMV；其中 3 个依赖财报，由 financials.py 提供）
# ---------------------------------------------------------------------------
def compute_valuation_factors(
    val: dict[str, pd.DataFrame],
    instr_df: pd.DataFrame,
    dates: list[pd.Timestamp],
    pb_daily: pd.DataFrame,
) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    ind = instr_df.set_index("symbol")["industry"]

    pe = val["pe_ttm"].where(val["pe_ttm"] > 0)  # 亏损股 PE 无意义 → NaN
    pb = val["pb"].where(val["pb"] > 0)  # 净资产为负 → NaN
    ps = val["ps_ttm"].where(val["ps_ttm"] > 0)
    mv = val["total_mv"]
    out["PE"] = pe
    out["PB"] = pb
    out["PS"] = ps
    out["DIVYLD"] = val["dividend_rate"]  # 单位口径逐日一致，仅用于截面排序
    out["EP"] = val["net_profit_ttm"] / (mv + _EPS)
    out["LNMV"] = np.log(mv + _EPS)

    # PB 36 个月分位（含当日在自身历史中的位置）
    pctl = {}
    for d in dates:
        hist = pb_daily.loc[:d].tail(750)  # ~36 个月交易日
        cur = hist.iloc[-1]
        pctl[d] = (hist < cur).sum() / hist.notna().sum()
    out["PBPCTL"] = pd.DataFrame(pctl).T

    out["RELPEIND"] = pe - _industry_median(pe, ind)
    out["RELPBIND"] = pb - _industry_median(pb, ind)
    return {k: v.reindex(dates) for k, v in out.items()}


# 依赖财报（income/balance/cashflow）的估值因子在 financials.py 中并入：
#   PE_DED / EV2EBITDA / PCF —— 见 compute_financial_factors()
FINANCIAL_VALUATION_CODES = ("PE_DED", "EV2EBITDA", "PCF")
