#!/usr/bin/env python3
"""
QuantDB SDK 数据同步脚本
========================

将 QuantDB 付费数据源的数据同步到本地 PostgreSQL + Parquet。

支持同步：
  - A 股日线 K 线（前复权/后复权/不复权） → stock_daily_latest
  - 交易日历 → 写入 JSON 缓存
  - AI 因子（315 维 L1/L2 因子） → 特征 Parquet
  - 估值数据 → 特征 Parquet

用法：
  # 增量同步全市场日线
  python backend/scripts/sync_quantdb_data.py --mode kline --incremental

  # 同步指定股票
  python backend/scripts/sync_quantdb_data.py --mode kline --symbols 600519.SH,000001.SZ

  # 全量同步（从指定日期开始）
  python backend/scripts/sync_quantdb_data.py --mode kline --full --start-date 2024-01-01

  # 同步 AI 因子
  python backend/scripts/sync_quantdb_data.py --mode ai-factors --symbols 600519.SH

  # 同步交易日历
  python backend/scripts/sync_quantdb_data.py --mode calendar
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("quantdb_sync")

# ---------------------------------------------------------------------------
# DB config
# ---------------------------------------------------------------------------
DB_HOST = os.getenv("DB_HOST", os.getenv("DB_MASTER_HOST", "127.0.0.1"))
DB_PORT = int(os.getenv("DB_PORT", os.getenv("DB_MASTER_PORT", "5432")))
DB_NAME = os.getenv("DB_NAME", "quantmind")
DB_USER = os.getenv("DB_USER", "quantmind")
DB_PASS = os.getenv("DB_PASSWORD", "quantmind")

FEATURE_PARQUET_DIR = Path(os.getenv("QM_FEATURE_DIR", str(PROJECT_ROOT / "db" / "feature_snapshots")))


def _get_engine():
    from sqlalchemy import create_engine
    db_url = os.getenv("DATABASE_URL", "").strip()
    if not db_url:
        from urllib.parse import quote_plus as _q
        db_url = f"postgresql+psycopg2://{DB_USER}:{_q(DB_PASS)}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    elif "asyncpg" in db_url:
        db_url = db_url.replace("asyncpg", "psycopg2")
    elif db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+psycopg2://", 1)
    return create_engine(db_url, pool_pre_ping=True)


# ---------------------------------------------------------------------------
# QuantDB SDK client
# ---------------------------------------------------------------------------
def _get_qdb_client():
    from backend.shared.runtime_secrets import get_secret
    from quantdb_sdk import QuantDBClient
    api_key = get_secret("QUANTDB_API_KEY")
    if not api_key:
        raise RuntimeError("QUANTDB_API_KEY 未配置")
    client = QuantDBClient(api_key=api_key)
    return client


def _to_internal(symbol: str) -> str:
    """600036.SH -> SH600036 (内部 PG 格式)"""
    s = symbol.strip().upper()
    if "." in s:
        code, ex = s.split(".", 1)
        return f"{ex}{code}"
    return s


def _to_qdb(symbol: str) -> str:
    """SH600036 -> 600036.SH (QuantDB 格式)

    支持输入: 600036.SH, SH600036, 600036, BJ873169
    纯数字自动识别: 6/9→SH, 0/3/2→SZ, 4/8→BJ
    """
    s = symbol.strip().upper()
    if "." in s:
        return s
    if s.startswith("SH") or s.startswith("SZ") or s.startswith("BJ"):
        return f"{s[2:]}.{s[:2]}"
    if s.isdigit():
        if s.startswith("6") or s.startswith("9"):
            return f"{s}.SH"
        if s.startswith("0") or s.startswith("3") or s.startswith("2"):
            return f"{s}.SZ"
        if s.startswith("4") or s.startswith("8"):
            return f"{s}.BJ"
    return s


# ---------------------------------------------------------------------------
# PG helpers
# ---------------------------------------------------------------------------
def _get_pg_latest_dates(engine) -> dict[str, date]:
    from sqlalchemy import text as sql_text
    with engine.begin() as conn:
        rows = conn.execute(
            sql_text("SELECT symbol, MAX(trade_date) AS max_dt FROM stock_daily_latest GROUP BY symbol")
        ).fetchall()
    return {r[0]: r[1] for r in rows}


def _get_pg_symbols(engine) -> list[str]:
    from sqlalchemy import text as sql_text
    with engine.begin() as conn:
        rows = conn.execute(
            sql_text("SELECT DISTINCT symbol FROM stock_daily_latest ORDER BY symbol")
        ).fetchall()
    return [r[0] for r in rows]


def _upsert_kline_to_pg(engine, df: pd.DataFrame) -> int:
    """将 K 线 DataFrame 写入 stock_daily_latest（UPSERT）。"""
    from sqlalchemy import text as sql_text

    if df is None or df.empty:
        return 0

    df = df.copy()
    df = df.drop_duplicates(subset=["trade_date", "symbol"])

    core_cols = ["trade_date", "symbol", "open", "high", "low", "close",
                 "volume", "amount", "adj_factor"]
    use_cols = [c for c in core_cols if c in df.columns]
    data = df[use_cols].fillna(0)
    data = data.replace([float("inf"), float("-inf")], 0)

    records = [tuple(row) for row in data.itertuples(index=False, name=None)]
    if not records:
        return 0

    non_pk = [c for c in use_cols if c not in ("trade_date", "symbol")]

    with engine.begin() as conn:
        for rec in records:
            rec_dict = dict(zip(use_cols, rec))
            placeholders = ", ".join([f":{c}" for c in use_cols])
            cols = ", ".join(use_cols)
            if non_pk:
                update_set = ", ".join([f"{c}=EXCLUDED.{c}" for c in non_pk])
                sql = (
                    f"INSERT INTO stock_daily_latest ({cols}) VALUES ({placeholders}) "
                    f"ON CONFLICT (trade_date, symbol) DO UPDATE SET {update_set}"
                )
            else:
                sql = (
                    f"INSERT INTO stock_daily_latest ({cols}) VALUES ({placeholders}) "
                    "ON CONFLICT (trade_date, symbol) DO NOTHING"
                )
            conn.execute(sql_text(sql), rec_dict)

    return len(records)


# ---------------------------------------------------------------------------
# K-line sync
# ---------------------------------------------------------------------------
def _fetch_kline_batch(
    client,
    symbols: list[str],
    start_date: date,
    end_date: date,
    adj_type: str = "forward",
    batch_size: int = 10,
) -> pd.DataFrame:
    """批量获取 K 线数据，返回统一格式 DataFrame。"""
    all_frames = []
    total = len(symbols)

    for i in range(0, total, batch_size):
        batch = symbols[i:i + batch_size]
        for sym in batch:
            qdb_sym = _to_qdb(sym)
            try:
                df = client.query_kline(
                    qdb_sym,
                    adj_type=adj_type,
                    start_date=start_date.isoformat(),
                    end_date=end_date.isoformat(),
                )
                if df is not None and not df.empty:
                    df = _normalize_kline(df, sym)
                    all_frames.append(df)
            except Exception as exc:
                log.warning("query_kline failed %s: %s", qdb_sym, exc)

        if (i + batch_size) % 50 == 0 or i + batch_size >= total:
            log.info("K线进度: %d/%d symbols", min(i + batch_size, total), total)

        if i + batch_size < total:
            time.sleep(0.2)

    if not all_frames:
        return pd.DataFrame()
    return pd.concat(all_frames, ignore_index=True)


def _normalize_kline(df: pd.DataFrame, internal_symbol: str) -> pd.DataFrame:
    """将 QuantDB K 线 DataFrame 归一化为 PG 写入格式。"""
    df = df.copy()

    # 日期列
    for col in ("trade_date", "date", "datetime"):
        if col in df.columns:
            df["trade_date"] = pd.to_datetime(df[col]).dt.date
            break
    if "trade_date" not in df.columns:
        if isinstance(df.index, pd.DatetimeIndex):
            df["trade_date"] = df.index.date
        else:
            df["trade_date"] = pd.to_datetime(df.index).dt.date

    # 重命名常见列
    col_map = {"vol": "volume", "turnover": "amount"}
    df.rename(columns={k: v for k, v in col_map.items() if k in df.columns}, inplace=True)

    # 确保核心列存在
    for c in ("open", "high", "low", "close", "volume", "amount"):
        if c not in df.columns:
            df[c] = 0.0
        else:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0)

    if "adj_factor" not in df.columns:
        df["adj_factor"] = 1.0

    df["symbol"] = internal_symbol
    df["source"] = "quantdb"

    return df[["symbol", "trade_date", "open", "high", "low", "close",
               "volume", "amount", "adj_factor", "source"]]


# ---------------------------------------------------------------------------
# Calendar sync
# ---------------------------------------------------------------------------
def sync_calendar(client, start_date: Optional[str] = None, end_date: Optional[str] = None) -> pd.DataFrame:
    """同步交易日历到本地 JSON 缓存。"""
    log.info("同步交易日历...")
    df = client.query_calendar(start_date=start_date, end_date=end_date)
    if df is None or df.empty:
        log.warning("交易日历为空")
        return df

    cache_dir = FEATURE_PARQUET_DIR / "quantdb_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / "trading_calendar.parquet"
    df.to_parquet(cache_path, index=False)
    log.info("交易日历已缓存: %d 行 → %s", len(df), cache_path)
    return df


# ---------------------------------------------------------------------------
# AI factors sync
# ---------------------------------------------------------------------------
def sync_ai_factors(
    client,
    symbols: list[str],
    sub_category: str = "l1_l2_factors",
) -> dict:
    """同步 AI 因子到 Parquet。"""
    log.info("同步 AI 因子 (%d symbols, sub_category=%s)...", len(symbols), sub_category)
    results = {"synced": 0, "errors": []}
    factor_dir = FEATURE_PARQUET_DIR / "quantdb_factors"
    factor_dir.mkdir(parents=True, exist_ok=True)

    for i, sym in enumerate(symbols):
        qdb_sym = _to_qdb(sym)
        try:
            df = client.load_as_df(
                category_id="6", sub_category=sub_category, symbol=qdb_sym
            )
            if df is not None and not df.empty:
                out_path = factor_dir / f"{sym}_{sub_category}.parquet"
                df.to_parquet(out_path, index=False)
                results["synced"] += 1
                log.debug("AI factors: %s → %d rows", sym, len(df))
        except Exception as exc:
            results["errors"].append(f"{sym}: {exc}")
            log.warning("AI factors failed %s: %s", sym, exc)

        if (i + 1) % 10 == 0:
            log.info("AI 因子进度: %d/%d", i + 1, len(symbols))
            time.sleep(0.1)

    log.info("AI 因子同步完成: %d synced, %d errors", results["synced"], len(results["errors"]))
    return results


# ---------------------------------------------------------------------------
# Valuation sync
# ---------------------------------------------------------------------------
def sync_valuation(client, symbols: list[str]) -> dict:
    """同步估值数据到 Parquet。"""
    log.info("同步估值数据 (%d symbols)...", len(symbols))
    results = {"synced": 0, "errors": []}
    val_dir = FEATURE_PARQUET_DIR / "quantdb_valuation"
    val_dir.mkdir(parents=True, exist_ok=True)

    for i, sym in enumerate(symbols):
        qdb_sym = _to_qdb(sym)
        try:
            df = client.load_as_df(
                category_id="4", sub_category="valuation", symbol=qdb_sym
            )
            if df is not None and not df.empty:
                out_path = val_dir / f"{sym}_valuation.parquet"
                df.to_parquet(out_path, index=False)
                results["synced"] += 1
        except Exception as exc:
            results["errors"].append(f"{sym}: {exc}")
            log.warning("valuation failed %s: %s", sym, exc)

        if (i + 1) % 10 == 0:
            log.info("估值进度: %d/%d", i + 1, len(symbols))
            time.sleep(0.1)

    log.info("估值同步完成: %d synced, %d errors", results["synced"], len(results["errors"]))
    return results


# ---------------------------------------------------------------------------
# Main: K-line sync
# ---------------------------------------------------------------------------
def run_kline_sync(
    symbols: Optional[list[str]] = None,
    incremental: bool = True,
    start_date: Optional[date] = None,
    end_date: Optional[date] = None,
    adj_type: str = "qfq",
    batch_size: int = 10,
) -> dict:
    """执行 K 线数据同步: QuantDB → stock_daily_latest。"""
    result = {
        "mode": "incremental" if incremental else "full",
        "started": datetime.now().isoformat(),
        "symbols_total": 0,
        "rows_synced": 0,
        "errors": [],
    }

    client = _get_qdb_client()
    engine = _get_engine()

    # 确定股票列表
    if symbols:
        target_symbols = [_to_internal(s) for s in symbols]
    else:
        try:
            stock_df = client.query_stock_list(limit=10000)
            if stock_df is not None and not stock_df.empty:
                sym_col = next(
                    (c for c in ("symbol", "code", "ts_code") if c in stock_df.columns),
                    stock_df.columns[0],
                )
                target_symbols = [_to_internal(s) for s in stock_df[sym_col].unique()]
            else:
                target_symbols = _get_pg_symbols(engine)
        except Exception:
            target_symbols = _get_pg_symbols(engine)

    result["symbols_total"] = len(target_symbols)
    log.info("目标股票: %d 只", len(target_symbols))

    if not target_symbols:
        log.warning("无股票可同步")
        return result

    # 确定日期范围
    today = date.today()
    if end_date is None:
        end_date = today

    if incremental and start_date is None:
        pg_latest = _get_pg_latest_dates(engine)
        log.info("PG 已有 %d 只股票的最新日期", len(pg_latest))
    else:
        pg_latest = {}

    adj_map = {"qfq": "forward", "hfq": "backward", "none": "unadjusted"}
    sdk_adj = adj_map.get(adj_type, "forward")

    # 分批同步
    total_rows = 0
    batch_symbols = []
    need_start_map = {}

    for sym in target_symbols:
        if incremental and start_date is None:
            pg_max = pg_latest.get(sym)
            if pg_max is not None and pg_max >= today - timedelta(days=1):
                continue
            sym_start = (pg_max + timedelta(days=1)) if pg_max else (start_date or date(2024, 1, 1))
        else:
            sym_start = start_date or date(2024, 1, 1)

        if sym_start > end_date:
            continue

        need_start_map[sym] = sym_start
        batch_symbols.append(sym)

    if not batch_symbols:
        log.info("所有股票已最新，无需同步")
        return result

    log.info("需同步 %d 只股票", len(batch_symbols))

    # 按日期分组批量下载
    for i in range(0, len(batch_symbols), batch_size):
        sub = batch_symbols[i:i + batch_size]
        # 用每只股票各自的起始日期
        frames = []
        for sym in sub:
            qdb_sym = _to_qdb(sym)
            sym_start = need_start_map[sym]
            try:
                df = client.query_kline(
                    qdb_sym,
                    adj_type=sdk_adj,
                    start_date=sym_start.isoformat(),
                    end_date=end_date.isoformat(),
                )
                if df is not None and not df.empty:
                    frames.append(_normalize_kline(df, sym))
            except Exception as exc:
                result["errors"].append(f"{sym}: {exc}")
                log.warning("query_kline failed %s: %s", qdb_sym, exc)

        if frames:
            combined = pd.concat(frames, ignore_index=True)
            rows = _upsert_kline_to_pg(engine, combined)
            total_rows += rows
            log.info("Batch %d-%d: %d rows → PG", i + 1, min(i + batch_size, len(batch_symbols)), rows)

        if (i + batch_size) % 50 == 0:
            log.info("K线总进度: %d/%d, 累计 %d rows", min(i + batch_size, len(batch_symbols)), len(batch_symbols), total_rows)

        if i + batch_size < len(batch_symbols):
            time.sleep(0.3)

    result["rows_synced"] = total_rows
    result["finished"] = datetime.now().isoformat()
    log.info("K线同步完成: %d rows 写入 stock_daily_latest", total_rows)
    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="QuantDB SDK 数据同步")
    parser.add_argument("--mode", choices=["kline", "calendar", "ai-factors", "valuation", "all"],
                        default="kline", help="同步模式")
    parser.add_argument("--symbols", type=str, help="股票列表，逗号分隔 (如 600519.SH,000001.SZ)")
    parser.add_argument("--incremental", action="store_true", default=True, help="增量同步（默认）")
    parser.add_argument("--full", action="store_true", help="全量同步")
    parser.add_argument("--start-date", type=str, help="起始日期 YYYY-MM-DD")
    parser.add_argument("--end-date", type=str, help="结束日期 YYYY-MM-DD")
    parser.add_argument("--adj-type", choices=["qfq", "hfq", "none"], default="qfq", help="复权类型")
    parser.add_argument("--batch-size", type=int, default=10, help="批量大小")
    args = parser.parse_args()

    incremental = not args.full
    symbols = args.symbols.split(",") if args.symbols else None
    start_date = date.fromisoformat(args.start_date) if args.start_date else None
    end_date = date.fromisoformat(args.end_date) if args.end_date else None

    if args.mode in ("kline", "all"):
        run_kline_sync(
            symbols=symbols,
            incremental=incremental,
            start_date=start_date,
            end_date=end_date,
            adj_type=args.adj_type,
            batch_size=args.batch_size,
        )

    if args.mode in ("calendar", "all"):
        client = _get_qdb_client()
        sync_calendar(
            client,
            start_date=args.start_date,
            end_date=args.end_date,
        )

    if args.mode in ("ai-factors", "all"):
        client = _get_qdb_client()
        if symbols is None:
            engine = _get_engine()
            symbols = _get_pg_symbols(engine)
        sync_ai_factors(client, [_to_internal(s) for s in symbols])

    if args.mode in ("valuation", "all"):
        client = _get_qdb_client()
        if symbols is None:
            engine = _get_engine()
            symbols = _get_pg_symbols(engine)
        sync_valuation(client, [_to_internal(s) for s in symbols])


if __name__ == "__main__":
    main()
