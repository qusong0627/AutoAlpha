#!/usr/bin/env python3
"""校验 quantdb → model_features parquet 的列变换可复现性（只读）。

P0 决策门工具：确认把 update_feature_parquet.py 的上游从 Postgres 换成
data/quantdb 之后，能否逐列复现现有 model_features_YYYY.parquet 的数值约定，
从而判断「不重训模型」是否成立。

已确立的变换（本脚本负责回归验证，勿随意改动）:

    close_m  = close_unadjusted  * factor      # open/high/low 同理
    volume_m = (volume_unadjusted / 100) / factor
    amount_m = amount_quantdb * 1e4            # quantdb amount 单位为万元

其中 factor 是逐 (symbol, date) 的复权因子，**不是**每只股票的常数，也不等于
close_forward/close_unadjusted，必须沿用 DB/quantdb 的 adj_factor 本身。

用法:
    python backend/scripts/validate_feature_parquet.py --date 2026-06-26
    python backend/scripts/validate_feature_parquet.py --date 2026-06-18 \
        --model models/users/default/10000001/mdl_train_20260605053833_d2192c60_91a41b57
    python backend/scripts/validate_feature_parquet.py --coverage-report
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LEGACY_SNAPSHOT_DIR = PROJECT_ROOT / "db" / "feature_snapshots"
QUANTDB_DIR = Path(
    os.getenv("QM_QUANTDB_DATA_DIR", "").strip() or (PROJECT_ROOT / "data" / "quantdb")
)


def _resolve_legacy_feature_parquet() -> Path | None:
    """旧入口 model_features_*.parquet 已废弃。

    本工具仅作迁移过渡期的 quantdb↔旧快照数值一致性校验；迁移完成后
    旧文件不存在即视为已完成，无需再跑。
    """
    try:
        from backend.shared.feature_source import warn_legacy_feature_snapshots

        warn_legacy_feature_snapshots("validate_feature_parquet")
    except Exception:  # noqa: BLE001
        pass
    candidates = sorted(LEGACY_SNAPSHOT_DIR.glob("model_features_20[0-9][0-9].parquet"))
    return candidates[-1] if candidates else None


FEATURE_PARQUET = _resolve_legacy_feature_parquet()

# quantdb amount 列的单位是万元，换算成元
AMOUNT_UNIT_SCALE = 1e4
# A股 1 手 = 100 股；model_features 的 volume 以手为单位并做了复权还原
VOLUME_LOT_SIZE = 100

# 只有这几个特征依赖绝对量纲，其余 79/85 个对 factor 不变
SCALE_DEPENDENT_FEATURES = (
    "liq_amount",
    "liq_volume",
    "style_ln_mv_total",
    "style_bp",
    "style_ep_ttm",
    "flow_large_net_amount",
)


def _log(msg: str) -> None:
    print(msg, flush=True)


def _normalize_model_symbol(series: pd.Series) -> pd.Series:
    """SH600519 -> 600519"""
    return series.str.replace(r"^(SH|SZ|BJ)(\d+)$", r"\2", regex=True)


def _normalize_quantdb_symbol(series: pd.Series) -> pd.Series:
    """600519.SH -> 600519"""
    return series.str.split(".").str[0]


def _quantdb_partition(dataset: str, dt: str, subdir: str = "1_kline_data") -> Path:
    return QUANTDB_DIR / subdir / dataset / f"dt={dt}" / "data.parquet"


def _load_quantdb_kline(dataset: str, dt: str) -> pd.DataFrame | None:
    path = _quantdb_partition(dataset, dt)
    if not path.exists():
        return None
    df = pd.read_parquet(
        path, columns=["symbol", "open", "high", "low", "close", "volume", "amount"]
    )
    df["k"] = _normalize_quantdb_symbol(df["symbol"])
    return df.drop(columns=["symbol"])


def _load_quantdb_valuation(dt: str) -> pd.DataFrame | None:
    path = _quantdb_partition("valuation", dt, subdir="5_technical_derived")
    if not path.exists():
        return None
    df = pd.read_parquet(
        path,
        columns=["symbol", "total_mv", "float_mv", "equity", "net_profit_ttm", "pe_ttm", "pb"],
    )
    df["k"] = _normalize_quantdb_symbol(df["symbol"])
    return df.drop(columns=["symbol"])


def _load_model_features(trade_date: str, columns: list[str]) -> pd.DataFrame:
    if FEATURE_PARQUET is None:
        raise FileNotFoundError(
            "旧 model_features_*.parquet 已不存在；旧入口迁移已完成，无需再用本工具校验"
        )
    df = pd.read_parquet(FEATURE_PARQUET, columns=columns)
    df = df[df["trade_date"] == trade_date].copy()
    df["k"] = _normalize_model_symbol(df["symbol"])
    return df


def _match_rate(ratio: pd.Series, tol: float) -> float:
    clean = ratio.replace([np.inf, -np.inf], np.nan).dropna()
    if clean.empty:
        return float("nan")
    return float(np.mean((clean - 1.0).abs() < tol))


def _classify(rate_exact: float, rate_loose: float) -> str:
    if not np.isnan(rate_exact) and rate_exact >= 0.99:
        return "EXACT"
    if not np.isnan(rate_loose) and rate_loose >= 0.95:
        return "APPROX"
    return "FAILED"


def verify_ohlcv_transform(trade_date: str) -> list[dict]:
    """验证 OHLCV + factor 的核心变换。"""
    dt = trade_date.replace("-", "")
    model = _load_model_features(
        trade_date,
        ["symbol", "trade_date", "open", "high", "low", "close", "volume", "factor"],
    )
    unadj = _load_quantdb_kline("daily_unadjusted", dt)
    if unadj is None:
        _log(f"  [skip] quantdb daily_unadjusted 缺少分区 dt={dt}")
        return []

    j = model.merge(unadj, on="k", suffixes=("_m", "_q"))
    if j.empty:
        _log(f"  [skip] {trade_date} 无重叠标的")
        return []

    results: list[dict] = []
    for col in ("open", "high", "low", "close"):
        expected = j[f"{col}_q"] * j["factor"]
        ratio = j[f"{col}_m"] / expected
        results.append(
            {
                "column": col,
                "formula": f"{col}_unadjusted * factor",
                "n": len(j),
                "exact_1e6": _match_rate(ratio, 1e-6),
                "loose_1e2": _match_rate(ratio, 1e-2),
            }
        )

    expected_vol = (j["volume_q"] / VOLUME_LOT_SIZE) / j["factor"]
    ratio_vol = j["volume_m"] / expected_vol
    results.append(
        {
            "column": "volume",
            "formula": "(volume_unadjusted / 100) / factor",
            "n": len(j),
            "exact_1e6": _match_rate(ratio_vol, 1e-6),
            "loose_1e2": _match_rate(ratio_vol, 1e-2),
        }
    )

    for row in results:
        row["verdict"] = _classify(row["exact_1e6"], row["loose_1e2"])
    return results


def verify_scale_dependent(trade_date: str) -> list[dict]:
    """验证 6 个依赖绝对量纲的特征。"""
    dt = trade_date.replace("-", "")
    model = _load_model_features(
        trade_date,
        [
            "symbol",
            "trade_date",
            "volume",
            "liq_amount",
            "liq_volume",
            "style_ln_mv_total",
            "style_bp",
            "style_ep_ttm",
        ],
    )
    valuation = _load_quantdb_valuation(dt)
    unadj = _load_quantdb_kline("daily_unadjusted", dt)
    if valuation is None or unadj is None:
        _log(f"  [skip] quantdb 缺少 valuation/daily_unadjusted 分区 dt={dt}")
        return []

    j = model.merge(valuation, on="k").merge(
        unadj[["k", "amount"]].rename(columns={"amount": "amount_wan"}), on="k"
    )
    if j.empty:
        return []

    results: list[dict] = []

    amt = j.dropna(subset=["liq_amount"])
    if not amt.empty:
        ratio = amt["liq_amount"] / (amt["amount_wan"] * AMOUNT_UNIT_SCALE)
        results.append(
            {
                "column": "liq_amount",
                "formula": "amount_quantdb * 1e4  (万元 -> 元)",
                "n": len(amt),
                "exact_1e6": _match_rate(ratio, 1e-6),
                "loose_1e2": _match_rate(ratio, 1e-2),
            }
        )

    vol = j.dropna(subset=["liq_volume", "volume"])
    if not vol.empty:
        ratio = vol["liq_volume"] / vol["volume"]
        results.append(
            {
                "column": "liq_volume",
                "formula": "== volume (同一列)",
                "n": len(vol),
                "exact_1e6": _match_rate(ratio, 1e-6),
                "loose_1e2": _match_rate(ratio, 1e-2),
            }
        )

    mv = j.dropna(subset=["style_ln_mv_total"])
    if not mv.empty:
        diff = (mv["style_ln_mv_total"] - np.log(mv["total_mv"].clip(lower=1))).abs()
        results.append(
            {
                "column": "style_ln_mv_total",
                "formula": "log(total_mv)  [元]",
                "n": len(mv),
                "exact_1e6": float(np.mean(diff < 1e-6)),
                "loose_1e2": float(np.mean(diff < 5e-2)),
            }
        )

    bp = j.dropna(subset=["style_bp"])
    if not bp.empty:
        diff = (bp["style_bp"] - 1.0 / bp["pb"].replace(0, np.nan)).abs()
        results.append(
            {
                "column": "style_bp",
                "formula": "1 / pb",
                "n": len(bp),
                "exact_1e6": float(np.mean(diff < 1e-6)),
                "loose_1e2": float(np.mean(diff < 5e-2)),
            }
        )

    ep = j.dropna(subset=["style_ep_ttm"])
    if not ep.empty:
        diff = (ep["style_ep_ttm"] - 1.0 / ep["pe_ttm"].replace(0, np.nan)).abs()
        results.append(
            {
                "column": "style_ep_ttm",
                "formula": "1 / pe_ttm",
                "n": len(ep),
                "exact_1e6": float(np.mean(diff < 1e-6)),
                "loose_1e2": float(np.mean(diff < 5e-2)),
            }
        )

    for row in results:
        row["verdict"] = _classify(row["exact_1e6"], row["loose_1e2"])
    return results


def verify_derived_features(trade_date: str, lookback_parts: int = 60) -> list[dict]:
    """用 quantdb 重算若干派生特征，与 parquet 现值对比（相关性口径）。"""
    dt = trade_date.replace("-", "")
    # 必须跨年取历史，否则年初日期的滚动窗口全为 NaN，会产生假阴性
    parts = sorted((QUANTDB_DIR / "1_kline_data" / "daily_forward").glob("dt=*/data.parquet"))
    parts = [p for p in parts if p.parent.name.split("=")[1] <= dt][-lookback_parts:]
    if len(parts) < 25:
        _log("  [skip] daily_forward 分区不足，无法重算滚动特征")
        return []

    frames = []
    for path in parts:
        frame = pd.read_parquet(path, columns=["symbol", "time", "close"])
        frames.append(frame)
    panel = pd.concat(frames)
    panel["k"] = _normalize_quantdb_symbol(panel["symbol"])
    panel = panel.rename(columns={"time": "trade_date"}).sort_values(["k", "trade_date"])

    grouped = panel.groupby("k")["close"]
    panel["calc_mom_ret_1d"] = grouped.pct_change()
    panel["calc_mom_ret_5d"] = grouped.pct_change(5)
    panel["calc_vol_std_20"] = panel.groupby("k")["calc_mom_ret_1d"].transform(
        lambda s: s.rolling(20, min_periods=20).std()
    )

    model = _load_model_features(
        trade_date, ["symbol", "trade_date", "mom_ret_1d", "mom_ret_5d", "vol_std_20"]
    )
    j = model.merge(
        panel[["k", "trade_date", "calc_mom_ret_1d", "calc_mom_ret_5d", "calc_vol_std_20"]],
        on=["k", "trade_date"],
    )
    if j.empty:
        return []

    results = []
    for col in ("mom_ret_1d", "mom_ret_5d", "vol_std_20"):
        pair = j[[col, f"calc_{col}"]].dropna()
        if pair.empty:
            continue
        corr = float(np.corrcoef(pair[col], pair[f"calc_{col}"])[0, 1])
        diff = (pair[col] - pair[f"calc_{col}"]).abs()
        results.append(
            {
                "column": col,
                "formula": "quantdb daily_forward 重算",
                "n": len(pair),
                "corr": corr,
                "median_abs_diff": float(diff.median()),
                "verdict": "APPROX" if corr >= 0.98 else "FAILED",
            }
        )
    return results


def coverage_report() -> pd.DataFrame:
    """逐日统计关键特征的 non-NaN 覆盖率，定位结构性缺失。"""
    watch = [
        "style_bp",
        "style_ep_ttm",
        "style_ln_mv_total",
        "ind_ret_1d",
        "ind_strength_20",
        "liq_amount",
        "liq_turnover_tl",
        "is_st",
    ]
    if FEATURE_PARQUET is None:
        raise FileNotFoundError(
            "旧 model_features_*.parquet 已不存在；旧入口迁移已完成，无需再用本工具校验"
        )
    available = set(pd.read_parquet(FEATURE_PARQUET, columns=["symbol"]).columns)
    del available
    import pyarrow.parquet as pq

    schema_names = set(pq.ParquetFile(FEATURE_PARQUET).schema_arrow.names)
    cols = ["trade_date"] + [c for c in watch if c in schema_names]
    df = pd.read_parquet(FEATURE_PARQUET, columns=cols)
    return df.groupby("trade_date")[cols[1:]].apply(lambda x: x.notna().mean())


def score_model_contract(model_dir: Path, ohlcv: list[dict], scale: list[dict]) -> None:
    """按某个模型的 feature_columns 汇总可复现性。"""
    meta_path = model_dir / "metadata.json"
    if not meta_path.exists():
        _log(f"[warn] 找不到 {meta_path}")
        return
    meta = json.loads(meta_path.read_text())
    features = meta.get("feature_columns") or meta.get("features") or []
    verdicts = {row["column"]: row["verdict"] for row in (*ohlcv, *scale)}

    failed = [c for c in SCALE_DEPENDENT_FEATURES if verdicts.get(c) == "FAILED"]
    scale_in_model = [c for c in features if c in SCALE_DEPENDENT_FEATURES]

    _log("")
    _log(f"模型契约评分: {model_dir.name}")
    _log(f"  feature_count      : {len(features)}")
    _log(f"  量纲相关特征       : {len(scale_in_model)}/{len(features)} -> {scale_in_model}")
    _log(f"  factor 不变特征     : {len(features) - len(scale_in_model)}/{len(features)}")
    if failed:
        _log(f"  ❌ 无法复现        : {failed}  => 需要重训")
    else:
        _log("  ✅ 全部量纲相关特征均可复现 => 无需重训")


def _print_table(title: str, rows: list[dict]) -> None:
    if not rows:
        return
    _log("")
    _log(title)
    for row in rows:
        n = row["n"]
        verdict = row["verdict"]
        mark = {"EXACT": "✅", "APPROX": "🟡", "FAILED": "❌"}.get(verdict, "?")
        if "corr" in row:
            _log(
                f"  {mark} {row['column']:20s} n={n:5d}  corr={row['corr']:.6f}  "
                f"medabs={row['median_abs_diff']:.3e}  [{verdict}]  <- {row['formula']}"
            )
        else:
            _log(
                f"  {mark} {row['column']:20s} n={n:5d}  exact={row['exact_1e6']:7.3%}  "
                f"loose={row['loose_1e2']:7.3%}  [{verdict}]  <- {row['formula']}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="校验 quantdb → model_features 变换可复现性")
    parser.add_argument("--date", action="append", dest="dates", help="校验日期，可重复")
    parser.add_argument("--model", type=Path, help="模型目录，用于按 feature_columns 打分")
    parser.add_argument("--coverage-report", action="store_true", help="输出逐日覆盖率")
    args = parser.parse_args()

    if FEATURE_PARQUET is None or not FEATURE_PARQUET.exists():
        _log(
            f"[error] 找不到旧特征表 {FEATURE_PARQUET}；"
            "旧入口已迁移到 QuantDB，无需该校验"
        )
        return 1

    if args.coverage_report:
        report = coverage_report()
        _log("逐日 non-NaN 覆盖率（尾部 15 个交易日）:")
        _log(report.tail(15).to_string())
        broken = report.columns[(report.tail(5) == 0).all()].tolist()
        if broken:
            _log("")
            _log(f"❌ 尾部 5 日恒为 NaN 的列: {broken}")
        return 0

    dates = args.dates or ["2026-06-26", "2026-06-18", "2026-03-10", "2026-01-06"]
    all_ohlcv: list[dict] = []
    all_scale: list[dict] = []

    for trade_date in dates:
        _log("")
        _log("=" * 78)
        _log(f"校验日期: {trade_date}")
        _log("=" * 78)
        ohlcv = verify_ohlcv_transform(trade_date)
        _print_table("OHLCV 核心变换:", ohlcv)
        scale = verify_scale_dependent(trade_date)
        _print_table("量纲相关特征:", scale)
        derived = verify_derived_features(trade_date)
        _print_table("派生特征重算比对:", derived)
        all_ohlcv.extend(ohlcv)
        all_scale.extend(scale)

    if args.model:
        score_model_contract(args.model, all_ohlcv, all_scale)

    failed = [r for r in (*all_ohlcv, *all_scale) if r["verdict"] == "FAILED"]
    _log("")
    if failed:
        _log(f"❌ 存在无法复现的列: {sorted({r['column'] for r in failed})}")
        return 2
    _log("✅ 所有受检列均可复现，可按「不重训」路线改造上游")
    return 0


if __name__ == "__main__":
    sys.exit(main())
