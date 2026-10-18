#!/usr/bin/env python3
"""市场 L1 因子日频数据集生成器（QuantHK/QuantUS → 6_ml_datasets/l1_factors）。

对齐 A 股 QuantDB 的 ML 数据集契约：
  {market_dir}/6_ml_datasets/l1_factors/dt=YYYYMMDD/data.parquet
  symbol / OHLCV / 日频因子列 —— 训练与回测直连读取，不再依赖 model_features_*.parquet。

因子计算复用 update_market_features.compute_market_features（与
model_features_{market}.parquet 同一实现、同一口径）；A 股专属列
（concept_*/idx_* 等）自动剔除。

用法:
  python backend/scripts/build_ml_l1_dataset.py --market hong_kong                 # 增量：补已有分区之后
  python backend/scripts/build_ml_l1_dataset.py --market hong_kong --rebuild --start-year 2019
"""

from __future__ import annotations

import argparse
import logging
import sys
import tempfile
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

log = logging.getLogger("build_ml_l1_dataset")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

RAW_COLS = ("open", "high", "low", "close", "volume", "amount")
_CN_ONLY_PREFIXES = ("concept_",)
_CN_ONLY_COLS = {
    "idx_all", "idx_hs300", "idx_zz1000", "idx_chinext", "idx_margin",
    "is_st", "listing_market", "ind_code_l1", "ind_code_l2",
}
# 同步系统元数据列：不进训练直读数据集。日期由 hive 分区列 dt=YYYYMMDD
# 承载（reader 经 _date_expression 读取），文件内再写 dt 会引入
# dictionary/double 混型（pandas 读分区列→字典编码→写盘类型不稳定）。
_META_COLS = {"release_id", "published_at", "dt"}


def _hub(market: str):
    if market == "hong_kong":
        from backend.services.engine.data_platform.quanthk_hub import QuantHKDataHub

        return QuantHKDataHub()
    if market == "crypto":
        from backend.services.engine.data_platform.quantbc_hub import QuantBCDataHub

        return QuantBCDataHub()
    if market == "futures":
        from backend.services.engine.data_platform.quantfutures_hub import QuantFuturesDataHub

        return QuantFuturesDataHub.get_instance()
    from backend.services.engine.data_platform.quantus_hub import QuantUSDataHub

    return QuantUSDataHub()


def _existing_partitions(root: Path, name: str = "l1_factors") -> set[str]:
    d = root / name
    return {p.name[3:] for p in d.glob("dt=*")} if d.is_dir() else set()


def _select_factor_columns(feats: pd.DataFrame, sym_col: str, date_col: str) -> list[str]:
    """排除 symbol/date、A 股专属列、同步元数据列后剩余的因子列（保持原始列序）。"""
    banned = set(_CN_ONLY_COLS) | set(_META_COLS) | {"raw_close", "factor"}
    return [
        c for c in feats.columns
        if c != sym_col and c != date_col
        and c not in banned
        and not c.startswith(_CN_ONLY_PREFIXES)
    ]


def build_l1(market: str, *, start_year: int | None = None, incremental: bool = True) -> dict:
    """计算市场 L1 因子并按 dt 分区写出。incremental 模式跳过已有分区。"""
    hub = _hub(market)
    ml_root = hub.data_dir / "6_ml_datasets"
    ml_root.mkdir(parents=True, exist_ok=True)

    existing = _existing_partitions(ml_root)
    warm_from_year = start_year or 2019

    log.info("[%s] 读日线 parquet (>= %d)", market, warm_from_year)
    from backend.scripts.update_market_features import (
        load_crypto_parquet,
        load_futures_parquet,
        load_hk_parquet,
        load_us_parquet,
    )

    if market == "hong_kong":
        kline = load_hk_parquet(start_year=warm_from_year)
    elif market == "crypto":
        kline = load_crypto_parquet()
    elif market == "futures":
        kline = load_futures_parquet()
    else:
        kline = load_us_parquet()
    if kline is None or kline.empty:
        return {"status": "no_data"}

    max_existing = max(existing) if existing else None
    if incremental and max_existing:
        # 计算窗口向前多留 ~190 自然日预热滚动因子；只写出更新的分区
        warm_cut = pd.Timestamp(max_existing) - pd.Timedelta(days=190)
        td = pd.to_datetime(kline["trade_date"])
        kline = kline[td >= warm_cut]

    log.info("[%s] 计算特征: %d 行", market, len(kline))
    from backend.scripts.update_market_features import compute_market_features

    feats = compute_market_features(kline, market)
    if feats is None or feats.empty:
        return {"status": "no_features"}

    sym_col = "instrument" if "instrument" in feats.columns else "symbol"
    date_col = "trade_date" if "trade_date" in feats.columns else "date"
    factor_cols = _select_factor_columns(feats, sym_col, date_col)
    col_order = ["symbol", *[c for c in RAW_COLS if c in feats.columns],
                 *factor_cols]
    col_order = list(dict.fromkeys(col_order))

    feats = feats.copy()
    feats[date_col] = pd.to_datetime(feats[date_col])
    written = 0
    last_written: str | None = None
    for dt_key, g in feats.groupby(feats[date_col].dt.date):
        dt_str = pd.Timestamp(dt_key).strftime("%Y%m%d")
        if dt_str <= "19700101":
            continue
        if incremental and existing and dt_str in existing:
            continue  # 增量模式：已有分区一律跳过；--rebuild 才覆盖重写
        renamed = g.rename(columns={sym_col: "symbol"})
        out = renamed[[c for c in col_order if c in renamed.columns]]
        out = out.replace([float("inf"), float("-inf")], None)
        dt_dir = ml_root / "l1_factors" / f"dt={dt_str}"
        dt_dir.mkdir(parents=True, exist_ok=True)
        target = dt_dir / "data.parquet"
        tmp_path = dt_dir / f".tmp-{target.name}"
        try:
            out.to_parquet(tmp_path, index=False)
            tmp_path.replace(target)
        finally:
            tmp_path.unlink(missing_ok=True)
        written += 1
        last_written = dt_str

    if incremental and max_existing:
        cut = (pd.Timestamp(max_existing)).strftime("%Y%m%d")
        return {"status": "ok", "mode": "incremental", "partitions_written": written,
                "since": cut, "last": last_written}
    return {"status": "ok", "mode": "full" if not existing else "skip-existing",
            "partitions_written": written, "last": last_written}


def materialize_from_snapshot(market: str) -> dict:
    """把既有 model_features_{market}.parquet 切片物化为 l1_factors 日频分区。

    用于首次回填历史（快照里已有完整因子，无需重算）；之后的新交易日
    由 build_l1 增量计算补齐。
    """
    hub = _hub(market)
    ml_root = hub.data_dir / "6_ml_datasets"
    ml_root.mkdir(parents=True, exist_ok=True)
    existing = _existing_partitions(ml_root)

    from backend.scripts.update_market_features import MARKET_PARQUET_NAMES

    snap_path = Path(PROJECT_ROOT) / "db" / "feature_snapshots" / MARKET_PARQUET_NAMES[market]
    if not snap_path.is_file():
        return {"status": "no_snapshot", "path": str(snap_path)}

    log.info("[%s] 从快照物化历史分区: %s", market, snap_path.name)
    import pyarrow.parquet as pq

    pf = pq.ParquetFile(str(snap_path))
    names = pf.schema_arrow.names
    sym_col = "instrument" if "instrument" in names else "symbol"
    date_col = "trade_date" if "trade_date" in names else "date"
    drop_set = set(_CN_ONLY_COLS) | {"raw_close", "factor"}
    written = 0
    # 流式落盘：攒批控制内存；同一日期跨 batch 边界时与已有分区做
    # concat+去重合并，保证不覆盖丢行也不重复行
    pending: dict[str, pd.DataFrame] = {}
    buf_rows = 0

    def _dt_dir(dt_str: str) -> Path:
        d = ml_root / "l1_factors" / f"dt={dt_str}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _write_partition(dt_str: str) -> None:
        nonlocal written
        out = pending.pop(dt_str)
        target = _dt_dir(dt_str) / "data.parquet"
        if target.exists():
            old = pd.read_parquet(target)
            out = pd.concat([old, out], ignore_index=True)
            out = out.drop_duplicates(subset=["symbol"], keep="last")
        tmp_path = target.parent / f".tmp-{target.name}"
        try:
            out.to_parquet(tmp_path, index=False)
            tmp_path.replace(target)
            written += 1
        finally:
            tmp_path.unlink(missing_ok=True)

    def _flush(force: bool = False) -> None:
        nonlocal buf_rows
        if force or buf_rows >= 1_500_000:
            for key in sorted(pending):
                _write_partition(key)
            buf_rows = 0

    for batch in pf.iter_batches(batch_size=500_000):
        df = batch.to_pandas()
        parsed = pd.to_datetime(df[date_col], errors="coerce")
        df["_dt"] = parsed.dt.strftime("%Y%m%d")
        df["date"] = parsed
        df["symbol"] = df[sym_col]
        drop_set2 = {"symbol", *RAW_COLS, sym_col, date_col, "_dt",
                     "date", "release_id", "published_at", *drop_set}
        cols = ["symbol", *[c for c in RAW_COLS if c in df.columns]]
        cols += [
            c for c in df.columns
            if c not in drop_set2
            and not c.startswith(_CN_ONLY_PREFIXES)
        ]
        cols = list(dict.fromkeys(cols))
        for dt_str, g in df.groupby("_dt"):
            g = g.dropna(subset=["symbol"])
            if g.empty or dt_str in existing or len(dt_str) != 8:
                continue
            if dt_str in pending:
                pending[dt_str] = pd.concat([pending[dt_str], g[cols]], ignore_index=True)
            else:
                buf_rows += len(g)
                pending[dt_str] = g[cols]
        _flush()

    _flush(force=True)
    log.info("[%s] 物化完成: %d 个新分区", market, written)
    return {"status": "ok", "partitions_written": written}


def main() -> int:
    parser = argparse.ArgumentParser(description="市场 L1 因子日频数据集生成")
    parser.add_argument("--market", required=True,
                        choices=["hong_kong", "us_stock", "futures", "crypto"])
    parser.add_argument("--start-year", type=int, default=None, help="因子计算起始年(默认2019)")
    parser.add_argument("--rebuild", action="store_true",
                        help="重算模式：不跳过已有分区之外的年份限制，重写新窗口分区")
    parser.add_argument("--from-model-features", action="store_true",
                        help="从既有 model_features 快照直接切片物化历史分区")
    args, _ = parser.parse_known_args()
    if args.from_model_features:
        result = materialize_from_snapshot(args.market)
    else:
        result = build_l1(args.market, start_year=args.start_year, incremental=not args.rebuild)
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
