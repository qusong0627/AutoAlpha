#!/usr/bin/env python3
"""判决测试：$factor 是否由「加法型后复权价 / 不复权价」反推而来。

模型假设：若厂商后复权价 hfq_t = m * raw_t + A（A = 上市以来累计现金分红，
随除息日跳增；m = 送转累乘因子），则写入 bin 的
    factor_t = hfq_t / raw_t = m + A / raw_t
必然逐日随股价波动；股价上涨 → factor 下降，股价下跌 → factor 上升。

反推检验（只用 bin 自身）：
    raw_t = close_t / factor_t
    相邻两日 (f1 - f2) = A * (1/raw1 - 1/raw2)  =>  A_t = (f1-f2)/(1/raw1-1/raw2)
若假设成立，非除权日区段内 A_t / m_t 应近似恒定（低离散），且在除息日跳增。

同时检验替代假设「factor 只是浮点噪声」：A_t 若稳定在 >>1e-6 的量级即排除。
"""
import numpy as np

QLIB = "/app/db/qlib_data"
cal = [ln.strip() for ln in open(f"{QLIB}/calendars/day.txt") if ln.strip()]
SYMS = ["sz002818", "sh603699", "sz300536", "sh603359", "sh600035", "sh603866"]


def load(sym, field):
    arr = np.fromfile(f"{QLIB}/features/{sym}/{field}.day.bin", dtype="<f4")
    return int(arr[0]), arr[1:]


for sym in SYMS:
    st, f = load(sym, "factor")
    stc, c = load(sym, "close")
    n = min(len(f), len(c), len(cal) - max(st, stc))
    dates = cal[stc : stc + n]
    cv = c[:n].astype(np.float64)
    # factor 数组起点可能与 close 不同，按索引对齐到 close 的日期
    off = stc - st
    fv = f[off : off + n].astype(np.float64)
    ok = np.isfinite(cv) & np.isfinite(fv) & (cv > 0) & (fv > 0)
    raw = np.where(ok, cv / fv, np.nan)  # 隐含的不复权价

    r1, r0 = raw[1:], raw[:-1]
    f1, f0 = fv[1:], fv[:-1]
    good = np.isfinite(r1) & np.isfinite(r0) & (r1 > 0) & (r0 > 0) & np.isfinite(f1) & np.isfinite(f0)
    d_raw = np.where(good, r1 / r0 - 1.0, np.nan)
    d_fac = np.where(good, f1 / f0 - 1.0, np.nan)
    inv0 = np.where(good, 1.0 / r0, np.nan)
    inv1 = np.where(good, 1.0 / r1, np.nan)
    denom = inv0 - inv1
    A = np.where(good & (np.abs(denom) > 1e-12), (f0 - f1) / denom, np.nan)
    m = np.where(good, f0 - A * inv0, np.nan)

    ev = np.abs(d_fac) > 5e-3  # 疑似除权除息日（因子大跳变），pair 索引，长度 n-1
    ev_prev = np.concatenate(([False], ev[:-1]))  # 上一个 pair 也是大跳变
    non = good & ~ev & ~ev_prev
    sel = np.where(non)[0]
    # 取靠后 400 个非事件日，看 A/m 是否稳定
    tail = sel[-400:] if len(sel) > 400 else sel
    At, mt = A[tail], m[tail]
    finA = At[np.isfinite(At) & (np.abs(At) > 0)]
    finm = mt[np.isfinite(mt)]
    print(f"\n=== {sym}  {dates[0]}~{dates[-1]}  n={n}")
    print(f"    隐含不复权价 raw: {np.nanmin(raw):.3f}~{np.nanmax(raw):.3f} "
          f"(最新 {raw[ok][-1]:.3f})")
    print(f"    非事件日相邻对 {len(tail)}；反推累计分红 A: 中位 {np.median(finA):.4f} "
          f"IQR {np.percentile(finA,25):.4f}~{np.percentile(finA,75):.4f} "
          f"CV {np.std(finA)/abs(np.mean(finA)):.3f}")
    print(f"    反推累乘因子 m: 中位 {np.median(finm):.6f} "
          f"IQR {np.percentile(finm,25):.6f}~{np.percentile(finm,75):.6f}")
    # Δf/f 与 -Δraw/raw 的相关性（加法型假设的充分特征）
    both = non & (np.abs(d_raw) > 1e-6) & (np.abs(d_fac) > 1e-8)
    x = d_raw[both]
    y = d_fac[both]
    if len(x) > 50:
        corr = np.corrcoef(x, -y)[0, 1]
        # 线性拟合 y = k * (-x)，理论 k = (m)/f - 1 = A/raw/f
        k = np.sum(y * (-x)) / np.sum((-x) ** 2)
        print(f"    corr(Δf/f, -Δraw/raw) = {corr:+.4f}  斜率 k = {k:.4f}"
              f"  (=A/raw 占比，即 factor 中加法部分权重)")
    print(f"    疑似除权日(|Δf|>0.5%) {int(ev.sum())} 天 / 非事件漂移日 "
          f"{int((non & (np.abs(d_fac) > 1e-5)).sum())} 天")
    # 展示一段：非事件日里 factor 连续下滑而 raw 上涨
    i = len(dates) - 40
    print("    末段样本 (date, close_bin, factor, implied_raw):")
    for j in range(i, min(i + 10, n)):
        print(f"      {dates[j]}  {cv[j]:9.3f}  {fv[j]:.6f}  {raw[j]:9.3f}")
