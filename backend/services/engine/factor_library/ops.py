"""因子库 —— 通达信/同花顺 & 聚宽技术指标算子（MyTT 口径，宽表向量化）。

口径约定（与 zoo 参考实现的差异均已标注）：
  - 全部算子作用于宽表（index=交易日, columns=symbol），只用到过去数据（无未来函数）；
  - 严格窗口纪律：rolling 类 min_periods=窗口（与 alpha_library 一致；zoo 原实现用
    min_periods=1 会把不完整窗口也算出来，训练侧会误导，不采用）；
  - 递归类（EMA / 中国式 SMA / RSI / TRIX / KTN / BullPower）按通达信语义从序列起点递推
    （ewm，不设窗口）；
  - 涨跌幅类用 daily_forward 前复权价；STD_TDX/BOLL/WR/CCI 用通达信 ddof=0；
  - 中国式 SMA(X,N,M) = ewm(alpha=M/N, adjust=False)（MyTT.SMA，与通达信一致）。

zoo 参考: qlib-factor-zoo/qlib/contrib/data/custom_ops.py @ ea21f31
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from numpy.lib.stride_tricks import sliding_window_view

_EPS = 1e-12


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def ema(df: pd.DataFrame, span: int) -> pd.DataFrame:
    return df.ewm(span=span, adjust=False).mean()


def sma_cn(df: pd.DataFrame, n: int, m: int = 1) -> pd.DataFrame:
    """中国式 SMA（通达信）：Y = (X·M + Y'·(N−M)) / N。"""
    return df.ewm(alpha=m / float(n), adjust=False).mean()


def ma(df: pd.DataFrame, n: int) -> pd.DataFrame:
    return df.rolling(n, min_periods=n).mean()


def std_tdx(df: pd.DataFrame, n: int) -> pd.DataFrame:
    return df.rolling(n, min_periods=n).std(ddof=0)


def ref(df: pd.DataFrame, n: int) -> pd.DataFrame:
    return df.shift(n)


def rolling_corr(a: pd.DataFrame, b: pd.DataFrame, n: int) -> pd.DataFrame:
    """滚动相关系数（窗口内完整样本，min_periods=n）。"""
    ma_, mb = a.rolling(n, min_periods=n).mean(), b.rolling(n, min_periods=n).mean()
    cov = (a * b).rolling(n, min_periods=n).mean() - ma_ * mb
    sa, sb = a.rolling(n, min_periods=n).std(), b.rolling(n, min_periods=n).std()
    return cov / (sa * sb + _EPS)


def _rolling_apply(frame: pd.DataFrame, w: int, fn, chunk: int = 256) -> pd.DataFrame:
    """滑窗逐窗聚合（fn 为 numpy 向量化函数，输入 (T-w+1, C, w)）。"""
    a = frame.to_numpy(dtype=np.float64)
    t, n = a.shape
    out = np.full((t, n), np.nan)
    if t >= w:
        sw = sliding_window_view(a, w, axis=0)
        for s in range(0, n, chunk):
            out[w - 1 :, s : s + chunk] = fn(sw[:, s : s + chunk, :])
    return pd.DataFrame(out, index=frame.index, columns=frame.columns)


def _series(values, ref_frame: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(
        np.asarray(values, dtype=float),
        index=ref_frame.index,
        columns=ref_frame.columns,
    )


# ---------------------------------------------------------------------------
# 波动 / 通道
# ---------------------------------------------------------------------------
def true_range(
    close: pd.DataFrame, high: pd.DataFrame, low: pd.DataFrame
) -> pd.DataFrame:
    pc = close.shift(1)
    tr = np.maximum(high - low, (high - pc).abs())
    return pd.DataFrame(
        np.maximum(tr.to_numpy(), (low - pc).abs().to_numpy()),
        index=close.index,
        columns=close.columns,
    )


def atr(
    close: pd.DataFrame, high: pd.DataFrame, low: pd.DataFrame, n: int
) -> pd.DataFrame:
    return true_range(close, high, low).rolling(n, min_periods=n).mean()


def rsv(
    close: pd.DataFrame, high: pd.DataFrame, low: pd.DataFrame, n: int
) -> pd.DataFrame:
    hhv = high.rolling(n, min_periods=n).max()
    llv = low.rolling(n, min_periods=n).min()
    return (close - llv) / (hhv - llv + _EPS) * 100.0


def boll(
    close: pd.DataFrame, n: int, p: float
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    mid = close.rolling(n, min_periods=n).mean()
    sd = std_tdx(close, n)
    return mid + p * sd, mid, mid - p * sd


def taq(
    high: pd.DataFrame, low: pd.DataFrame, n: int
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    up = high.rolling(n, min_periods=n).max()
    dn = low.rolling(n, min_periods=n).min()
    return up, (up + dn) / 2.0, dn


def ktn(close: pd.DataFrame, high: pd.DataFrame, low: pd.DataFrame, n: int, m: int):
    tr = true_range(close, high, low)
    a = tr.rolling(m, min_periods=m).mean()
    tp = (high + low + close) / 3.0
    mid = ema(tp, n)
    return mid + 2.0 * a, mid, mid - 2.0 * a


# ---------------------------------------------------------------------------
# 超买超卖 / 情绪
# ---------------------------------------------------------------------------
def rsi(close: pd.DataFrame, n: int) -> pd.DataFrame:
    """MyTT.RSI：SMA(max(diff,0),N,1) / SMA(|diff|,N,1) × 100（∈[0,100]）。

    注：zoo 参考实现用 up/dn 比值（无上界），与 MyTT/通达信不符，本项目按 MyTT 修正。
    """
    diff = close.diff(1)
    up = _series(np.where(diff > 0, diff, 0.0), close)
    absd = diff.abs()
    return sma_cn(up, n) / (sma_cn(absd, n) + _EPS) * 100.0


def bias(close: pd.DataFrame, n: int) -> pd.DataFrame:
    m = close.rolling(n, min_periods=n).mean()
    return (close - m) / (m + _EPS) * 100.0


def bbi(close: pd.DataFrame, m1: int, m2: int, m3: int, m4: int) -> pd.DataFrame:
    return (ma(close, m1) + ma(close, m2) + ma(close, m3) + ma(close, m4)) / 4.0


def wr(
    close: pd.DataFrame, high: pd.DataFrame, low: pd.DataFrame, n: int
) -> pd.DataFrame:
    hhv = high.rolling(n, min_periods=n).max()
    llv = low.rolling(n, min_periods=n).min()
    return (hhv - close) / (hhv - llv + _EPS) * 100.0


def cci(
    close: pd.DataFrame, high: pd.DataFrame, low: pd.DataFrame, n: int
) -> pd.DataFrame:
    tp = (high + low + close) / 3.0
    ma_tp = tp.rolling(n, min_periods=n).mean()
    avedev = _rolling_apply(
        tp, n, lambda w: np.abs(w - w.mean(axis=2, keepdims=True)).mean(axis=2)
    )
    return (tp - ma_tp) / (0.015 * avedev + _EPS)


def psy(close: pd.DataFrame, n: int) -> pd.DataFrame:
    up = close.gt(close.shift(1)).astype(float)
    up = up.where(close.shift(1).notna())  # 首日/停牌复牌不伪造涨跌
    return up.rolling(n, min_periods=n).mean() * 100.0


def psy_ma(close: pd.DataFrame, n: int, m: int) -> pd.DataFrame:
    return psy(close, n).rolling(m, min_periods=m).mean()


# ---------------------------------------------------------------------------
# 动量 / 趋势
# ---------------------------------------------------------------------------
def roc(close: pd.DataFrame, n: int) -> pd.DataFrame:
    r = close.shift(n)
    return (close - r) / (r + _EPS) * 100.0


def maroc(close: pd.DataFrame, n: int, m: int) -> pd.DataFrame:
    return roc(close, n).rolling(m, min_periods=m).mean()


def mtm(close: pd.DataFrame, n: int) -> pd.DataFrame:
    return close - close.shift(n)


def mtm_ma(close: pd.DataFrame, n: int, m: int) -> pd.DataFrame:
    return mtm(close, n).rolling(m, min_periods=m).mean()


def trix(close: pd.DataFrame, m1: int) -> pd.DataFrame:
    tr = ema(ema(ema(close, m1), m1), m1)
    return (tr - tr.shift(1)) / (tr.shift(1) + _EPS) * 100.0


def trma(close: pd.DataFrame, m1: int, m2: int) -> pd.DataFrame:
    return trix(close, m1).rolling(m2, min_periods=m2).mean()


def dmi(close: pd.DataFrame, high: pd.DataFrame, low: pd.DataFrame, m1: int, m2: int):
    """返回 (PDI, MDI, ADX, ADXR)（MyTT.DMI）。"""
    tr = true_range(close, high, low)
    hd = high - high.shift(1)
    ld = low.shift(1) - low
    dmp = _series(np.where((hd > 0) & (hd > ld), hd, 0.0), close)
    dmm = _series(np.where((ld > 0) & (ld > hd), ld, 0.0), close)
    tr_s = tr.rolling(m1, min_periods=m1).sum()
    pdi = dmp.rolling(m1, min_periods=m1).sum() * 100.0 / (tr_s + _EPS)
    mdi = dmm.rolling(m1, min_periods=m1).sum() * 100.0 / (tr_s + _EPS)
    dx = (mdi - pdi).abs() / (pdi + mdi + _EPS) * 100.0
    adx = dx.rolling(m2, min_periods=m2).mean()
    adxr = (adx + adx.shift(m2)) / 2.0
    return pdi, mdi, adx, adxr


def trix_trma_pair(close: pd.DataFrame, m1: int, m2: int):
    return trix(close, m1), trma(close, m1, m2)


# ---------------------------------------------------------------------------
# 量价
# ---------------------------------------------------------------------------
def vr(close: pd.DataFrame, volume: pd.DataFrame, m1: int) -> pd.DataFrame:
    d = close.diff(1)
    up = _series(np.where(d > 0, volume, 0.0), close)
    dn = _series(np.where(d <= 0, volume, 0.0), close)
    # 首日无 diff：两头都记 0
    first = close.shift(1).isna()
    up, dn = up.where(~first, 0.0), dn.where(~first, 0.0)
    return (
        up.rolling(m1, min_periods=m1).sum()
        / (dn.rolling(m1, min_periods=m1).sum() + _EPS)
        * 100.0
    )


def cr(
    close: pd.DataFrame, high: pd.DataFrame, low: pd.DataFrame, n: int
) -> pd.DataFrame:
    mid = ((high + low + close) / 3.0).shift(1)
    up = _series(np.maximum(0, high - mid), close)
    dn = _series(np.maximum(0, mid - low), close)
    return (
        up.rolling(n, min_periods=n).sum()
        / (dn.rolling(n, min_periods=n).sum() + _EPS)
        * 100.0
    )


def ar(
    open_: pd.DataFrame,
    close: pd.DataFrame,
    high: pd.DataFrame,
    low: pd.DataFrame,
    m1: int,
) -> pd.DataFrame:
    ho = high - open_
    ol = open_ - low
    return (
        ho.rolling(m1, min_periods=m1).sum()
        / (ol.rolling(m1, min_periods=m1).sum() + _EPS)
        * 100.0
    )


def br(
    close: pd.DataFrame, high: pd.DataFrame, low: pd.DataFrame, m1: int
) -> pd.DataFrame:
    pc = close.shift(1)
    up = _series(np.maximum(0, high - pc), close)
    dn = _series(np.maximum(0, pc - low), close)
    return (
        up.rolling(m1, min_periods=m1).sum()
        / (dn.rolling(m1, min_periods=m1).sum() + _EPS)
        * 100.0
    )


def obv(close: pd.DataFrame, volume: pd.DataFrame) -> pd.DataFrame:
    d = close.diff(1)
    direction = _series(np.where(d > 0, volume, np.where(d < 0, -volume, 0.0)), close)
    direction = direction.where(close.shift(1).notna(), np.nan)
    return direction.cumsum()


def mfi(
    close: pd.DataFrame,
    high: pd.DataFrame,
    low: pd.DataFrame,
    volume: pd.DataFrame,
    n: int,
) -> pd.DataFrame:
    typ = (high + low + close) / 3.0
    ref = typ.shift(1)
    mf = typ * volume
    pos = _series(np.where(typ > ref, mf, 0.0), close)
    neg = _series(np.where(typ < ref, mf, 0.0), close)
    pos_s = pos.rolling(n, min_periods=n).sum()
    neg_s = neg.rolling(n, min_periods=n).sum()
    return 100.0 - 100.0 / (1.0 + pos_s / (neg_s + _EPS))


def vpt(close: pd.DataFrame, volume: pd.DataFrame) -> pd.DataFrame:
    pc = close.shift(1)
    daily = volume * (close - pc) / (pc + _EPS)
    daily = daily.where(pc.notna())
    return daily.cumsum()


def wvad(
    open_: pd.DataFrame,
    close: pd.DataFrame,
    high: pd.DataFrame,
    low: pd.DataFrame,
    volume: pd.DataFrame,
    n: int,
) -> pd.DataFrame:
    w = (close - open_) / (high - low + _EPS) * volume
    return w.rolling(n, min_periods=n).sum()


def vosc(volume: pd.DataFrame, n1: int, n2: int) -> pd.DataFrame:
    a = volume.rolling(n1, min_periods=n1).mean()
    b = volume.rolling(n2, min_periods=n2).mean()
    return (a - b) / (a + _EPS) * 100.0


def money_flow(
    close: pd.DataFrame, volume: pd.DataFrame, vwap: pd.DataFrame, n: int
) -> pd.DataFrame:
    mf = vwap * volume * np.sign(close.pct_change())
    return mf.rolling(n, min_periods=n).sum()


def emv(
    high: pd.DataFrame, low: pd.DataFrame, volume: pd.DataFrame, n: int
) -> pd.DataFrame:
    hl = high + low
    mid = 100.0 * (hl - hl.shift(1)) / (hl + _EPS)
    vol_ma = volume.rolling(n, min_periods=n).mean()
    hl_ma = (high - low).rolling(n, min_periods=n).mean()
    raw = mid * (vol_ma / (volume + _EPS)) * (high - low) / (hl_ma + _EPS)
    return raw.rolling(n, min_periods=n).mean()


def maemv(
    high: pd.DataFrame, low: pd.DataFrame, volume: pd.DataFrame, n: int, m: int
) -> pd.DataFrame:
    return emv(high, low, volume, n).rolling(m, min_periods=m).mean()


def mass(high: pd.DataFrame, low: pd.DataFrame, n1: int, n2: int) -> pd.DataFrame:
    hl = high - low
    ma1 = hl.rolling(n1, min_periods=n1).mean()
    ma2 = ma1.rolling(n1, min_periods=n1).mean()
    return (ma1 / (ma2 + _EPS)).rolling(n2, min_periods=n2).sum()


def ma_mass(
    high: pd.DataFrame, low: pd.DataFrame, n1: int, n2: int, m: int
) -> pd.DataFrame:
    return mass(high, low, n1, n2).rolling(m, min_periods=m).mean()


def dpo(close: pd.DataFrame, m1: int, m2: int) -> pd.DataFrame:
    return close - close.rolling(m1, min_periods=m1).mean().shift(m2)


def madpo(close: pd.DataFrame, m1: int, m2: int, m3: int) -> pd.DataFrame:
    return dpo(close, m1, m2).rolling(m3, min_periods=m3).mean()


def dfma_dif(close: pd.DataFrame, n1: int, n2: int) -> pd.DataFrame:
    return ma(close, n1) - ma(close, n2)


def dfma_difma(close: pd.DataFrame, n1: int, n2: int, m: int) -> pd.DataFrame:
    return dfma_dif(close, n1, n2).rolling(m, min_periods=m).mean()


# ---------------------------------------------------------------------------
# 聚宽（JQ110）补充算子
# ---------------------------------------------------------------------------
def aroon_up(high: pd.DataFrame, n: int) -> pd.DataFrame:
    return _rolling_apply(high, n, lambda w: (np.argmax(w, axis=2) + 1) / n * 100.0)


def aroon_down(low: pd.DataFrame, n: int) -> pd.DataFrame:
    return _rolling_apply(low, n, lambda w: (np.argmin(w, axis=2) + 1) / n * 100.0)


def bull_power(high: pd.DataFrame, close: pd.DataFrame, n: int) -> pd.DataFrame:
    return high - ema(close, n)


def bear_power(low: pd.DataFrame, close: pd.DataFrame, n: int) -> pd.DataFrame:
    return low - ema(close, n)


def variance(close: pd.DataFrame, n: int) -> pd.DataFrame:
    return close.pct_change().rolling(n, min_periods=n).var()


def skewness(close: pd.DataFrame, n: int) -> pd.DataFrame:
    return close.pct_change().rolling(n, min_periods=n).skew()


def kurtosis(close: pd.DataFrame, n: int) -> pd.DataFrame:
    return close.pct_change().rolling(n, min_periods=n).kurt()


def sharpe_ratio(close: pd.DataFrame, n: int) -> pd.DataFrame:
    rets = close.pct_change()
    mean = rets.rolling(n, min_periods=n).mean()
    std = rets.rolling(n, min_periods=n).std()
    return mean / (std + _EPS) * np.sqrt(250.0)


def price_rank(close: pd.DataFrame, n: int) -> pd.DataFrame:
    lo = close.rolling(n, min_periods=n).min()
    hi = close.rolling(n, min_periods=n).max()
    return (close - lo) / (hi - lo + _EPS)


def cumulative_range(
    high: pd.DataFrame, low: pd.DataFrame, close: pd.DataFrame, n: int
) -> pd.DataFrame:
    hhv = high.rolling(n, min_periods=n).max()
    llv = low.rolling(n, min_periods=n).min()
    return (hhv - llv) / (close + _EPS)


def slope(df: pd.DataFrame, n: int) -> pd.DataFrame:
    """滚动线性回归斜率（最小二乘，x=0..n-1）。"""
    x = np.arange(n, dtype=float)
    x_mean = x.mean()
    denom = ((x - x_mean) ** 2).sum()

    def _fn(w):  # w: (T-n+1, C, n)
        y_mean = w.mean(axis=2, keepdims=True)
        return ((w - y_mean) * (x - x_mean)).sum(axis=2) / denom

    return _rolling_apply(df, n, _fn)
