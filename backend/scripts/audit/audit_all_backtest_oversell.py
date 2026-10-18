#!/usr/bin/env python3
"""全量回测结果审计：验证「卖出数量 > 买入数量」的系统性与资金影响。

对 /data/backtest_results 下所有含 trades 的 JSON 逐笔做复权份额台账审计：
  期望卖出量 = Σ(lot_i / f_buy_i) × f_sell   （qlib 内部浮点复权份额口径）
统计：零头卖出率、超卖笔数、虚增现金、以及虚增现金占期末权益比例。
"""

import glob
import json
import os
import sys
from collections import defaultdict

import numpy as np

QLIB = "/app/db/qlib_data"

calendar = [ln.strip() for ln in open(f"{QLIB}/calendars/day.txt") if ln.strip()]
date_idx = {d: i for i, d in enumerate(calendar)}
_cache: dict[str, tuple[int, np.ndarray]] = {}


def get_field(sym: str, field: str, date: str) -> float:
    key = f"{sym}/{field}"
    st = _cache.get(key)
    if st is None:
        p = f"{QLIB}/features/{sym}/{field}.day.bin"
        if not os.path.exists(p):
            _cache[key] = (0, np.array([], dtype="<f4"))
            return float("nan")
        arr = np.fromfile(p, dtype="<f4")
        st = (int(arr[0]), arr[1:])
        _cache[key] = st
    start, vals = st
    i = date_idx.get(date)
    if i is None:
        return float("nan")
    j = i - start
    return float(vals[j]) if 0 <= j < len(vals) else float("nan")


def get_factor(sym: str, date: str) -> float:
    v = get_field(sym, "factor", date)
    if np.isfinite(v) and v > 0:
        return v
    i = date_idx.get(date)
    if i is None:
        return float("nan")
    for back in range(1, 15):
        if i - back < 0:
            break
        v = get_field(sym, "factor", calendar[i - back])
        if np.isfinite(v) and v > 0:
            return v
    return float("nan")


def audit(path: str) -> dict | None:
    try:
        payload = json.load(open(path))
    except Exception:
        return None
    trades = payload.get("trades") or []
    if not trades:
        return None
    buys = [t for t in trades if str(t.get("action", "")).lower() == "buy"]
    sells = [t for t in trades if str(t.get("action", "")).lower() == "sell"]
    if not buys or not sells:
        return None

    by_sym: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        by_sym[str(t.get("symbol", "")).lower()].append(t)

    n_odd_sell = sum(1 for t in sells if int(round(float(t["quantity"]))) % 100)
    n_odd_buy = sum(1 for t in buys if int(round(float(t["quantity"]))) % 100)
    oversell = match_ok = 0
    phantom = 0.0
    for sym, items in by_sym.items():
        items.sort(key=lambda t: (str(t["date"]), 0 if str(t["action"]).lower() == "sell" else 1))
        cum_buy = cum_sell = open_adj = 0.0
        for t in items:
            try:
                qty = float(t["quantity"])
            except (KeyError, TypeError, ValueError):
                continue
            d = str(t["date"])[:10]
            f = get_factor(sym, d)
            if str(t["action"]).lower() == "buy":
                cum_buy += qty
                if np.isfinite(f) and f > 0:
                    open_adj += qty / f
            else:
                open_raw = cum_buy - cum_sell
                if qty > open_raw + 0.5 and open_raw > 0:
                    oversell += 1
                    phantom += (qty - open_raw) * float(t.get("price") or 0)
                    exp = open_adj * f if np.isfinite(f) and f > 0 else open_raw
                    if exp > 0 and abs(qty / open_raw - exp / open_raw) / (exp / open_raw) < 1e-3:
                        match_ok += 1
                if np.isfinite(f) and f > 0:
                    open_adj = max(0.0, open_adj - qty / f)
                cum_sell += qty

    final_val = None
    for k in ("final_equity", "end_value", "total_value", "nav"):
        v = payload.get(k)
        if isinstance(v, (int, float)):
            final_val = float(v)
            break
    return {
        "path": path,
        "n_trades": len(trades),
        "n_buy": len(buys),
        "n_sell": len(sells),
        "odd_buy": n_odd_buy,
        "odd_sell": n_odd_sell,
        "oversell": oversell,
        "match_ok": match_ok,
        "phantom": phantom,
        "final_val": final_val,
        "meta": payload.get("strategy_name") or payload.get("strategy_id") or "",
    }


def main() -> int:
    paths = glob.glob("/data/backtest_results/**/*.json", recursive=True)
    print(f"扫描 {len(paths)} 个回测结果文件 ...")
    rows = [r for r in (audit(p) for p in paths) if r]
    print(f"含成交流水的结果 {len(rows)} 个\n")

    tot_odd_sell = sum(r["odd_sell"] for r in rows)
    tot_sell = sum(r["n_sell"] for r in rows)
    tot_odd_buy = sum(r["odd_buy"] for r in rows)
    tot_buy = sum(r["n_buy"] for r in rows)
    tot_over = sum(r["oversell"] for r in rows)
    tot_ok = sum(r["match_ok"] for r in rows)
    tot_phantom = sum(r["phantom"] for r in rows)
    print(f"合计：买 {tot_buy}（零头 {tot_odd_buy}） / 卖 {tot_sell}（零头 {tot_odd_sell}）")
    print(f"零头卖出率 {tot_odd_sell/max(tot_sell,1):.1%}；零头买入率 {tot_odd_buy/max(tot_buy,1):.1%}")
    print(f"超卖笔数 {tot_over}，其中因子模型吻合(<0.1%) {tot_ok}（{tot_ok/max(tot_over,1):.1%}）")
    print(f"虚增现金合计（跨全部回测）≈ {tot_phantom:,.0f} 元\n")

    rows.sort(key=lambda r: -r["oversell"])
    print(f"{'trades':>7} {'oddSell%':>9} {'oversell':>9} {'match%':>7} "
          f"{'phantom':>12} {'final':>14}  path")
    for r in rows[:25]:
        pct = r["odd_sell"] / max(r["n_sell"], 1)
        mp = r["match_ok"] / max(r["oversell"], 1)
        fv = f"{r['final_val']:,.0f}" if r["final_val"] else "-"
        print(f"{r['n_trades']:7d} {pct:9.1%} {r['oversell']:9d} {mp:7.1%} "
              f"{r['phantom']:12,.0f} {fv:>14}  {r['path'][22:]}")

    # 单个结果内 phantom / final 的占比（有权益数据的）
    ratios = [r["phantom"] / r["final_val"] for r in rows if r["final_val"]]
    if ratios:
        print(f"\n虚增现金/期末权益：均值 {np.mean(ratios):.3%}  "
              f"中位 {np.median(ratios):.3%}  最大 {np.max(ratios):.3%}  "
              f"(样本 {len(ratios)} 个)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
