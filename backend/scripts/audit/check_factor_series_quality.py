#!/usr/bin/env python3
"""核查 $factor 序列本身是否合理。

判据：后复权因子在无公司行为的交易日必须保持不变（阶梯状）。
若因子频繁跳变（尤其单日 >1%）、或半年内多次 6%+ 跳变，则 bin 里的 factor
序列本身被污染，超卖就不是"分红再投资的正确记账"，而是数据缺陷。
"""
import numpy as np

QLIB = "/app/db/qlib_data"
cal = [ln.strip() for ln in open(f"{QLIB}/calendars/day.txt") if ln.strip()]

SYMS = ["sz002818", "sh603866", "sh600035", "sh603699", "sz300536", "sh603359"]


def load(sym, field):
    arr = np.fromfile(f"{QLIB}/features/{sym}/{field}.day.bin", dtype="<f4")
    return int(arr[0]), arr[1:]


for sym in SYMS:
    st, f = load(sym, "factor")
    stc, c = load(sym, "close")
    n = len(f)
    dates = cal[st : st + n]
    fin = np.isfinite(f) & (f > 0)
    # 相邻交易日因子变化率：chg[i] = f[i+1]/f[i] - 1，长度 n-1
    chg = f[1:] / f[:-1] - 1.0
    both = fin[1:] & fin[:-1]
    valid = np.where(both & np.isfinite(chg))[0]
    jumps = [(dates[i + 1], float(f[i]), float(f[i + 1]), float(chg[i])) for i in valid if abs(chg[i]) > 1e-5]
    print(f"\n=== {sym}: 区间 {dates[0]}~{dates[-1]} n={n} 有效={int(fin.sum())}")
    print(f"    factor 首值 {f[0]:.4f} 末值 {f[-1]:.4f} 总增幅 {f[-1]/f[0]-1:+.2%}")
    print(f"    因子跳变次数 {len(jumps)}（区间内 {len(dates)} 个交易日）")
    big = [j for j in jumps if abs(j[3]) > 0.005]
    print(f"    其中 |Δ|>0.5% 的跳变 {len(big)} 次")
    for d, a, b, r in jumps[-12:]:
        print(f"      {d}: {a:.6f} -> {b:.6f}  ({r:+.4%})")
    # 复权价 vs 因子：因子跳变日 close(后复权) 是否连续
    for d, a, b, r in big[-4:]:
        i = dates.index(d)
        if i > 0 and np.isfinite(c[i - 1]) and np.isfinite(c[i]):
            print(f"      [价格连续性] {d} adj_close {c[i-1]:.3f} -> {c[i]:.3f} "
                  f"({c[i]/c[i-1]-1:+.2%})  隐含不复权涨跌幅需={1/(b/a)-1:+.2%}")
