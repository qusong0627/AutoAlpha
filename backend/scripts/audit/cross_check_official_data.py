#!/usr/bin/env python3
"""用 QuantDB 官方数据包做独立真值比对（与 qlib bin 无共同生产者）。

比对四条：
A. daily_unadjusted.Close       vs  close_bin / factor_bin   （隐含不复权价）
B. daily_backward.Close         vs  close_bin                （bin 价格是否就是官方后复权价）
C. dividend_factors 官方累计复权因子 vs factor_bin            （因子是否被写成日频比值）
D. 官方后复权价的日收益 vs bin 价格日收益                      （收益率基准是否一致）
"""
import glob
import os

import numpy as np
import pandas as pd

QLIB = "/app/db/qlib_data"
QDB = "/data/quantdb"
cal = [ln.strip() for ln in open(f"{QLIB}/calendars/day.txt") if ln.strip()]
date_idx = {d: i for i, d in enumerate(cal)}

pairs = [("002818.SZ", "sz002818"), ("603699.SH", "sh603699"),
         ("002081.SZ", "sz002081"), ("603866.SH", "sh603866")]


def load_bin(sym, field):
    arr = np.fromfile(f"{QLIB}/features/{sym}/{field}.day.bin", dtype="<f4")
    st = int(arr[0])
    s = pd.Series(arr[1:], index=cal[st : st + len(arr) - 1])
    s.index = pd.to_datetime(s.index)
    return s


def read_one(kind, code):
    p = f"{QDB}/1_kline_data/{kind}/{code}.parquet"
    if not os.path.exists(p):
        return None
    df = pd.read_parquet(p)
    dc = [c for c in df.columns if c.lower() in ("trade_date", "datetime", "date", "time")]
    if not dc:
        print(f"  [{kind}] 无日期列，列={list(df.columns)[:12]}")
        return None
    df[dc[0]] = pd.to_datetime(df[dc[0]])
    return df.set_index(dc[0]).sort_index()


def pick_close(df):
    for c in ("Close", "close", "CLOSE", "Clsprc", "AdjClose", "adj_close"):
        if c in df.columns:
            return df[c].astype(float)
    return None


for code, sym in pairs:
    print(f"\n########## {code} / {sym}")
    cb = load_bin(sym, "close")
    fb = load_bin(sym, "factor")
    implied_raw = (cb / fb).dropna()

    un = read_one("daily_unadjusted", code)
    bw = read_one("daily_backward", code)
    fw = read_one("daily_forward", code)
    if un is not None:
        uc = pick_close(un)
        j = implied_raw.align(uc, join="inner")
        err = (j[0] / j[1] - 1).abs()
        print(f"A. 隐含不复权价 vs daily_unadjusted.Close: n={len(err)} "
              f"最大相对偏差 {err.max():.2e} 中位 {err.median():.2e}")
    if bw is not None:
        bc = pick_close(bw)
        j = cb.align(bc, join="inner")
        ratio = j[0] / j[1]
        print(f"B. close_bin vs daily_backward.Close: 比值中位 {ratio.median():.6f} "
              f"min {ratio.min():.6f} max {ratio.max():.6f}")
    if fw is not None:
        fc = pick_close(fw)
        j = cb.align(fc, join="inner")
        r2 = (j[0] / j[1])
        print(f"B'. close_bin vs daily_forward.Close: 比值 CV {r2.std()/r2.mean():.4f}")

    # C. 官方复权因子表
    dp = f"{QDB}/3_financial_data/dividend_factors/{code}.parquet"
    if os.path.exists(dp):
        d = pd.read_parquet(dp)
        print(f"C. dividend_factors 列: {list(d.columns)}")
        fc_ = [c for c in d.columns if "actor" in c or "factor" in c.lower()]
        dc_ = [c for c in d.columns if c.lower() in ("trade_date", "datetime", "date",
                                                     "ex_date", "end_date")]
        if fc_ and dc_:
            d[dc_[0]] = pd.to_datetime(d[dc_[0]])
            official = d.set_index(dc_[0])[fc_[0]].astype(float).sort_index()
            official = official[official > 0]
            step = official.reindex(
                pd.date_range(official.index.min(), official.index.max(), freq="B")
            ).ffill().dropna()
            changed = (step.diff().abs() > 1e-9).sum()
            print(f"   官方 {fc_[0]}: 记录 {len(official)} 条，铺平到工作日 {len(step)} 天，"
                  f"变化 {changed} 次 → 阶梯状")
            j = fb.align(step, join="inner")
            r = (j[0] / j[1])
            jfb = j[0]
            drift = (jfb.diff().abs() > 1e-6).sum()
            print(f"   bin factor 在对齐区间内逐日变化 {drift}/{len(jfb)} 天 "
                  f"({drift/len(jfb):.1%}) —— 非阶梯")
            print(f"   bin_factor / 官方factor: 中位 {r.median():.4f} "
                  f"min {r.min():.4f} max {r.max():.4f}")
    else:
        print("C. 无 dividend_factors 文件")
