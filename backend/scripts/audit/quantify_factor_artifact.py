#!/usr/bin/env python3
"""用官方分红送配表(dividend_factors)重建「阶梯状乘法后复权因子」，
量化 qlib bin 里 $factor 的日频漂移有多少是真实公司行为、多少是纯数据缺陷。
"""
import os

import numpy as np
import pandas as pd

QLIB = "/app/db/qlib_data"
QDB = "/data/quantdb"
cal = [ln.strip() for ln in open(f"{QLIB}/calendars/day.txt") if ln.strip()]
cald = pd.to_datetime(pd.Series(cal))

pairs = [("002818.SZ", "sz002818"), ("603699.SH", "sh603699"),
         ("002081.SZ", "sz002081"), ("603866.SH", "sh603866")]


def load_bin(sym, field):
    arr = np.fromfile(f"{QLIB}/features/{sym}/{field}.day.bin", dtype="<f4")
    st = int(arr[0])
    s = pd.Series(arr[1:].astype(np.float64), index=cald[st : st + len(arr) - 1])
    return s


for code, sym in pairs:
    dp = f"{QDB}/3_financial_data/dividend_factors/{code}.parquet"
    if not os.path.exists(dp):
        continue
    d = pd.read_parquet(dp)
    d["time"] = pd.to_datetime(d["time"])
    d = d.sort_values("time")
    fb = load_bin(sym, "factor").dropna()
    cb = load_sym_close = load_bin(sym, "close").dropna()
    raw = (cb / fb).dropna()

    print(f"\n########## {code} / {sym}  事件表 {len(d)} 条")
    print(d.tail(6).to_string(index=False))

    # 事件日是否落在交易日；bin factor 在事件日附近的跳变 vs 平时的漂移
    ev_days = set(d["time"].dt.normalize())
    chg = fb.diff()
    big = fb[(chg.abs() / fb.shift(1)).abs() > 5e-3]
    small = fb[(chg.abs() / fb.shift(1)).abs() <= 5e-3].dropna()
    n_small_chg = int((chg.dropna()[(chg.abs() / fb.shift(1)).abs() <= 5e-3] != 0).sum())
    print(f"  bin factor: 总交易日 {len(fb)}；大跳变(>0.5%) {len(big)} 次；"
          f"小漂移(<=0.5% 且非零) {n_small_chg} 次")
    in_ev = sum(1 for dt in big.index if dt.normalize() in ev_days)
    print(f"  大跳变中落在事件表日期上的: {in_ev}/{len(big)}")

    # 用事件表算真实送转比例 vs bin factor 跳变
    for dt, row in list(d.iterrows())[-4:]:
        dt = dt.normalize() if isinstance(dt, pd.Timestamp) else pd.Timestamp(row["time"]).normalize()
        try:
            i = fb.index.get_loc(dt)
        except KeyError:
            continue
        if i == 0:
            continue
        r = row.to_dict()
        jump = float(fb.iloc[i] / fb.iloc[i - 1] - 1)
        bonus = float(r.get("stockBonus") or 0) / 10
        allot = float(r.get("allotment") or 0) / 10
        interest = float(r.get("interest") or 0) / 10
        print(f"   事件 {dt.date()} 派{interest:.3f} 送{bonus:.3f} 配{allot:.3f}"
              f"(配价{r.get('allotPrice')}) -> bin factor 跳变 {jump:+.4%}")

    # 无事件区间内的因子漂移 => 纯数据缺陷
    seg = fb[(fb.index >= "2025-01-01")]
    if len(seg) > 100:
        tot_drift = float(np.prod(seg.values[1:] / seg.values[:-1]) - 1)
        ev_in = [t for t in ev_days if pd.Timestamp("2025-01-01") <= t <= seg.index[-1]]
        legit = 1.0
        for t in ev_in:
            try:
                i = seg.index.get_loc(t)
            except KeyError:
                continue
            if i > 0:
                legit *= float(seg.iloc[i] / seg.iloc[i - 1])
        print(f"  2025-01-01 以来 bin factor 总变化 {tot_drift:+.2%}"
              f"；其中落在 {len(ev_in)} 个事件日上的变化 {legit-1:+.2%}"
              f"；非事件日漂移 {tot_drift - (legit-1):+.2%} ← 纯数据缺陷")
