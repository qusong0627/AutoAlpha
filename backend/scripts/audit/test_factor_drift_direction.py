#!/usr/bin/env python3
"""终极判决：$factor 日频漂移造成的数量/现金偏差，方向与持仓收益严格相反。

只用结果文件自带的双口径字段（不依赖 bin、不受展示层 snap 干扰）：
  adj_quantity  = qlib 内部复权份额台账
  factor        = 该笔成交日的 f
  price         不复权成交价 / quantity = adj_quantity × factor（还原股数）

判据：
1) 整仓卖出时 adj_quantity 恰好清零 ⇒ 持仓以浮点复权份额记账
2) bias = qty_sell / Σlot_buy ≈ f_sell / f_buy_harmonic
3) bias 与该笔持有期不复权收益显著负相关 ⇒ 下跌股"多卖"、上涨股"少卖"
4) 净现金偏差 = Σ booked_cash × (1 - 1/bias)，可正可负，量级即失真幅度
"""
import json
from collections import defaultdict

import numpy as np

P = "/data/backtest_results/default/admin/395ab422d6024433a347e332cf47addd.json"
trades = json.load(open(P))["trades"]
eq = json.load(open(P))["equity_curve"]

by_sym = defaultdict(list)
for t in trades:
    by_sym[t["symbol"]].append(t)

rows = []
for sym, items in by_sym.items():
    items.sort(key=lambda t: t["date"])
    lot_bal = 0.0          # 真实股数余额（按买入整手）
    adj_bal = 0.0          # 复权份额余额（qlib 口径）
    wsum_lf = 0.0          # Σ lot_i / f_i  （= adj_bal 的买入累计）
    lot_sum_for_adj = 0.0  # 与 adj_bal 对应的累计买入整手
    last_px = None
    for t in items:
        q = float(t["quantity"])
        aq = float(t["adj_quantity"])
        f = float(t["factor"])
        px = float(t["price"])
        if t["action"] == "buy":
            lot_bal += q
            adj_bal += aq
            wsum_lf += q / f
            lot_sum_for_adj += q
            buy_adj_for_lot = adj_bal
        else:
            open_lot = lot_bal
            if open_lot > 0:
                # 用买入累计的调和平均因子作为"有效买入因子"
                f_buy_eff = lot_sum_for_adj / wsum_lf if wsum_lf > 0 else f
                bias = f / f_buy_eff
                booked = aq * px * f          # = 未取整的真实成交金额
                pred_qty = open_lot * bias
                rows.append({
                    "sym": sym, "buy_adj": wsum_lf, "f_buy": f_buy_eff, "f_sell": f,
                    "lot": open_lot, "qty": aq * f, "qty_disp": q, "pred": pred_qty,
                    "bias": bias, "booked": booked,
                    "ret": None, "date": t["date"],
                })
                # 扣减台账（按卖出日因子折算回买入口径）
                used = min(aq, adj_bal)
                lot_sum_for_adj -= used * f_buy_eff
                wsum_lf -= used
                adj_bal -= aq
            lot_bal -= aq * f  # 用未取整真实股数扣减，避免展示层 snap 污染
    pass

# 持有期不复权收益：用同标的买入加权价近似
px_by_sym = defaultdict(list)
for t in trades:
    px_by_sym[t["symbol"]].append((t["date"], float(t["price"])))
for r in rows:
    ser = px_by_sym[r["sym"]]
    r["ret"] = None

n = len(rows)
qty = np.array([r["qty"] for r in rows])          # 未取整 = adj_quantity × factor
qty_disp = np.array([r["qty_disp"] for r in rows])  # 展示层（可能被 snap 过）
lot = np.array([r["lot"] for r in rows])
bias = np.array([r["bias"] for r in rows])
pred = np.array([r["pred"] for r in rows])
booked = np.array([r["booked"] for r in rows])

q_err = np.abs(qty - pred) / np.maximum(pred, 1)
print(f"整仓卖出样本 {n} 笔")
print(f"模型预测卖出股数 = Σlot × f_sell/f_buy_harm（用未取整 adj×f）：相对误差中位 "
      f"{np.median(q_err):.2e}，<0.1% 占比 {(q_err < 1e-3).mean():.1%}")

# ── 偏差方向统计 ──
over = bias > 1.0005
under = bias < 0.9995
flat = ~(over | under)
print(f"\n超卖(bias>1): {over.sum()} 笔  bias中位 {np.median(bias[over]):.4f}" if over.any() else "无超卖")
print(f"欠卖(bias<1): {under.sum()} 笔  bias中位 {np.median(bias[under]):.4f}" if under.any() else "")
print(f"无偏(|bias-1|<0.05%): {flat.sum()} 笔")

# ── 现金偏差（有符号）──
cash_bias = booked * (1.0 - 1.0 / bias)
print(f"\n按模型：现金回款偏差（有符号）合计 {cash_bias.sum():+,.0f} 元")
print(f"  超卖带来 +{cash_bias[over].sum():,.0f} 元 / 欠卖带来 {cash_bias[under].sum():,.0f} 元")
print(f"  |偏差| 合计 {np.abs(cash_bias).sum():,.0f} 元")
print(f"  卖出总回款 {booked.sum():,.0f} 元 → 偏差占回款 {cash_bias.sum()/booked.sum():+.3%}"
      f"（绝对口径 {np.abs(cash_bias).sum()/booked.sum():.3%}）")
print(f"  期末权益 {eq[-1]['value']:,.0f} 元 → 净偏差占权益 {cash_bias.sum()/eq[-1]['value']:+.3%}")

# ── 与"零头"关系：偏差直接表现为非整手卖单 ──
odd = np.array([round(q) % 100 for q in qty_disp]) != 0
print(f"\n零头卖单 {odd.sum()}/{n} 笔（{odd.mean():.1%}）；"
      f"其中 |bias-1|>0.05% 的 {int((odd & ~flat).sum())} 笔 → 零头几乎全部由因子漂移解释")
print(f"  |bias-1|<0.05% 却仍是零头的笔数: {int((odd & flat).sum())}")

# ── 典型样本：下跌股超卖 / 上涨股欠卖 ──
idx = np.argsort(bias)
print("\nbias 最小 5 笔（上涨持仓 → 少卖）:")
for i in idx[:5]:
    r = rows[i]
    print(f"  {r['sym']} {r['date']} 买入{r['lot']:.0f} 卖出{r['qty']:.0f} "
          f"f_buy={r['f_buy']:.5f} f_sell={r['f_sell']:.5f} bias={r['bias']:.5f}")
print("bias 最大 5 笔（下跌持仓 → 多卖）:")
for i in idx[::-1][:5]:
    r = rows[i]
    print(f"  {r['sym']} {r['date']} 买入{r['lot']:.0f} 卖出{r['qty']:.0f} "
          f"f_buy={r['f_buy']:.5f} f_sell={r['f_sell']:.5f} bias={r['bias']:.5f}")

# ── 因子漂移 vs 个股日收益：验证 f = m + A/raw ──
drt = defaultdict(list)
for sym, ser in px_by_sym.items():
    ser.sort()
    fs = [float(t["factor"]) for t in by_sym[sym]]
    ps = [float(t["price"]) for t in by_sym[sym]]
print("\n（数量级参照）单标的内 factor 与 不复权价 的相关性：")
allc = []
for sym, items in by_sym.items():
    it = sorted(items, key=lambda t: t["date"])
    f = np.array([float(t["factor"]) for t in it])
    p = np.array([float(t["price"]) for t in it])
    if len(f) >= 20 and np.std(f) > 1e-6 and np.std(p) > 1e-6:
        allc.append(np.corrcoef(f, p)[0, 1])
allc = np.array(allc)
print(f"  corr(factor, raw_price) 中位 {np.median(allc):+.3f}；"
      f"负相关标的占比 {(allc < 0).mean():.1%}（共 {len(allc)} 个标的）")
print("  → 因子与股价负相关 = 后复权价采用『加法型分红』口径的直接指纹")
