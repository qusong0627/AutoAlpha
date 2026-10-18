"""JQ110 —— 聚宽策略因子库（109 个，五组：动量/情绪量能/技术/风险/风格）。

因子名与表达式对照见 data/quantdb/.../alpha_library/expressions/06_jq110.md
（来源 qlib-factor-zoo handler.py @ ea21f31）。

与 zoo 参考实现的既定偏差（均因 QuantDB 有更真实的数据源，且因子语义就是如此）：
  - 成交额类（TVMA/TVSTD/money_flow）用真实 amount（元）而非 vwap×volume 代理；
  - 换手率类（share_turnover_*）用真实换手 amount/流通市值，而非总手数代理；
  - liquidity 用日均成交额（亿元），而非成交量/1e6；
  - beta 用对中证500（000905.SH）的真实回归 β，而非 close 的 Slope 近似。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from backend.services.engine.factor_library import ops

_EPS = 1e-12


def compute(
    daily: dict[str, pd.DataFrame],
    amount: pd.DataFrame,
    turnover: pd.DataFrame,
    vwap: pd.DataFrame,
    market_ret: pd.Series,
) -> dict[str, pd.DataFrame]:
    """返回 {factor_name: 宽表}，共 109 个。

    amount: 元（名义成交额）；turnover: 日换手（amount/流通市值）；vwap: 名义口径（成交额/成交量）；
    market_ret: 基准指数日收益（与面板日期对齐）。
    """
    o, h, lo, c, v = (
        daily["open"],
        daily["high"],
        daily["low"],
        daily["close"],
        daily["volume"],
    )
    mkt = pd.DataFrame(
        np.repeat(market_ret.reindex(c.index).to_numpy()[:, None], c.shape[1], axis=1),
        index=c.index,
        columns=c.columns,
    )
    out: dict[str, pd.DataFrame] = {}

    # ===== 第1组：MOMENTUM 动量 =====
    for w_ in (6, 12, 20, 60, 120):
        out[f"JQ110_ROC_{w_:03d}"] = ops.roc(c, w_)
    for w_ in (5, 10, 20, 60):
        m = ops.ma(c, w_)
        out[f"JQ110_BIAS_{w_:02d}"] = c / (m + _EPS) - 1
    out["JQ110_aroon_up_25"] = ops.aroon_up(h, 25)
    out["JQ110_aroon_down_25"] = ops.aroon_down(lo, 25)
    bbi = ops.bbi(c, 3, 6, 12, 24)
    out["JQ110_BBI"] = bbi
    out["JQ110_BBIC"] = c / (bbi + _EPS) - 1
    for w_ in (10, 15, 20, 88):
        out[f"JQ110_CCI_{w_:03d}"] = ops.cci(c, h, lo, w_)
    out["JQ110_CR20"] = ops.cr(c, h, lo, 20)
    out["JQ110_MASS"] = ops.mass(h, lo, 9, 25)
    for w_ in (5, 10):
        out[f"JQ110_TRIX_{w_:02d}"] = ops.trix(c, w_)
    out["JQ110_Price1M"] = c / (c.shift(20) + _EPS) - 1
    out["JQ110_Price3M"] = c / (c.shift(60) + _EPS) - 1
    out["JQ110_Price1Y"] = c / (c.shift(250) + _EPS) - 1
    out["JQ110_Rank1M"] = ops.price_rank(c, 20)
    out["JQ110_52week_rank"] = ops.price_rank(c, 250)
    out["JQ110_bull_power"] = ops.bull_power(h, c, 13)
    out["JQ110_bear_power"] = ops.bear_power(lo, c, 13)
    vpt = ops.vpt(c, v)
    dvpt = vpt.diff(1)
    out["JQ110_VPT"] = vpt
    out["JQ110_single_day_VPT"] = dvpt
    out["JQ110_single_day_VPT_06"] = dvpt.rolling(6, min_periods=6).mean()
    out["JQ110_single_day_VPT_12"] = dvpt.rolling(12, min_periods=12).mean()
    out["JQ110_Volume1M"] = (v / (ops.ma(v, 20) + _EPS)) * (
        c / (c.shift(20) + _EPS) - 1
    )
    for w_ in (6, 12, 24):
        out[f"JQ110_PLRC_{w_:02d}"] = ops.slope(c, w_) / (c + _EPS)

    # ===== 第2组：EMOTION 情绪量能 =====
    vol250 = ops.ma(v, 250)
    for w_ in (5, 10, 20, 60, 120, 240):
        out[f"JQ110_VOL_{w_:03d}"] = ops.ma(v, w_) / (vol250 + _EPS)
    vol120 = ops.ma(v, 120)
    for w_ in (5, 10, 20):
        out[f"JQ110_DAVOL_{w_:02d}"] = ops.ma(v, w_) / (vol120 + _EPS)
    v_ma20 = ops.ma(v, 20)
    out["JQ110_turnover_volatility"] = v.rolling(20, min_periods=20).std() / (
        v_ma20 + _EPS
    )
    for w_ in (6, 20):
        out[f"JQ110_TVMA_{w_:02d}"] = ops.ma(amount, w_)
        out[f"JQ110_TVSTD_{w_:02d}"] = amount.rolling(w_, min_periods=w_).std()
    for w_ in (5, 10, 12, 26):
        out[f"JQ110_VEMA_{w_:02d}"] = ops.ema(v, w_)
    for w_ in (10, 20):
        out[f"JQ110_VSTD_{w_:02d}"] = v.rolling(w_, min_periods=w_).std()
    jq_ar = ops.ar(o, c, h, lo, 20)
    jq_br = ops.br(c, h, lo, 20)
    out["JQ110_AR"] = jq_ar
    out["JQ110_BR"] = jq_br
    out["JQ110_ARBR"] = jq_ar / (jq_br + _EPS)
    for w_ in (6, 14):
        out[f"JQ110_ATR_{w_:02d}"] = ops.atr(c, h, lo, w_)
    out["JQ110_PSY"] = ops.psy(c, 12)
    ev12, ev26 = ops.ema(v, 12), ops.ema(v, 26)
    vdiff = ev12 - ev26
    vdea = ops.ema(vdiff, 9)
    out["JQ110_VDIFF"] = vdiff
    out["JQ110_VDEA"] = vdea
    out["JQ110_VMACD"] = 2 * (vdiff - vdea)
    out["JQ110_VOSC"] = ops.vosc(v, 5, 20)
    out["JQ110_VR"] = ops.vr(c, v, 20)
    for w_ in (6, 12):
        out[f"JQ110_VROC_{w_:02d}"] = (v - v.shift(w_)) / (v.shift(w_) + _EPS)
    wvad = ops.wvad(o, c, h, lo, v, 24)
    out["JQ110_WVAD"] = wvad
    out["JQ110_MAWVAD"] = wvad.rolling(6, min_periods=6).mean()
    out["JQ110_money_flow_20"] = ops.money_flow(c, v, vwap, 20)

    # ===== 第3组：TECHNICAL 技术指标 =====
    out["JQ110_EMA5"] = ops.ema(c, 5)
    for w_ in (10, 12, 20, 26, 120):
        out[f"JQ110_EMAC_{w_:03d}"] = ops.ema(c, w_) / (c + _EPS) - 1
    for w_ in (5, 10, 20, 60, 120):
        out[f"JQ110_MAC_{w_:03d}"] = ops.ma(c, w_) / (c + _EPS) - 1
    out["JQ110_MACDC"] = (ops.ema(c, 12) - ops.ema(c, 26)) / (c + _EPS)
    bup, _, bdn = ops.boll(c, 20, 2.0)
    out["JQ110_boll_up"] = bup / (c + _EPS) - 1
    out["JQ110_boll_down"] = bdn / (c + _EPS) - 1
    out["JQ110_MFI14"] = ops.mfi(c, h, lo, v, 14)

    # ===== 第4组：RISK 风险统计 =====
    for w_ in (20, 60, 120):
        out[f"JQ110_Variance_{w_:03d}"] = ops.variance(c, w_)
        out[f"JQ110_sharpe_ratio_{w_:03d}"] = ops.sharpe_ratio(c, w_)
        out[f"JQ110_Skewness_{w_:03d}"] = ops.skewness(c, w_)
        out[f"JQ110_Kurtosis_{w_:03d}"] = ops.kurtosis(c, w_)

    # ===== 第5组：STYLE 风格 =====
    ret = c.pct_change()
    out["JQ110_daily_std"] = ret.rolling(20, min_periods=20).std()
    out["JQ110_hist_sigma"] = ret.rolling(120, min_periods=120).std()
    out["JQ110_residual_vol"] = ret.rolling(60, min_periods=60).std()
    out["JQ110_cumulative_range"] = ops.cumulative_range(h, lo, c, 20)
    out["JQ110_momentum"] = c / (c.shift(250) + _EPS) - 1
    out["JQ110_liquidity"] = ops.ma(amount, 20) / 1e8  # 日均成交额（亿元）
    out["JQ110_share_turnover_monthly"] = turnover.rolling(20, min_periods=20).sum()
    out["JQ110_share_turnover_annual"] = turnover.rolling(250, min_periods=250).mean()
    out["JQ110_share_turnover_quarterly"] = turnover.rolling(60, min_periods=60).mean()
    # 真实市场 β（120 日；cov(ret, mkt)/var(mkt)）
    ma_r, ma_m = (
        ret.rolling(120, min_periods=120).mean(),
        mkt.rolling(120, min_periods=120).mean(),
    )
    cov = (ret * mkt).rolling(120, min_periods=120).mean() - ma_r * ma_m
    var_m = (mkt**2).rolling(120, min_periods=120).mean() - ma_m**2
    out["JQ110_beta"] = cov / (var_m + _EPS)

    assert len(out) == 109, f"JQ110 因子数 {len(out)} != 109"
    return out
