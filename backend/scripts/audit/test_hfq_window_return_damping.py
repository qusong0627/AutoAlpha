"""判决性测试（收紧口径）：在「窗口内无任何分红送配」的股票上，close_bin 的区间收益
必须严格等于不复权价区间收益（因为没有现金流）。差额 = 纯口径污染对净值的影响。

与上一版的差别：
1. 日期取 bin 与官方 parquet 的**交集**，两端对齐，避免停牌/缺失造成的端点漂移；
2. 逐点检验恒等式 close_bin == raw × factor，把「恒等式破裂」与「收益阻尼」分开计数；
3. 对污染最重的标的打印诊断（日期跨度、f 范围、逐点最大偏差）。
"""
import glob
import os
import random

import numpy as np
import pandas as pd

QLIB = "/app/db/qlib_data"
QDB = "/data/quantdb"
START = pd.Timestamp("2025-01-01")

cal = [ln.strip() for ln in open(f"{QLIB}/calendars/day.txt") if ln.strip()]
cald = pd.to_datetime(pd.Series(cal))


def load_bin(sym, field):
    p = f"{QLIB}/features/{sym}/{field}.day.bin"
    if not os.path.exists(p):
        return None
    arr = np.fromfile(p, dtype="<f4")
    st = int(arr[0])
    return pd.Series(arr[1:].astype(np.float64), index=cald[st : st + len(arr) - 1])


syms = [os.path.basename(d) for d in glob.glob(f"{QLIB}/features/*")]
random.seed(7)
random.shuffle(syms)

rows = []
checked = 0
for sym in syms:
    if len(rows) >= 150 or checked > 2000:
        break
    checked += 1
    code = f"{sym[2:]}.{sym[:2].upper()}"
    up = f"{QDB}/1_kline_data/daily_unadjusted/{code}.parquet"
    dp = f"{QDB}/3_financial_data/dividend_factors/{code}.parquet"
    if not (os.path.exists(up) and os.path.exists(dp)):
        continue
    cb = load_bin(sym, "close")
    fb = load_bin(sym, "factor")
    if cb is None or fb is None:
        continue
    cb = cb[cb.index >= START].dropna()
    if len(cb) < 150:
        continue
    d = pd.read_parquet(dp)
    d["time"] = pd.to_datetime(d["time"])
    if ((d["time"] >= cb.index[0]) & (d["time"] <= cb.index[-1])).any():
        continue  # 窗口内有分红送配 => 收益本就应不同
    u = pd.read_parquet(up)
    tcol = next(c for c in u.columns if c.lower() in ("time", "trade_date", "datetime", "date"))
    ccol = next(c for c in u.columns if c.lower() in ("close",))
    u[tcol] = pd.to_datetime(u[tcol])
    raw = u.set_index(tcol)[ccol].astype(float).sort_index()
    raw = raw[(raw.index >= cb.index[0]) & (raw.index <= cb.index[-1])]
    raw = raw[raw > 0]
    idx = cb.index.intersection(raw.index)
    if len(idx) < 150:
        continue
    cbx, rawx = cb.reindex(idx), raw.reindex(idx)
    fbz = fb.reindex(idx)
    fbz = fbz.ffill()
    identity_err = float((cbx / (rawx * fbz) - 1.0).abs().max())   # 恒等式检验
    f_span = float(fbz.max() - fbz.min())
    bin_ret = float(cbx.iloc[-1] / cbx.iloc[0] - 1)
    raw_ret = float(rawx.iloc[-1] / rawx.iloc[0] - 1)
    rows.append((sym, len(idx), str(idx[0].date()), str(idx[-1].date()), raw_ret, bin_ret,
                 (1 + bin_ret) / (1 + raw_ret), float(fbz.iloc[0]), f_span, identity_err))

df = pd.DataFrame(rows, columns=["sym", "n", "d0", "d1", "raw_ret", "bin_ret", "ratio",
                                "f_b", "f_span", "id_err"])
print(f"检查 {checked} 个标的，纳入「窗口内零分红送配」样本 {len(df)} 个"
      f"（窗口 {START.date()} ~ {cald.iloc[-1].date()}，日期取交集）\n")

print("== 恒等式 close_bin == raw × factor 的逐点最大相对偏差 ==")
print(df["id_err"].describe(percentiles=[.5, .9, .99]).to_string())
bad_id = df[df["id_err"] > 0.01]
print(f"恒等式破裂(>1%)的标的: {len(bad_id)}/{len(df)}")
print(f"因子在窗口内有变化(f_span>1e-4)的标的: {int((df['f_span'] > 1e-4).sum())}/{len(df)}")

print("\n== 零现金流窗口的收益对比 ==")
print(df[["raw_ret", "bin_ret", "ratio"]].describe(percentiles=[.05, .25, .5, .75, .95]).to_string())
good = df[(df["id_err"] <= 0.01) & (df["f_span"] <= 1e-4)]
print(f"\n仅看恒等式成立且因子无变化的标的 n={len(good)}（这些应当逐日完全相同）:")
print(f"  |bin_ret - raw_ret| 最大 {float((good['bin_ret'] - good['raw_ret']).abs().max()):.2e}"
      if len(good) else "  无样本")
sub = df[(df["raw_ret"] * df["bin_ret"] > 0) & (df["raw_ret"].abs() > 0.05)]
amp = (sub["bin_ret"].abs() / sub["raw_ret"].abs())
print(f"\n方向一致且|raw_ret|>5% 的样本 {len(sub)} 个：|bin|/|raw| 中位 {amp.median():.3f} "
      f"p05 {amp.quantile(.05):.3f} p95 {amp.quantile(.95):.3f}")
print(f"收益偏差 (bin_ret - raw_ret) 分位: "
      f"{np.percentile(df['bin_ret'] - df['raw_ret'], [5, 25, 50, 75, 95]).round(4)}")

print("\n污染最重 Top12 诊断:")
tmp = df.assign(gap=lambda x: (x["bin_ret"] - x["raw_ret"]).abs()).nlargest(12, "gap")
for _, r in tmp.iterrows():
    print(f"  {r['sym']:10s} {r['d0']}~{r['d1']} raw {r['raw_ret']:+8.2%} bin {r['bin_ret']:+8.2%}"
          f"  f_b {r['f_b']:.4f} f_span {r['f_span']:.4f} 恒等式偏差 {r['id_err']:.2e}")
print("\n最轻 Top6:")
for _, r in tmp.nsmallest(6, "gap").iterrows():
    print(f"  {r['sym']:10s} {r['d0']}~{r['d1']} raw {r['raw_ret']:+8.2%} bin {r['bin_ret']:+8.2%}"
          f"  f_b {r['f_b']:.4f} f_span {r['f_span']:.4f} 恒等式偏差 {r['id_err']:.2e}")
