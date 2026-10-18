#!/usr/bin/env python3
"""将后复权日线 OHLCV 物化回填到 L1 因子分区。

历史 ``l1_factors_YYYYMMDD.parquet`` 只包含因子；缺少 OHLCV 会使直连训练
无法构造未来收益标签。本脚本按分区逐文件原子改写，因子值绝不改动。
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from collections import Counter
from collections.abc import Iterable
from datetime import date
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.shared.stock_utils import StockCodeUtil

log = logging.getLogger("backfill_l1_ohlcv")
OHLCV_COLUMNS = ("open", "high", "low", "close", "volume", "amount")
_DATE_RE = re.compile(r"(?:dt=|l1_factors_)(\d{8})")


def _partition_date(path: Path) -> str | None:
    match = _DATE_RE.search(str(path).replace("\\", "/"))
    return match.group(1) if match else None


def _factor_files(root: Path, start: str | None, end: str | None) -> Iterable[Path]:
    for path in sorted(root.rglob("*.parquet")):
        dt = _partition_date(path)
        if dt is None:
            continue
        if start and dt < start:
            continue
        if end and dt > end:
            continue
        yield path


def _symbol_key(values: pd.Series) -> pd.Series:
    return values.map(lambda value: StockCodeUtil.to_suffix(str(value or "")))


def _backfill_file(
    factor_path: Path,
    daily_root: Path,
    *,
    dry_run: bool,
) -> tuple[str, int, int]:
    """返回 (状态, 回填单元数, 未匹配行数)。"""
    dt = _partition_date(factor_path)
    if not dt:
        return "skip_no_date", 0, 0
    daily_path = daily_root / f"dt={dt}" / "data.parquet"
    if not daily_path.exists():
        return "skip_no_daily", 0, 0

    factor = pd.read_parquet(factor_path)
    if factor.empty or "symbol" not in factor.columns:
        return "skip_invalid_factor", 0, 0
    daily = pd.read_parquet(daily_path, columns=["symbol", *OHLCV_COLUMNS])
    if daily.empty or "symbol" not in daily.columns:
        return "skip_invalid_daily", 0, 0

    daily = daily.copy()
    daily["_symbol_key"] = _symbol_key(daily["symbol"])
    daily = daily.drop_duplicates(subset="_symbol_key", keep="last").set_index("_symbol_key")
    factor_key = _symbol_key(factor["symbol"])
    unmatched = int((~factor_key.isin(daily.index)).sum())
    changed = 0

    for column in OHLCV_COLUMNS:
        values = factor_key.map(daily[column])
        if column not in factor.columns:
            missing = pd.Series(True, index=factor.index)
            factor[column] = values.to_numpy()
        else:
            missing = factor[column].isna()
            if not missing.any():
                continue
            factor.loc[missing, column] = values.loc[missing].to_numpy()
        changed += int((missing & values.notna()).sum())

    if not changed:
        return "unchanged", 0, unmatched
    if dry_run:
        return "would_update", changed, unmatched

    # 同目录临时文件 + replace，任何中断都不会留下半写入的有效目标文件。
    temp_path = factor_path.with_suffix(".ohlcv.tmp.parquet")
    try:
        factor.to_parquet(temp_path, index=False)
        verified = pd.read_parquet(temp_path, columns=["symbol", *OHLCV_COLUMNS])
        if len(verified) != len(factor) or int(verified["close"].notna().sum()) == 0:
            raise RuntimeError("temporary parquet verification failed")
        os.replace(temp_path, factor_path)
    finally:
        temp_path.unlink(missing_ok=True)
    return "updated", changed, unmatched


def backfill_l1_ohlcv(
    data_dir: str | Path,
    *,
    start: str | date | None = None,
    end: str | date | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    root = Path(data_dir)
    l1_root = root / "6_ml_datasets" / "l1_factors"
    daily_root = root / "1_kline_data" / "daily_backward"
    if not l1_root.is_dir() or not daily_root.is_dir():
        raise FileNotFoundError(f"missing l1_factors or daily_backward under {root}")

    start_s = pd.Timestamp(start).strftime("%Y%m%d") if start else None
    end_s = pd.Timestamp(end).strftime("%Y%m%d") if end else None
    result: Counter[str] = Counter()
    for index, factor_path in enumerate(_factor_files(l1_root, start_s, end_s), start=1):
        state, changed, unmatched = _backfill_file(factor_path, daily_root, dry_run=dry_run)
        result["files_scanned"] += 1
        result[state] += 1
        result["cells_backfilled"] += changed
        result["unmatched_rows"] += unmatched
        if index % 100 == 0:
            log.info("scanned=%d updated=%d cells=%d", index, result["updated"], result["cells_backfilled"])
    return dict(result)


def main() -> int:
    parser = argparse.ArgumentParser(description="回填 l1_factors 历史 OHLCV")
    parser.add_argument("--data-dir", default=os.getenv("QM_QUANTDB_DATA_DIR", "/data/quantdb"))
    parser.add_argument("--start", help="起始交易日 YYYY-MM-DD")
    parser.add_argument("--end", help="结束交易日 YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    result = backfill_l1_ohlcv(args.data_dir, start=args.start, end=args.end, dry_run=args.dry_run)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
