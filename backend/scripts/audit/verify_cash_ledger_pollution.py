#!/usr/bin/env python3
"""决定性验证：虚增股数带来的现金是否真的进入了 qlib 现金/权益账本。

trade 记录同时含双口径字段：
  quantity      = adj_quantity × factor   （还原出的"原始股数"）
  adj_quantity                            （qlib 内部浮点复权份额）
  price         不复权价 / adj_price 后复权价
  cash_after / equity_after               账本余额

若 sells 的 cash_after 增量 == quantity*price - commission，则说明账本确实按
"超卖的 quantity"结算了现金 —— 失真进入权益曲线，不是仅展示层问题。
"""
import json

import numpy as np

P = "/data/backtest_results/default/admin/395ab422d6024433a347e332cf47addd.json"
d = json.load(open(P))
trades = d["trades"]
eq = d["equity_curve"]

print(f"权益曲线 {len(eq)} 点: {eq[0]['date']} {eq[0]['value']:,.0f} -> "
      f"{eq[-1]['date']} {eq[-1]['value']:,.0f}")
print(f"区间收益 {(eq[-1]['value']/eq[0]['value']-1):+.2%}")

# ── 1. 双口径自洽性检查（quantity == adj_quantity*factor） ──
bad = 0
for t in trades:
    q, aq, f = float(t["quantity"]), float(t["adj_quantity"]), float(t["factor"])
    if abs(q - aq * f) > max(1e-4, q * 1e-5):
        bad += 1
print(f"\nquantity != adj_quantity*factor 的记录: {bad}/{len(trades)}")

# ── 2. 买入恒为整手、卖出零头 ──
buys = [t for t in trades if t["action"] == "buy"]
sells = [t for t in trades if t["action"] == "sell"]
print(f"买入 {len(buys)} 笔，全部整手: "
      f"{all(not (round(float(t['quantity'])) % 100) for t in buys)}")
print(f"卖出 {len(sells)} 笔，零头 "
      f"{sum(1 for t in sells if round(float(t['quantity'])) % 100)} 笔")
print(f"卖出 adj_quantity 为整数的笔数: "
      f"{sum(1 for t in sells if abs(float(t['adj_quantity']) - round(float(t['adj_quantity']))) < 1e-6)}")

# ── 3. 现金账本增量核对：cash_after 是否按超卖 quantity 结算 ──
prev_cash = None
prev_equity = None
n_checked = n_cash_ok = n_cash_bad = 0
worst = []
for t in trades:
    q = float(t["quantity"])
    price = float(t["price"])
    com = float(t.get("commission") or 0)
    cash_after = float(t["cash_after"])
    if prev_cash is not None:
        side = 1 if t["action"] == "sell" else -1
        expect = prev_cash + side * (q * price) - com
        diff = cash_after - expect
        n_checked += 1
        if abs(diff) <= max(1.0, expect * 1e-6):
            n_cash_ok += 1
        else:
            n_cash_bad += 1
            worst.append((abs(diff), t["date"], t["symbol"], t["action"], q, cash_after, expect))
    prev_cash = cash_after
print(f"\ncash_after 递推核对: 一致 {n_cash_ok} / 不一致 {n_cash_bad}（共 {n_checked}）")
worst.sort(reverse=True)
for w in worst[:5]:
    print(f"  最大偏差 {w[0]:,.2f} 元 @ {w[1]} {w[2]} {w[3]} qty={w[4]:.0f}")

# ── 4. 复权份额台账：整仓卖出时 adj_quantity 是否被"清零" ──
by_sym = {}
for t in trades:
    by_sym.setdefault(t["symbol"], []).append(t)
zero_check = zero_ok = 0
for sym, items in by_sym.items():
    adj = 0.0
    for t in sorted(items, key=lambda x: x["date"]):
        aq = float(t["adj_quantity"])
        if t["action"] == "buy":
            adj += aq
        else:
            is_full = abs(adj - aq) <= max(1e-3, adj * 1e-5)
            zero_check += 1
            if is_full:
                zero_ok += 1
                adj = 0.0
            else:
                adj = max(0.0, adj - aq)
print(f"\n整仓卖出（adj_quantity 恰好等于累计复权份额）识别: {zero_ok}/{zero_check} 笔卖出")
print("  → 说明 qlib 侧持仓以浮点复权份额记账，卖出时按份额全额出清")

# ── 5. 超卖造成的虚增现金 / 期末权益 ──
from collections import defaultdict
cum = defaultdict(lambda: [0.0, 0.0])
phantom = 0.0
n_over = 0
factors = {}


def fac_get(sym, date):
    return None


for t in trades:
    sym = t["symbol"]
    q = float(t["quantity"])
    if t["action"] == "buy":
        cum[sym][0] += q
    else:
        open_raw = cum[sym][0] - cum[sym][1]
        if q > open_raw + 0.5 and open_raw > 0:
            n_over += 1
            phantom += (q - open_raw) * float(t["price"])
        cum[sym][1] += q
final = eq[-1]["value"]
print(f"\n超卖 {n_over} 笔；虚增现金（不复权价口径） {phantom:,.0f} 元")
print(f"期末权益 {final:,.0f} 元 → 虚增占期末权益 {phantom/final:.2%}")
print(f"若剔除虚增现金，期末权益约 {final-phantom:,.0f} 元，"
      f"区间收益由 {(final/eq[0]['value']-1):+.2%} 降为 {((final-phantom)/eq[0]['value']-1):+.2%}")
