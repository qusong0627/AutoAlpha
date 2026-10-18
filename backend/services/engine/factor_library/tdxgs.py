"""TDXGS —— 通达信/同花顺技术指标因子库（88 个，MyTT 口径）。

因子名与表达式对照见 data/quantdb/.../alpha_library/expressions/05_tdxgs.md
（来源 qlib-factor-zoo handler.py @ ea21f31）；全部仅依赖日线 OHLCV。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from backend.services.engine.factor_library import ops

_EPS = 1e-12


def compute(daily: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """返回 {factor_name: 宽表(交易日 × symbol)}，共 88 个。"""
    o, h, lo, c, v = (
        daily["open"],
        daily["high"],
        daily["low"],
        daily["close"],
        daily["volume"],
    )
    out: dict[str, pd.DataFrame] = {}

    # 1. EMA / 2. MA
    for n in (5, 10, 12, 20, 26, 50, 60):
        out[f"TDXGS_EMA_{n:02d}"] = ops.ema(c, n)
    for n in (5, 10, 20, 60):
        out[f"TDXGS_MA_{n:02d}"] = ops.ma(c, n)

    # 3. ATR
    for n in (10, 14, 20, 60):
        out[f"TDXGS_ATR_{n:02d}"] = ops.atr(c, h, lo, n)

    # 4. RSI / 5. BIAS / 6. BBI / 7. WR / 8. CCI
    for n in (6, 12, 14, 24):
        out[f"TDXGS_RSI_{n:02d}"] = ops.rsi(c, n)
    for n in (6, 12, 24):
        out[f"TDXGS_BIAS_{n:02d}"] = ops.bias(c, n)
    out["TDXGS_BBI"] = ops.bbi(c, 3, 6, 12, 24)
    for n in (6, 10):
        out[f"TDXGS_WR_{n:02d}"] = ops.wr(c, h, lo, n)
    for n in (14, 20):
        out[f"TDXGS_CCI_{n:02d}"] = ops.cci(c, h, lo, n)

    # 9. DMI
    for m1, m2 in ((14, 6), (7, 3)):
        pdi, mdi, adx, adxr = ops.dmi(c, h, lo, m1, m2)
        out[f"TDXGS_PDI_{m1}_{m2}"] = pdi
        out[f"TDXGS_MDI_{m1}_{m2}"] = mdi
        out[f"TDXGS_ADX_{m1}_{m2}"] = adx
        out[f"TDXGS_ADXR_{m1}_{m2}"] = adxr

    # 10. BOLL
    for n in (20, 26):
        up, mid, dn = ops.boll(c, n, 2.0)
        out[f"TDXGS_BOLL_UP_{n:02d}"] = up
        out[f"TDXGS_BOLL_MID_{n:02d}"] = mid
        out[f"TDXGS_BOLL_DN_{n:02d}"] = dn

    # 11. PSY
    out["TDXGS_PSY_12"] = ops.psy(c, 12)
    out["TDXGS_PSYMA_12_6"] = ops.psy_ma(c, 12, 6)
    out["TDXGS_PSY_20"] = ops.psy(c, 20)
    out["TDXGS_PSYMA_20_10"] = ops.psy_ma(c, 20, 10)

    # 12. ROC / 13. MTM / 14. TRIX
    out["TDXGS_ROC_12"] = ops.roc(c, 12)
    out["TDXGS_MAROC_12_6"] = ops.maroc(c, 12, 6)
    out["TDXGS_MTM_12"] = ops.mtm(c, 12)
    out["TDXGS_MTMMA_12_6"] = ops.mtm_ma(c, 12, 6)
    for m1, m2 in ((12, 20), (9, 15)):
        out[f"TDXGS_TRIX_{m1}"] = ops.trix(c, m1)
        out[f"TDXGS_TRMA_{m1}_{m2}"] = ops.trma(c, m1, m2)

    # 15. VR / 16. CR / 17. AR & BR
    for m1 in (26, 12):
        out[f"TDXGS_VR_{m1:02d}"] = ops.vr(c, v, m1)
    for n in (20, 26):
        out[f"TDXGS_CR_{n:02d}"] = ops.cr(c, h, lo, n)
    out["TDXGS_AR_26"] = ops.ar(o, c, h, lo, 26)
    out["TDXGS_BR_26"] = ops.br(c, h, lo, 26)

    # 18. OBV / 19. MFI
    out["TDXGS_OBV"] = ops.obv(c, v)
    for n in (14, 9):
        out[f"TDXGS_MFI_{n:02d}"] = ops.mfi(c, h, lo, v, n)

    # 20. DPO / 21. TAQ / 22. KTN
    out["TDXGS_DPO_20_10"] = ops.dpo(c, 20, 10)
    out["TDXGS_MADPO_20_10_6"] = ops.madpo(c, 20, 10, 6)
    for n in (20, 50):
        up, mid, dn = ops.taq(h, lo, n)
        out[f"TDXGS_TAQ_UP_{n:02d}"] = up
        out[f"TDXGS_TAQ_MID_{n:02d}"] = mid
        out[f"TDXGS_TAQ_DN_{n:02d}"] = dn
    kup, kmid, kdn = ops.ktn(c, h, lo, 20, 10)
    out["TDXGS_KTN_UP"], out["TDXGS_KTN_MID"], out["TDXGS_KTN_DN"] = kup, kmid, kdn

    # 23. EMV / 24. MASS / 25. DFMA / 26. STD
    out["TDXGS_EMV_14"] = ops.emv(h, lo, v, 14)
    out["TDXGS_MAEMV_14_9"] = ops.maemv(h, lo, v, 14, 9)
    out["TDXGS_MASS_9_25"] = ops.mass(h, lo, 9, 25)
    out["TDXGS_MA_MASS_9_25_6"] = ops.ma_mass(h, lo, 9, 25, 6)
    out["TDXGS_DFMA_DIF"] = ops.dfma_dif(c, 10, 50)
    out["TDXGS_DFMA_DIFMA"] = ops.dfma_difma(c, 10, 50, 10)
    for n in (10, 20):
        out[f"TDXGS_STD_{n:02d}"] = ops.std_tdx(c, n)

    # 27. 加工因子
    atr20 = ops.atr(c, h, lo, 20)
    ma20 = ops.ma(c, 20)
    up20, mid20, dn20 = ops.boll(c, 20, 2.0)
    out["TDXGS_ATR20_RATIO"] = atr20 / (c + _EPS)
    out["TDXGS_BOLL_BANDWIDTH"] = (up20 - dn20) / (mid20 + _EPS)
    out["TDXGS_EMA20_CSRANK"] = ops.ema(c, 20).rank(axis=1, pct=True)  # 截面分位
    out["TDXGS_CORR_CV_20"] = ops.rolling_corr(c, v, 20)
    vol_ma20 = ops.ma(v, 20)
    out["TDXGS_VOL_RATIO_20"] = v / (vol_ma20 + _EPS)
    out["TDXGS_AMPLITUDE"] = (h - lo) / (o + _EPS)
    out["TDXGS_TREND_STRENGTH"] = (c - ma20).abs() / (atr20 + _EPS)

    assert len(out) == 88, f"TDXGS 因子数 {len(out)} != 88"
    return out
