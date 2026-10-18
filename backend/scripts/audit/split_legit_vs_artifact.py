#!/usr/bin/env python3
"""最终定量：把回测成交流水里每一笔卖出的"股数偏差"拆成
  (a) 真实公司行为（分红送配除权日）应得的股数变化
  (b) $factor 非事件日漂移造成的纯数据缺陷

真值来源：QuantDB 官方包 daily_unadjusted（不复权价）+ dividend_factors（分红送配事件），
据此构造标准**乘法**后复权因子 f_true（阶梯状、单调不降）。
"""
import json
import os
from collections import defaultdict

import numpy as np
import pandas as pd

QLIB = "/app/db/qlib_data"
QDB = "/data/quantdb"
RESULT = "/data/backtest_results/default/admin/395ab422d6024433a347e332cf47addd.json"

cal = [ln.strip() for ln in open(f"{QLIB}/calendars/day.txt") if ln.strip()]
cald = pd.to_datetime(pd.Series(cal))


def load_bin(sym, field):
    arr = np.fromfile(f"{QLIB}/features/{sym}/{field}.day.bin", dtype="<f4")
    st = int(arr[0])
    return pd.Series(arr[1:].astype(np.float64), index=cald[st : st + len(arr) - 1])


def build_true_factor(sym: str):
    """标准乘法后复权因子：除权日 f *= 前收盘 / 除权参考价。"""
    code = f"{sym[2:]}.{sym[:2].upper()}"
    up = f"{QDB}/1_kline_data/daily_unadjusted/{code}.parquet"
    dp = f"{QDB}/3_financial_data/dividend_factors/{code}.parquet"
    if not (os.path.exists(up) and os.path.exists(dp)):
        return None
    u = pd.read_parquet(up)
    tcol = next((c for c in ("time", "trade_date", "datetime", "date") if c in u.columns), None)
    ccol = next((c for c in ("close", "Close") if c in u.columns), None)
    if tcol is None or ccol is None:
        return None
    u[tcol] = pd.to_datetime(u[tcol])
    raw = u.set_index(tcol)[ccol].astype(float).sort_index()
    raw = raw[raw > 0]
    if raw.empty:
        return None

    d = pd.read_parquet(dp)
    d["time"] = pd.to_datetime(d["time"])
    d = d.sort_values("time")

    f = pd.Series(1.0, index=raw.index)
    cum = 1.0
    for _, r in d.iterrows():
        ex = pd.Timestamp(r["time"]).normalize()
        prev = raw.index[raw.index < ex]
        if len(prev) == 0:
            continue
        p0 = float(raw.loc[prev[-1]])
        bonus = float(r.get("stockBonus") or 0) / 10.0
        allot = float(r.get("allotment") or 0) / 10.0
        cash = float(r.get("interest") or 0) / 10.0
        aprice = float(r.get("allotPrice") or 0) / 10.0
        ref = (p0 - cash + aprice * allot) / (1.0 + bonus + allot)
        if ref <= 0:
            continue
        cum *= p0 / ref
        f.loc[f.index >= ex] = cum
    return f


def factor_at(ft: pd.Series, ts: pd.Timestamp) -> float:
    pre = ft.index[ft.index <= ts]
    return float(ft.loc[pre[-1]]) if len(pre) else float("nan")


trades = json.load(open(RESULT))["trades"]
by_sym = defaultdict(list)
for t in trades:
    by_sym[t["symbol"]].append(t)

# 复权份额台账配对整仓卖出（adj_quantity 精确清零 = qlib 内部口径）
pairs = []
for sym, items in by_sym.items():
    items.sort(key=lambda t: t["date"])
    adj = lot = wadj = 0.0
    last_buy = None
    for t in items:
        f = float(t["factor"])
        aq = float(t["adj_quantity"])
        q = float(t["quantity"])
        if t["action"] == "buy":
            adj += aq
            lot += q
            wadj += q / f
            last_buy = pd.Timestamp(t["date"])
        else:
            if lot > 0 and last_buy is not None:
                pairs.append((sym, last_buy, pd.Timestamp(t["date"]), lot, aq * f,
                              aq * float(t["price"]) * f))
                used = min(aq, adj)
                avg_f = lot / wadj if wadj > 0 else f
                lot = max(0.0, lot - used * avg_f)
                wadj = max(0.0, wadj - used)
            adj = max(0.0, adj - aq)
            lot = max(0.0, lot - q)

print(f"整仓卖出配对 {len(pairs)} 笔")

cache: dict[str, object] = {}
n_use = no_event = with_event = 0
no_event_over = no_event_under = 0
qty_artifact = cash_artifact = cash_artifact_signed = 0.0
cash_legit_dev = 0.0
total_booked = 0.0
worst = []
for sym, b, s, lot, qty, booked in pairs:
    if sym not in cache:
        cache[sym] = build_true_factor(sym)
    ft = cache[sym]
    total_booked += booked
    if ft is None:
        continue
    fb_t, fs_t = factor_at(ft, b), factor_at(ft, s)
    if not (np.isfinite(fb_t) and np.isfinite(fs_t)) or fb_t <= 0:
        continue
    n_use += 1
    true_ratio = fs_t / fb_t
    should_sell = lot * true_ratio          # 真实世界里这笔应当卖出的股数
    bin_ratio = qty / lot if lot > 0 else np.nan
    dev_qty = qty - should_sell
    dev_cash = booked * (1.0 - 1.0 / (bin_ratio / true_ratio)) if true_ratio > 0 else 0.0
    if abs(true_ratio - 1.0) < 1e-9:        # 持有期内无任何分红送配
        no_event += 1
        qty_artifact += abs(dev_qty)
        cash_artifact += abs(dev_cash)
        cash_artifact_signed += dev_cash
        if dev_qty > 1:
            no_event_over += 1
        elif dev_qty < -1:
            no_event_under += 1
    else:
        with_event += 1
        cash_legit_dev += abs(dev_cash)
    worst.append((abs(dev_cash), sym, str(b.date()), str(s.date()), lot, qty,
                  should_sell, true_ratio, bin_ratio))

eq = json.load(open(RESULT))["equity_curve"]
final = eq[-1]["value"]
print(f"有官方真值可比对 {n_use} 笔（覆盖 {len(cache)} 个标的）")
print(f"  持有期内**无任何分红送配**（真值比=1）: {no_event} 笔"
      f"（{no_event/max(n_use,1):.1%}）→ 偏差 100% 是数据缺陷")
print(f"     其中多卖 >1 股 {no_event_over} 笔 / 少卖 >1 股 {no_event_under} 笔")
print(f"  持有期内确有公司行为: {with_event} 笔")
print(f"\n无公司行为卖出的股数偏差 |Σ| {qty_artifact:,.0f} 股")
print(f"无公司行为卖出的现金偏差 |Σ| {cash_artifact:,.0f} 元  "
      f"（有符号 {cash_artifact_signed:+,.0f} 元）")
print(f"有公司行为卖出的残余偏差 |Σ| {cash_legit_dev:,.0f} 元")
print(f"卖出总回款 {total_booked:,.0f} 元；期末权益 {final:,.0f} 元")
print(f"→ 纯缺陷现金偏差 占回款 {cash_artifact/total_booked:.3%}"
      f"，占期末权益（绝对口径）{cash_artifact/final:.1%}")

worst.sort(reverse=True)
print("\n现金偏差 Top12（symbol 买入日→卖出日 应卖/实卖 真值比 bin比 偏差元）:")
for w in worst[:12]:
    print(f"  {w[0]:10,.0f}  {w[1]} {w[2]}->{w[3]} 应卖{w[5]:8.0f} 实卖{w[6]:8.0f} "
          f"真值比{w[7]:.5f} bin比{w[8]:.5f}")
