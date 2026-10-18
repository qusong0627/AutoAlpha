#!/usr/bin/env python3
"""复权因子重建（rebuild_qlib_multiplicative_factor.py）落盘后的验收脚本。

修复前后各跑一次，输出可直接对照。

三段检查：
1. 案例回归：qlib 整仓卖出的股数 = 买入股数 × $factor(卖日)/$factor(买日)，
   用用户反馈的真实成交复现该偏离（修复前应逐位命中，修复后应为 0）。
2. 全市场漂移：持有 N 个交易日后卖出，股数偏离 |f(t+N)/f(t)-1| 的分布
   （修复前逐日累积漂移，修复后仅真送转/配股日非零）。
   按「已重建 / 未重建」分桶统计：未重建 = 工具拒写或跳过的标的，仍是旧加法因子。
3. 不变量体检：只换复权口径、不动真实价格 -- 裸价 close/factor 必须与修复前
   的 .day.bin.bak 一致到 float32 精度；volume/amount 未被改写；字段布局对齐；
   high/low 区间有序；$factor 单调不降；change.day.bin 与 pct_change(close) 自洽。

用法：
    python backend/scripts/audit/verify_factor_rebuild_applied.py \
        --qlib-dir /app/db/qlib_data --sample 400
"""

from __future__ import annotations

import argparse
import random
from pathlib import Path

import numpy as np
import pandas as pd

# 用户反馈的真实成交（代码, 买入日, 卖出日, 买入股数）
CASES = [
    ("sz300536", "2025-01-02", "2025-01-07", 3300),
    ("sh603359", "2026-05-12", "2026-06-10", 10400),
    ("sh603612", "2026-05-06", "2026-06-10", 1400),
]
HOLDS = [5, 20, 60]
FIELDS = ("open", "high", "low", "close", "factor", "change")
STEP_EPS = 1e-7


def read_bin(path: Path) -> tuple[int, np.ndarray] | None:
    if not path.exists():
        return None
    arr = np.fromfile(path, dtype="<f4")
    if arr.size < 2:
        return None
    return int(arr[0]), arr[1:].astype(np.float64)


def load(qlib_dir: Path) -> tuple[list[str], pd.DatetimeIndex]:
    cal = [
        ln.strip()
        for ln in (qlib_dir / "calendars" / "day.txt").read_text().splitlines()
        if ln.strip()
    ]
    return cal, pd.DatetimeIndex(pd.to_datetime(cal))


def field_at(
    qlib_dir: Path, sym: str, field: str, cal_ts: pd.DatetimeIndex, n_cal: int
):
    """返回 (该标的覆盖的日期轴, 字段值)。"""
    got = read_bin(qlib_dir / "features" / sym / f"{field}.day.bin")
    if got is None:
        return None, None
    start, vals = got
    n = min(vals.size, n_cal - start)
    if n <= 0:
        return None, None
    return cal_ts[start : start + n], vals[:n]


def check_cases(qlib_dir: Path) -> None:
    cal, cal_ts = load(qlib_dir)
    print("===== 1. 案例回归（整仓卖出股数 = 买入 x f(卖)/f(买)）=====")
    for sym, d0, d1, qty in CASES:
        _, f = field_at(qlib_dir, sym, "factor", cal_ts, len(cal))
        if f is None:
            print(f"  {sym}: 无 factor.day.bin")
            continue
        idx, _ = field_at(qlib_dir, sym, "close", cal_ts, len(cal))
        p0 = int(idx.searchsorted(pd.Timestamp(d0)))
        p1 = int(idx.searchsorted(pd.Timestamp(d1)))
        if p1 >= len(f):
            print(f"  {sym}: 日期越界")
            continue
        r = f[p1] / f[p0]
        print(
            f"  {sym} {d0}->{d1}  f={f[p0]:.6f}->{f[p1]:.6f}  ratio={r:.6f}  "
            f"买 {qty} -> 卖 {qty * r:.0f}（偏离 {qty * r - qty:+.0f} 股）"
        )


def check_drift(qlib_dir: Path, sample: int, seed: int) -> list[str]:
    cal, cal_ts = load(qlib_dir)
    syms = sorted(p.name for p in (qlib_dir / "features").iterdir() if p.is_dir())
    rng = random.Random(seed)
    rng.shuffle(syms)
    buckets = ("rebuilt", "stale")
    drift = {b: {h: [] for h in HOLDS} for b in buckets}
    steps: dict[str, list[int]] = {b: [] for b in buckets}
    days_tot = dict.fromkeys(buckets, 0)
    n = dict.fromkeys(buckets, 0)
    for sym in syms[:sample]:
        idx, f = field_at(qlib_dir, sym, "factor", cal_ts, len(cal))
        if f is None:
            continue
        # 已重建 = 写了 .bak 备份；未重建 = 工具拒写/跳过的标的，仍是旧加法因子
        key = (
            "rebuilt"
            if (qlib_dir / "features" / sym / "factor.day.bin.bak").exists()
            else "stale"
        )
        f = f[np.isfinite(f) & (f > 0)]
        if f.size < 120:
            continue
        n[key] += 1
        days_tot[key] += f.size
        steps[key].append(int((np.abs(np.diff(f) / f[:-1]) > STEP_EPS).sum()))
        for h in HOLDS:
            r = np.abs(f[h:] / f[:-h] - 1.0)
            drift[key][h] += [float(r.max()), float(np.median(r))]
    print(f"\n===== 2. 全市场因子漂移（{sum(n.values())} 个标的抽样）=====")
    print("  含义：持有 N 个交易日后整仓卖出，卖出股数相对买入股数的偏离")
    for b in buckets:
        if not n[b]:
            continue
        print(
            f"  [{'已重建' if b == 'rebuilt' else '未重建（旧加法因子）'}] "
            f"{n[b]} 个标的"
        )
        for h in HOLDS:
            v = np.array(drift[b][h], dtype=float).reshape(-1, 2)
            worst, med = pd.Series(v[:, 0]), pd.Series(v[:, 1])
            print(
                f"    {h:>3d} 日  中位偏离 {med.median() * 100:6.3f}%  "
                f"p95 偏离 {med.quantile(0.95) * 100:6.3f}%  "
                f"最坏 {worst.max() * 100:6.3f}%  "
                f"偏离>0.1% 标的占比 {(med > 0.001).mean():.1%}"
            )
        sn = pd.Series(steps[b], dtype=float)
        print(
            f"      $factor 变化天数：中位 {sn.median():.0f} / 平均可用日 "
            f"{days_tot[b] / max(n[b], 1):.0f}"
        )
    return syms[:sample]


def check_invariants(qlib_dir: Path, picked: list[str]) -> None:
    cal, cal_ts = load(qlib_dir)
    bad_layout: list[str] = []
    bad_volume: list[str] = []
    bad_raw: list[float] = []
    bad_change: list[float] = []
    n_bak = n_change = n_rebuilt = 0
    # 已重建（有 .bak）与未重建（工具拒写/跳过，仍是旧加法因子）必须分开统计，
    # 否则残留旧数据会把「因子单调」这项打成假阳性。
    mono = {"rebuilt": 0, "stale": 0}
    order = {"rebuilt": 0, "stale": 0}
    stale: list[str] = []
    for sym in picked:
        src = qlib_dir / "features" / sym
        bins = {f: read_bin(src / f"{f}.day.bin") for f in FIELDS}
        if bins["close"] is None or bins["factor"] is None:
            continue
        start, close = bins["close"]
        _, factor = bins["factor"]
        key = "rebuilt" if (src / "factor.day.bin.bak").exists() else "stale"
        if key == "stale":
            stale.append(sym)
        else:
            n_rebuilt += 1
        for f, got in bins.items():
            if got is not None and (got[0] != start or got[1].size != close.size):
                bad_layout.append(f"{sym}:{f}")
        if bins["change"] is not None:
            n_change += 1
            c = bins["change"][1]
            expect = np.full(close.size, np.nan)
            expect[1:] = close[1:] / close[:-1] - 1.0
            m = np.isfinite(expect) & np.isfinite(c)
            if m.any():
                bad_change.append(float(np.abs(c[m] - expect[m]).max()))
        with np.errstate(invalid="ignore"):
            rel = np.diff(factor) / factor[:-1]
            mono[key] += int((rel < -STEP_EPS).sum())
        o, h, lo = bins["open"], bins["high"], bins["low"]
        if o and h and lo:
            with np.errstate(invalid="ignore"):
                order[key] += int(
                    (
                        (lo[1] > np.minimum(o[1], close))
                        | (h[1] < np.maximum(o[1], close))
                    ).sum()
                )
        vb, v = read_bin(src / "volume.day.bin.bak"), read_bin(src / "volume.day.bin")
        if vb is not None:
            bad_volume.append(f"{sym}:volume 被改写")
        elif v is not None and (v[0] != start or v[1].size != close.size):
            bad_volume.append(f"{sym}:volume 布局漂移")
        cb, fb = (
            read_bin(src / "close.day.bin.bak"),
            read_bin(src / "factor.day.bin.bak"),
        )
        if cb and fb and cb[0] == start:
            n_bak += 1
            raw_new = np.where(
                np.isfinite(close) & (factor > 0), close / factor, np.nan
            )
            raw_old = np.where(np.isfinite(cb[1]) & (fb[1] > 0), cb[1] / fb[1], np.nan)
            m = np.isfinite(raw_new) & np.isfinite(raw_old) & (raw_old > 0)
            if m.any():
                bad_raw.append(float(np.abs(raw_new[m] / raw_old[m] - 1.0).max()))
    print(
        f"\n===== 3. 不变量体检（{len(picked)} 个标的，已重建 {n_rebuilt} 个，"
        f"未重建 {len(stale)} 个）====="
    )
    print(f"  含 change.day.bin 的标的        : {n_change}")
    print(f"  字段布局不一致（应为 0）        : {len(bad_layout)}  {bad_layout[:5]}")
    print(f"  volume 被改写/漂移（应为 0）    : {len(bad_volume)}  {bad_volume[:5]}")
    print(
        f"  high/low 区间违例天数（应为 0） : 已重建 {order['rebuilt']}  "
        f"未重建 {order['stale']}"
    )
    print(
        f"  $factor 回落天数（应为 0）      : 已重建 {mono['rebuilt']}  "
        f"未重建 {mono['stale']}（旧加法因子，本就该到处回落）"
    )
    if stale:
        print(f"  未重建标的（工具拒写/跳过）      : {len(stale)}  {stale[:5]}")
    if bad_raw:
        vr = pd.Series(bad_raw, dtype=float)
        print(
            f"  裸价 max|new/old-1|（应为 0）  : 中位 {vr.median():.2e}  "
            f"p95 {vr.quantile(0.95):.2e}  最大 {vr.max():.2e}"
        )
    if bad_change:
        vc = pd.Series(bad_change, dtype=float)
        print(
            f"  change vs pct_change(close)  : 中位 {vc.median():.2e}  最大 {vc.max():.2e}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="验收 qlib 复权因子重建结果")
    parser.add_argument("--qlib-dir", default="/app/db/qlib_data")
    parser.add_argument("--sample", type=int, default=400)
    parser.add_argument("--seed", type=int, default=20260831)
    args = parser.parse_args()
    qlib_dir = Path(args.qlib_dir)
    if not (qlib_dir / "calendars" / "day.txt").exists():
        print(f"[ERR] no qlib calendar under {qlib_dir}")
        return 2
    print(f"qlib dir : {qlib_dir}")
    check_cases(qlib_dir)
    picked = check_drift(qlib_dir, args.sample, args.seed)
    check_invariants(qlib_dir, picked)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
