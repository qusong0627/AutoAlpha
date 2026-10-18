#!/usr/bin/env python3
"""港股历史日线 → QuantHK 导入脚本。

从外部 duckdb (stock.duckdb) 读取港股全市场日线历史数据，按 QuantDB 的
Hive 分区格式落盘到 quanthk 本地 parquet，作为 yahoo 日线的历史补充。

数据源结构 (stock_data 表):
  date DATE, open/high/low/close DOUBLE, volume BIGINT, amount DOUBLE,
  stock_code VARCHAR, stock_name VARCHAR, market VARCHAR

落盘格式:
  {quanthk}/1_kline_data/daily_forward/dt=YYYYMMDD/data.parquet
  stock_code 5位（00001）→ 4位+.HK（0001.HK），与 quanthk 现有日线对齐。

用法:
  python backend/scripts/quanthk_import_stock_duckdb.py --source /path/to/stock.duckdb
  python backend/scripts/quanthk_import_stock_duckdb.py --source ... --start 2020-01-01
  python backend/scripts/quanthk_import_stock_duckdb.py --source ... --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("quanthk_import_stock_duckdb")

QUANTHK_DATA_DIR = Path(
    os.getenv("QM_QUANTHK_DATA_DIR", str(PROJECT_ROOT / "data" / "quanthk"))
)
REL_DIR = "1_kline_data/daily_forward"

# 输出列（与 quanthk 日线对齐，含 yahoo 元数据列）
OUT_COLS = [
    "symbol", "time", "open", "high", "low", "close",
    "volume", "amount", "release_id", "published_at",
]


def _quanthk_root() -> Path:
    env_val = os.getenv("QM_QUANTHK_DATA_DIR", "").strip()
    if env_val:
        p = Path(env_val)
        p.mkdir(parents=True, exist_ok=True)
        return p
    if Path("/data/quanthk").is_dir():
        return Path("/data/quanthk")
    QUANTHK_DATA_DIR.mkdir(parents=True, exist_ok=True)
    return QUANTHK_DATA_DIR


def _to_qhk_symbol(code: str) -> str:
    """5位港股代码 → 4位+.HK。00001 → 0001.HK，00700 → 0700.HK。"""
    code = code.strip().zfill(5)
    if code.startswith("0") and len(code) == 5:
        code = code[1:]
    return f"{code}.HK"


def _load_dates(source: str, start: str | None, end: str | None) -> list[str]:
    """列出待导入的交易日（YYYYMMDD）。"""
    import duckdb

    con = duckdb.connect(source, read_only=True)
    sql = "SELECT DISTINCT date FROM stock_data WHERE market='HK'"
    conds = []
    if start:
        conds.append(f"date >= DATE '{start}'")
    if end:
        conds.append(f"date <= DATE '{end}'")
    if conds:
        sql += " AND " + " AND ".join(conds)
    rows = con.execute(sql).fetchall()
    con.close()
    dates = [d[0].strftime("%Y%m%d") for d in rows if d[0] is not None]
    return sorted(dates)


def _import_date(source: str, date_str: str, target_dir: Path, *, dry_run: bool = False) -> int:
    """导入单日分区。返回行数。"""
    import duckdb

    dt = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
    con = duckdb.connect(source, read_only=True)
    df = con.execute(
        f"SELECT date, open, high, low, close, volume, amount, stock_code, stock_name "
        f"FROM stock_data WHERE market='HK' AND date = DATE '{dt}'"
    ).df()
    con.close()

    if df.empty:
        log.warning("  %s 无数据", date_str)
        return 0

    # 标准化 + 代码转换
    df = df.rename(columns={"date": "time", "stock_code": "symbol"})
    df["symbol"] = df["symbol"].astype(str).map(_to_qhk_symbol)
    df["time"] = pd.to_datetime(df["time"]).dt.date
    for c in ("open", "high", "low", "close"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0).astype("int64")
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce").fillna(0.0)
    df["release_id"] = "stock_duckdb"
    df["published_at"] = datetime.now().isoformat(timespec="seconds")
    df = df[OUT_COLS].dropna(subset=["close"])

    if dry_run:
        return len(df)

    # 按日分区落盘（已有分区合并去重，按 symbol+time）
    dt_dir = target_dir / f"dt={date_str}"
    dt_dir.mkdir(parents=True, exist_ok=True)
    target = dt_dir / "data.parquet"
    if target.exists():
        old = pd.read_parquet(target)
        combined = pd.concat([old, df], ignore_index=True)
        combined = combined.drop_duplicates(subset=["symbol", "time"], keep="last")
        combined.to_parquet(target, index=False)
    else:
        df.to_parquet(target, index=False)

    return len(df)


def import_stock(source: str, *, start: str | None = None, end: str | None = None, dry_run: bool = False) -> dict:
    """全量导入港股历史日线到 quanthk。"""
    if not Path(source).is_file():
        raise FileNotFoundError(f"duckdb 文件不存在: {source}")

    root = _quanthk_root()
    target_dir = root / REL_DIR

    dates = _load_dates(source, start, end)
    log.info("待导入交易日: %d 个 (%s ~ %s)", len(dates), dates[0] if dates else "-", dates[-1] if dates else "-")

    if dry_run:
        return {"source": source, "days": len(dates), "dry_run": True, "dir": str(target_dir)}

    total_rows = 0
    imported = 0
    for i, date_str in enumerate(dates):
        rows = _import_date(source, date_str, target_dir)
        total_rows += rows
        imported += 1
        if (i + 1) % 200 == 0 or i == len(dates) - 1:
            log.info("  进度 %d/%d，累计 %d 行", i + 1, len(dates), total_rows)

    return {
        "source": source,
        "days": imported,
        "rows": total_rows,
        "dir": str(target_dir),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="港股历史日线 → QuantHK")
    parser.add_argument("--source", required=True, help="stock.duckdb 路径")
    parser.add_argument("--start", default=None, help="起始日期 YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="结束日期 YYYY-MM-DD")
    parser.add_argument("--dry-run", action="store_true", help="仅列出待导入，不写盘")
    args = parser.parse_args()

    try:
        result = import_stock(args.source, start=args.start, end=args.end, dry_run=args.dry_run)
        print(result)
        return 0
    except Exception as exc:  # noqa: BLE001
        log.error("导入失败: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
