"""验证 rebuild_qlib_multiplicative_factor 的核心前提与产出质量。

前提：qlib bin 里的 $change 必须是**含权**涨跌幅（除权日分母是交易所除权参考价），
否则 cumprod(1+$change) 会丢掉全部分红，等价于不复权。

判据：
1. 逐日比较 change 与「不复权价涨跌幅」，|差| > 1e-4 的日子应当
   (a) 数量很少（≈除权除息日数），且
   (b) 与官方 dividend_factors 的事件日期吻合，且
   (c) 符号为正（含权涨跌幅 > 裸价涨跌幅，因为参考价 < 昨收）。
2. 用工具同一套代码算出的 f_new，与用官方 dividend_factors 按标准公式重建的 f_true
   应当一致（阶梯位置相同、幅度接近）。
"""
import os
import random
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, "/tmp")
from rebuild_qlib_multiplicative_factor import build_multiplicative_basis, read_bin  # noqa: E402

QLIB = "/app/db/qlib_data"
QDB = "/data/quantdb"

cal = [ln.strip() for ln in open(f"{QLIB}/calendars/day.txt") if ln.strip()]
cald = pd.to_datetime(pd.Series(cal))


def series(sym, field):
    got = read_bin(Path(f"{QLIB}/features/{sym}/{field}.day.bin"))
    if got is None:
        return None
    st, arr = got
    return pd.Series(arr, index=cald[st : st + arr.size])


syms = sorted(d for d in os.listdir(f"{QLIB}/features") if os.path.isdir(f"{QLIB}/features/{d}"))
random.seed(11)
random.shuffle(syms)

n_ok = 0
rows = []
jump_rows = []
for sym in syms:
    if n_ok >= 60:
        break
    code = f"{sym[2:]}.{sym[:2].upper()}"
    dp = f"{QDB}/3_financial_data/dividend_factors/{code}.parquet"
    up = f"{QDB}/1_kline_data/daily_unadjusted/{code}.parquet"
    if not (os.path.exists(dp) and os.path.exists(up)):
        continue
    cb, fb, ch = series(sym, "close"), series(sym, "factor"), series(sym, "change")
    if cb is None or fb is None or ch is None:
        continue
    # 三个 bin 的实际长度可以不同（各字段最后有效日不一致），统一到 close 轴上再喂给工具
    fb = fb.reindex(cb.index)
    ch = ch.reindex(cb.index)
    try:
        basis = build_multiplicative_basis(cb.to_numpy(), fb.to_numpy(), ch.to_numpy())
    except ValueError:
        continue
    raw = pd.Series(basis["raw"], index=cb.index)
    f_new = pd.Series(basis["factor_new"], index=cb.index)
    vraw = raw[raw > 0]
    raw_ret = vraw.pct_change()
    diff = (ch.reindex(vraw.index) - raw_ret).dropna()
    ex_like = diff[diff.abs() > 1e-4]

    d = pd.read_parquet(dp)
    d["time"] = pd.to_datetime(d["time"])
    ev = set(pd.DatetimeIndex(d["time"]).normalize())
    inter = ex_like.index.intersection(pd.DatetimeIndex(sorted(ev)))
    if len(ex_like) == 0 or len(ev) == 0:
        continue

    # 官方事件表重建标准乘法因子
    u = pd.read_parquet(up)
    tcol = next(c for c in u.columns if c.lower() in ("time", "trade_date", "datetime", "date"))
    u[tcol] = pd.to_datetime(u[tcol])
    uraw = u.set_index(tcol)["close"].astype(float).sort_index()
    uraw = uraw[uraw > 0]
    ft = pd.Series(1.0, index=uraw.index)
    cum = 1.0
    for _, r in d.sort_values("time").iterrows():
        exd = pd.Timestamp(r["time"]).normalize()
        prev = uraw.index[uraw.index < exd]
        if len(prev) == 0:
            continue
        p0 = float(uraw.loc[prev[-1]])
        bonus = float(r.get("stockBonus") or 0) / 10.0
        allot = float(r.get("allotment") or 0) / 10.0
        cash = float(r.get("interest") or 0) / 10.0
        aprice = float(r.get("allotPrice") or 0) / 10.0
        ref = (p0 - cash + aprice * allot) / (1.0 + bonus + allot)
        if ref <= 0:
            continue
        cum *= p0 / ref
        ft.loc[ft.index >= exd] = cum
    j = f_new.dropna().align(ft, join="inner")
    a, b = j[0] / j[0].iloc[0], j[1] / j[1].iloc[0]
    rel = (a / b - 1.0).dropna()

    n_ok += 1
    rows.append(
        {
            "sym": sym,
            "n_days": len(vraw),
            "ex_like": len(ex_like),
            "official_ev": len(ev),
            "in_window_ev": int(sum(1 for t in ev if vraw.index[0] <= t <= vraw.index[-1])),
            "hit_on_ev": len(inter),
            "positive_jump": int((ex_like > 0).sum()),
            "f_new_steps": int((np.abs(np.diff(f_new.dropna().to_numpy()) / f_new.dropna().to_numpy()[:-1]) > 1e-7).sum()),
            "f_true_steps": int((np.abs(np.diff(ft.to_numpy()) / ft.to_numpy()[:-1]) > 1e-7).sum()),
            "agree_max_rel": float(rel.abs().max()),
            "agree_med_rel": float(rel.abs().median()),
        }
    )
    for dt, val in list(ex_like.items())[-3:]:
        jump_rows.append((sym, str(dt.date()), float(val), float(fb.get(dt, np.nan))))

df = pd.DataFrame(rows)
print(f"样本 {len(df)} 个标的\n")
print("== 前提 1：$change 是含权涨跌幅吗 ==")
print(f"  「|change - 裸价涨跌幅| > 1e-4」的日子总数 {int(df['ex_like'].sum())}"
      f"，官方事件日总数 {int(df['in_window_ev'].sum())}")
print(f"  其中落在官方事件日上的 {int(df['hit_on_ev'].sum())}"
      f"（{df['hit_on_ev'].sum() / max(df['ex_like'].sum(), 1):.1%}）")
print(f"  跳变符号为正的日子 {int(df['positive_jump'].sum())}/{int(df['ex_like'].sum())}"
      f"（{df['positive_jump'].sum() / max(df['ex_like'].sum(), 1):.1%}）")
print(f"  每标的异常日占交易日比例 中位 {(df['ex_like'] / df['n_days']).median():.4%}")

print("\n== 前提 2：f_new（cumprod 口径） vs f_true（官方分红表重建） ==")
print(f"  阶跃次数：f_new 中位 {df['f_new_steps'].median():.0f} / f_true 中位 {df['f_true_steps'].median():.0f}")
print(f"  归一化后最大相对偏差 中位 {df['agree_med_rel'].median():.4%} "
      f"p95 {df['agree_max_rel'].quantile(.95):.4%} 最大 {df['agree_max_rel'].max():.4%}")
print("\n对照明细（前 12）:")
print(df[["sym", "n_days", "ex_like", "in_window_ev", "hit_on_ev", "positive_jump",
          "f_new_steps", "f_true_steps", "agree_max_rel"]].head(12).to_string(index=False))

print("\n跳变样本（symbol 日期 change-裸涨幅 旧factor）:")
for r in jump_rows[:20]:
    print(f"  {r[0]:10s} {r[1]}  {r[2]:+.5f}  f_old={r[3]:.4f}")
