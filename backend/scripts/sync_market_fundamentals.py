"""
港股/美股基本面数据同步（Quant 平台单源）
==========================================
从 QuantHK / QuantUS 本地 parquet 估值数据读取 PE/PB/PS/市值等基本面指标，
更新到 stock_daily_latest_hk / stock_daily_latest_us 表。
不再直连 yfinance/akshare 外部数据源。

用法:
    python sync_market_fundamentals.py [--market HK|US|ALL] [--dry-run]
"""

import argparse
import logging
import os
import sys
from pathlib import Path

import pandas as pd
from sqlalchemy import create_engine, text

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def _get_engine():
    db_url = (
        f"postgresql://{os.getenv('DB_USER', 'quantmind')}:{os.getenv('DB_PASSWORD', 'quantmind')}"
        f"@{os.getenv('DB_HOST', 'quantmind-db')}:{os.getenv('DB_PORT', '5432')}/{os.getenv('DB_NAME', 'quantmind')}"
    )
    return create_engine(db_url)


# ── Quant 平台估值读取 ──────────────────────────────────────────────

def _fetch_quant_valuation(market: str) -> pd.DataFrame:
    """从 Quant 平台本地 parquet 读取估值快照。"""
    if market == "HK":
        from backend.services.engine.data_platform.quanthk_hub import QuantHKDataHub

        return QuantHKDataHub().fetch_valuation()
    from backend.services.engine.data_platform.quantus_hub import QuantUSDataHub

    return QuantUSDataHub().fetch_valuation()


def _normalize_symbol(sym: str, market: str) -> str:
    """Quant parquet symbol → PG 表 symbol 格式。"""
    if market == "HK":
        # 0001.HK → 00001（PG 表用 5 位代码）
        return str(sym).replace(".HK", "").zfill(5)
    return str(sym)


# ── 数据库更新 ──────────────────────────────────────────────────────

def _update_db(engine, table: str, records: list[dict], market: str) -> int:
    """更新数据库中的基本面数据。"""
    if not records:
        return 0

    updated = 0
    with engine.begin() as conn:
        for rec in records:
            sym = rec["symbol"]
            set_parts = []
            params = {"sym": sym}

            for db_col in ("pe_ttm", "pb", "roe", "total_mv", "float_mv"):
                val = rec.get(db_col)
                if val is None or pd.isna(val):
                    continue
                if db_col in ("total_mv", "float_mv") and float(val) <= 0:
                    continue
                set_parts.append(f"{db_col} = :{db_col}")
                params[db_col] = float(val)

            if not set_parts:
                continue

            sql = f"UPDATE {table} SET {', '.join(set_parts)} WHERE symbol = :sym"
            res = conn.execute(text(sql), params)
            updated += res.rowcount

    return updated


# ── 主流程 ──────────────────────────────────────────────────────────

def sync_market_fundamentals(market: str, dry_run: bool = False) -> dict:
    """同步指定市场的基本面数据（Quant 平台 parquet 单源）。"""
    market = market.upper()

    table_map = {
        "HK": "stock_daily_latest_hk",
        "US": "stock_daily_latest_us",
    }
    if market not in table_map:
        return {"error": f"unsupported market: {market}"}

    table = table_map[market]

    # 从 Quant 平台读取估值快照
    df = _fetch_quant_valuation(market)
    if df.empty:
        log.warning("%s 估值数据为空，跳过", market)
        return {"market": market, "symbols_with_data": 0, "rows_updated": 0}

    # symbol 归一 + 提取可写字段
    df = df.copy()
    df["symbol"] = df["symbol"].astype(str).map(
        lambda s: _normalize_symbol(s, market)
    )
    # 取每个 symbol 最新一条快照
    df = df.sort_values("trade_date", na_position="first").groupby("symbol").tail(1)

    records = []
    for _, row in df.iterrows():
        rec = {"symbol": row["symbol"]}
        for col in ("pe_ttm", "pb", "total_mv", "float_mv"):
            if col in df.columns:
                rec[col] = row[col]
        # roe 由 Quant 字段派生：净利润(TTM) / 股东权益 × 100
        if {"net_profit_ttm", "equity"} <= set(df.columns):
            np_ttm = row.get("net_profit_ttm")
            equity = row.get("equity")
            if pd.notna(np_ttm) and pd.notna(equity) and float(equity) > 0:
                rec["roe"] = float(np_ttm) / float(equity) * 100
        records.append(rec)

    log.info(
        "%s 估值快照: %d 行 → %d 个标的有数据",
        market, len(df), len(records),
    )

    if dry_run:
        return {"market": market, "symbols_with_data": len(records), "dry_run": True}

    updated = _update_db(_get_engine(), table, records, market)
    log.info("Updated %d rows in %s", updated, table)

    return {
        "market": market,
        "symbols_with_data": len(records),
        "rows_updated": updated,
    }


def main():
    parser = argparse.ArgumentParser(description="Sync market fundamentals from Quant platforms")
    parser.add_argument("--market", default="ALL", choices=["HK", "US", "ALL"])
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.market == "ALL":
        for m in ["HK", "US"]:
            result = sync_market_fundamentals(m, args.dry_run)
            log.info(f"{m} result: {result}")
    else:
        result = sync_market_fundamentals(args.market, args.dry_run)
        log.info(f"Result: {result}")


if __name__ == "__main__":
    main()
