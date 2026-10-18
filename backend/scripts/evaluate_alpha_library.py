#!/usr/bin/env python3
"""Alpha 库因子 IC 检验（429 因子 × 5 前瞻期）→ 排序表。

方法（对齐 evaluate_factors.py 既有方法论）:
  - RankIC: 逐日横截面 Spearman 秩相关（rank 对异常值鲁棒，无需去极值）
  - ICIR = mean(IC)/std(IC)，胜率 = P(IC>0)，t 值 = ICIR × sqrt(n)
  - 前瞻期: fwd_ret_1/2/5/10/20（来自 alpha_library_labels 独立标签表）
  - 综合评分: score = |ic_mean| × icir（同时要求高且稳）

内存策略: 因子按块读取（70 因子/块），块内完成 rank+corr 后释放；
标签表一次载入（5 列 × 1430 万行 ≈ 0.3GB）。

用法:
  python backend/scripts/evaluate_alpha_library.py              # 全量
  python backend/scripts/evaluate_alpha_library.py --horizon 5  # 只看 T+5
  python backend/scripts/evaluate_alpha_library.py --top 50     # 只看 Top 50
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.services.engine.data_platform.quantdb_factor_reader import QuantDBFactorReader
from backend.shared.stock_utils import StockCodeUtil

log = logging.getLogger("eval_alpha")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

LABELS_GLOB = (
    f"{PROJECT_ROOT}/data/quantdb/6_ml_datasets/alpha_library_labels/dt=*/data.parquet"
)
OUT_DIR = PROJECT_ROOT / "data" / "quantdb" / "6_ml_datasets" / "alpha_library" / "ic_evaluation"

HORIZONS = ("fwd_ret_1", "fwd_ret_2", "fwd_ret_5", "fwd_ret_10", "fwd_ret_20")

# 与 PLAN.md 一致的 21 个跳过因子（不在数据集中，仅作清单完整性参考）
# 数据集本身只有 429 列，无需在此过滤。


def load_labels() -> pd.DataFrame:
    """读取独立标签表（symbol 转前缀格式与 read_range 对齐）。"""
    con = duckdb.connect()
    df = con.execute(
        f"SELECT symbol, time, {', '.join(HORIZONS)} FROM read_parquet('{LABELS_GLOB}')"
    ).fetchdf()
    con.close()
    df["time"] = pd.to_datetime(df["time"])
    df = df.rename(columns={"time": "trade_date"})
    df["symbol"] = df["symbol"].map(lambda v: StockCodeUtil.to_prefix(str(v)))
    df = df.dropna(subset=["symbol", "trade_date"]).drop_duplicates(
        subset=["symbol", "trade_date"], keep="last"
    )
    log.info("labels loaded: %d rows (%s ~ %s)", len(df),
             df["trade_date"].min().date(), df["trade_date"].max().date())
    return df


def compute_ic_series(
    frame: pd.DataFrame, factor_cols: list[str], forward_col: str
) -> pd.DataFrame:
    """逐日横截面 RankIC 序列（index=trade_date, columns=factor_cols）。

    口径：**成对完整（pairwise-complete）秩相关** —— 每个因子各自与目标在其有效配对样本上
    先取秩再算 Pearson。三处历史缺陷都在这里修掉（见 tests/data_platform/test_evaluate_alpha_library_ic.py）：
    1. 不在外面预排名：预排名是按「各列全部有效样本」排序，NaN 多时其秩与成对样本上的秩不是
       同一个分布，Pearson 会系统性偏低（实测 50% NaN 时 1.0 被算成 0.995、真实 0.65 被报成 0.32）
    2. corrwith 逐列成对剔除 NaN，分母即有效配对样本数（旧实现分母用全截面 len(g)）
    3. 常量列 corrwith 直接给 NaN，不会像旧实现那样因分母 ~1e-16 溢出成 ±inf
    """
    need = factor_cols + [forward_col, "trade_date"]
    sub = frame[need].copy()

    def _daily_corr(g: pd.DataFrame) -> pd.Series:
        if len(g) < 20:
            return pd.Series(np.nan, index=factor_cols)
        target = g[forward_col]
        # 目标本身有效样本太少或当日无变化 → 整日不可用
        if target.notna().sum() < 20 or target.std() < 1e-9:
            return pd.Series(np.nan, index=factor_cols)
        return g[factor_cols].corrwith(target, method="spearman")

    # 逐日循环（不用 groupby.apply：pandas 2.2+ 会警告 grouping 列参与运算，且各版本行为不一致）
    rows: dict = {}
    for trade_date, g in sub.groupby("trade_date"):
        rows[trade_date] = _daily_corr(g)
    ic_df = pd.DataFrame.from_dict(rows, orient="index")
    ic_df.index.name = "trade_date"
    return ic_df


def ic_stats(ic_series: pd.Series, factor: str, horizon: str) -> dict:
    s = ic_series.dropna()
    n = len(s)
    if n < 20:
        return {"factor": factor, "horizon": horizon, "ic_mean": np.nan, "icir": np.nan,
                "win_rate": np.nan, "t_value": np.nan, "sample_days": n}
    mean = s.mean()
    std = s.std()
    icir = mean / std if std > 1e-9 else 0.0
    return {
        "factor": factor, "horizon": horizon,
        "ic_mean": mean, "icir": icir,
        "win_rate": (s > 0).mean(),
        "t_value": icir * np.sqrt(n),
        "sample_days": n,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--horizon", type=int, default=None, help="只看某个前瞻期（1/2/5/10/20）")
    ap.add_argument("--top", type=int, default=0, help="只输出 Top N")
    ap.add_argument("--start-year", type=int, default=2016)
    ap.add_argument("--factor-block", type=int, default=70, help="每块因子数（内存控制）")
    args = ap.parse_args()

    t0 = time.time()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    labels = load_labels()
    horizons = [f"fwd_ret_{args.horizon}"] if args.horizon else list(HORIZONS)

    # 429 因子清单
    SKIP = {27, 30, 50, 51, 55, 69, 73, 121, 127, 128, 131, 137, 143, 146, 147, 149, 151, 165, 166, 181, 183}
    all_factors = [f"a101_{i:03d}" for i in range(1, 102)]
    all_factors += [f"gtja_{i:03d}" for i in range(1, 192) if i not in SKIP]
    all_factors += ["a158_KMID", "a158_KLEN", "a158_KMID2", "a158_KUP", "a158_KUP2",
                    "a158_KLOW", "a158_KLOW2", "a158_KSFT", "a158_KSFT2",
                    "a158_OPEN0", "a158_HIGH0", "a158_LOW0", "a158_VWAP0"]
    all_factors += [f"a158_{b}{w}" for b in
                    ["ROC", "MA", "BETA", "RSQR", "RESI", "STD", "MAX", "MIN", "QTLU",
                     "QTLD", "RANK", "RSV", "IMAX", "IMIN", "IMXD", "CORR", "CORD",
                     "CNTP", "CNTN", "CNTD", "SUMP", "SUMN", "SUMD", "VMA", "VSTD",
                     "WVMA", "VSUMP", "VSUMN", "VSUMD"]
                    for w in (5, 10, 20, 30, 60)]
    assert len(all_factors) == 429, len(all_factors)
    log.info("因子清单: %d 个, 前瞻期: %s", len(all_factors), horizons)

    reader = QuantDBFactorReader(
        data_dir=PROJECT_ROOT / "data" / "quantdb", market="CN"
    )
    start = max(date(args.start_year, 1, 1), date(2016, 1, 4))  # 钳制到数据起点
    end = date(2026, 8, 28)

    rows: list[dict] = []
    n_block = (len(all_factors) + args.factor_block - 1) // args.factor_block
    for bi in range(n_block):
        block = all_factors[bi * args.factor_block:(bi + 1) * args.factor_block]
        tb = time.time()
        frame = reader.read_range("alpha_library", features=block,
                                  start=start, end=end)
        # 剔除未来收益全 NaN 的尾部（避免无意义样本）
        merged = frame.merge(labels, on=["symbol", "trade_date"], how="left")
        log.info("[block %d/%d] %d 因子 %d 行 读取+合并 %.0fs",
                 bi + 1, n_block, len(block), len(merged), time.time() - tb)
        for hz in horizons:
            ic_df = compute_ic_series(merged, block, hz)
            for fac in block:
                rows.append(ic_stats(ic_df[fac], fac, hz))
        del merged, frame

    res = pd.DataFrame(rows)
    res.to_csv(OUT_DIR / "ic_results_all.csv", index=False)

    # ── 排序与汇总 ──
    # 综合分 = |IC| × |ICIR|（同时要求强度与稳定性）。
    # 注意 ICIR 必须取绝对值：反向因子（IC<0、ICIR<0）同样有效，
    # 旧实现漏了绝对值，把 ICIR=-0.65 的最强反向因子 gtja_070 排到 422/422 倒数第一。
    res["score"] = res["ic_mean"].abs() * res["icir"].abs()
    summary = res[res["horizon"] == "fwd_ret_5"].copy()
    summary = summary.sort_values("score", ascending=False)
    summary = summary.reset_index(drop=True)

    top = summary.head(args.top) if args.top else summary
    print("\n" + "=" * 100)
    print(f"Alpha 库因子 IC 检验排序（T+5 前瞻，{args.start_year}~2026-08，共 {len(summary)} 因子）")
    print("=" * 100)
    print(f"{'排名':>4} {'因子':<14} {'IC均值':>8} {'ICIR':>7} {'胜率':>7} {'t值':>7} {'评分':>8}  {'类别'}")
    for i, r in top.iterrows():
        cat = "Alpha101" if r["factor"].startswith("a101") else "GTJA191" if r["factor"].startswith("gtja") else "Alpha158"
        print(f"{i + 1:>4} {r['factor']:<14} {r['ic_mean']:>8.4f} {r['icir']:>7.3f} "
              f"{r['win_rate']:>7.1%} {r['t_value']:>7.2f} {r['score']:>8.4f}  {cat}")

    # 通过阈值统计（ICIR 同样取绝对值：反向因子与正向因子一视同仁）
    for th_ic, th_icir in [(0.02, 0.3), (0.03, 0.3), (0.02, 0.5)]:
        passed = summary[(summary["ic_mean"].abs() >= th_ic) & (summary["icir"].abs() >= th_icir)]
        print(f"\n通过 |IC|>={th_ic} 且 ICIR>={th_icir}: {len(passed)} 个")
        if len(passed):
            print("  " + ", ".join(passed["factor"].head(20)))
    print(f"\n结果已保存: {OUT_DIR / 'ic_results_all.csv'}（全部因子 × 全部前瞻期）")
    print(f"总耗时 {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
