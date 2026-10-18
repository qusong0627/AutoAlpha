#!/usr/bin/env python3
"""
校验 stock_daily_latest 复权口径，并模拟推理中心「个股推理」的基准价计算。
用于排查「推理中心个股推理价格异常」：K 线接口用 _to_nominal_price(close, adj_factor)
还原为真实不复权价，而 predict_single_stock 若直接用 sdl.close（前复权价）当基准价，
会对近期除权/送转过的股票产生与行情不一致的异常价格。

本脚本校验两件事：
1. 表内复权口径是否一致（是否存在「前复权行」与「不复权行」混用导致的跳变）。
2. 修复后 current_price（close/adj_factor）与原始 close 差异明显的候选股票，
   即「修复前基准价异常」的股票清单，便于核对是否已真正修复。

用法:
    # 默认：汇总诊断 + 最新行前复权候选（按 adj_factor 从小到大取 top）
    python backend/scripts/check_sdl_inference_price.py

    # 只看某只股票（前缀式或数字码均可，内部归一化到 6 位数字码）
    python backend/scripts/check_sdl_inference_price.py --symbol SZ300750

    # 指定候选数量 / 表格名（默认 stock_daily_latest，可传 _hk/_us 等）
    python backend/scripts/check_sdl_inference_price.py --limit 30 --table stock_daily_latest_us

可选参数:
    --table    要校验的表名（默认 stock_daily_latest）
    --symbol   只校准指定股票
    --limit    复权候选最多输出条数（默认 20）
    --jump     相邻日 adj_factor 跳变阈值（默认 0.1，即口径切换嫌疑）
"""

from __future__ import annotations

import argparse
import os
import re
import sys

import pandas as pd
from sqlalchemy import create_engine, text

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)


def _get_engine():
    """Create SQLAlchemy engine from environment (same convention as fix_sdl_data)."""
    db_url = os.getenv(
        "DATABASE_URL",
        f"postgresql://{os.getenv('DB_USER', 'quantmind')}:{os.getenv('DB_PASSWORD', 'quantmind2026')}"
        f"@{os.getenv('DB_HOST', 'db')}:{os.getenv('DB_PORT', '5432')}/{os.getenv('DB_NAME', 'quantmind')}",
    )
    if "+asyncpg" in db_url:
        db_url = db_url.replace("+asyncpg", "+psycopg2")
    if not db_url.startswith("postgresql"):
        db_url = f"postgresql+psycopg2://{os.getenv('DB_USER', 'quantmind')}:{os.getenv('DB_PASSWORD', 'quantmind2026')}@{os.getenv('DB_HOST', 'db')}:5432/quantmind"
    return create_engine(db_url, pool_pre_ping=True, future=True)


def _norm_to_digits(raw: str) -> str:
    """归一化股票代码为 6 位数字码，前缀式(如 SH600036)或后缀式(如 600036.SH)均可。"""
    m = re.search(r"(\d{6})", raw or "")
    return m.group(1) if m else (raw or "").strip()


def _load_frame(engine, table: str) -> pd.DataFrame:
    """加载表内核心列（symbol/trade_date/close/adj_factor/volume），suppress 警告。"""
    sql = f"""
        SELECT symbol, trade_date, close, adj_factor, volume
        FROM {table}
        WHERE volume > 0
    """
    with engine.connect() as conn:
        return pd.read_sql(text(sql), conn)


def _fmt(v) -> str:
    return "NULL" if pd.isna(v) else f"{float(v):.4f}"


def diagnose(engine, table: str, symbol: str | None, limit: int, jump_th: float):
    df = _load_frame(engine, table)

    if df.empty:
        print(f"[{table}] 无有效数据 (volume>0)。")
        return

    if symbol:
        digits = _norm_to_digits(symbol)
        df = df[df["symbol"].astype(str).str.contains(digits, regex=False)]
        if df.empty:
            print(f"[{table}] 未找到符号包含 {digits} 的记录。")
            return

    df["adj_factor"] = pd.to_numeric(df["adj_factor"], errors="coerce").fillna(1.0)
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df["trade_date"] = df["trade_date"].astype(str)
    df = df[df["close"].notna()].copy()

    total = len(df)
    adj_1 = int((df["adj_factor"] == 1.0).sum())
    adj_not1 = int((df["adj_factor"] != 1.0).sum())
    print("=" * 72)
    print(f"  {table} 复权口径诊断 (总行数: {total:,})")
    print("=" * 72)
    print(f"  adj_factor = 1.0   : {adj_1:,} 行 ({adj_1/total*100:.1f}%)")
    print(f"  adj_factor != 1.0  : {adj_not1:,} 行 ({adj_not1/total*100:.1f}%)")
    print(f"  adj_factor 范围    : [{df['adj_factor'].min():.6f}, {df['adj_factor'].max():.6f}]")

    # ---- 口径切换（复权前后跳变）嫌疑 ----
    if symbol is None:
        g = df.sort_values(["symbol", "trade_date"]).copy()
        g["prev"] = g.groupby("symbol")["adj_factor"].shift(1)
        g["adj_jump"] = (g["adj_factor"] - g["prev"]).abs()
        jumps = g[g["adj_jump"] > jump_th]
        jump_symbols = jumps["symbol"].nunique()
        print(f"\n⚠️  adj_factor 跳变(>{jump_th})记录数      : {len(jumps):,} (涉及 {jump_symbols:,} 只)")

    # ---- 每只股票最新行 + 历史口径概览 ----
    latest = df.sort_values(["symbol", "trade_date"]).groupby("symbol", as_index=False).tail(1)
    stats = (
        df.groupby("symbol")["adj_factor"]
        .agg(min_adj="min", max_adj="max")
        .reset_index()
    )
    latest = latest.merge(stats, on="symbol", how="left")
    latest["nominal_close"] = (latest["close"] / latest["adj_factor"]).round(2)
    latest["price_gap_pct"] = (
        (latest["nominal_close"] - latest["close"]).abs() / latest["close"] * 100
    )

    # A. 修复后仍会异常：最新行 adj_factor 明显 <1（前复权价未还原则基准价偏低/偏离行情）
    recent_adj = latest[latest["adj_factor"] <= 0.95]
    print(f"\n📌 最新行 adj_factor<=0.95 的股票（修复前基准价=前复权价，异常候选）: {len(recent_adj)}")

    # B. 口径混用嫌疑：同一只股票 max_adj≈1 且 min_adj<1，且 gap 明显
    mixed = latest[(latest["max_adj"] > 0.999) & (latest["min_adj"] < 0.95)]
    print(f"📌 同股历史在 前复权(<0.95) 与 不复权(≈1.0) 双口径并存（口径混用嫌疑，需 fix）: {len(mixed)}")

    # 候选 = A ∪ B，按价格偏离幅度排序输出
    cand = pd.concat([recent_adj, mixed]).drop_duplicates("symbol")
    cand = cand.sort_values("price_gap_pct", ascending=False).head(limit)

    if not cand.empty:
        print("\n  候选清单 (symbol / 最新日 / close(前复权) / adj / 已还原nominal / 偏离%):")
        for _, r in cand.iterrows():
            print(
                f"    {str(r['symbol']):<12} {str(r['trade_date']):<12} "
                f"close={_fmt(r['close']):>10}  adj={_fmt(r['adj_factor']):>8}  "
                f"nominal={float(r['nominal_close']):>10.2f}  偏离={float(r['price_gap_pct']):>6.2f}% "
                f"  (min_adj={_fmt(r['min_adj'])}, max_adj={_fmt(r['max_adj'])})"
            )
    else:
        print("\n  ✅ 未发现前复权口径明显的候选，口径一致或已按不复权(adj=1)存储。")

    print("\n结论口径说明:")
    print("  - 修复后 current_price = close / adj_factor（真实不复权价），与 K 线一致。")
    print("  - 若同一股票出现'历史前复权 + 最新不复权(adj=1)'混用，请先运行:")
    print("      python backend/scripts/fix_sdl_data.py --fix-adjust")


def main():
    p = argparse.ArgumentParser(description="校验 stock_daily_latest 复权口径与推理中心基准价")
    p.add_argument("--table", default="stock_daily_latest", help="表名，默认 stock_daily_latest")
    p.add_argument("--symbol", default=None, help="只校准指定股票(前缀式/数字码)")
    p.add_argument("--limit", type=int, default=20, help="候选最多输出条数")
    p.add_argument("--jump", type=float, default=0.1, help="adj_factor 跳变阈值(口径切换嫌疑)")
    args = p.parse_args()

    engine = _get_engine()
    try:
        diagnose(engine, args.table, args.symbol, args.limit, args.jump)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
