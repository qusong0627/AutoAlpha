#!/usr/bin/env python3
"""akshare vs 雅虎 字段对比统计。

对港股/美股的分红、估值、财务等数据段，抽样对比 akshare 与雅虎的数值，
统计一致率与覆盖情况，辅助决策以 akshare 为准后的数据整理。

用法:
  python backend/scripts/compare_akshare_yahoo.py --market HK --field valuation --symbols 0700.HK,0005.HK
  python backend/scripts/compare_akshare_yahoo.py --market HK --field dividend --symbols 0700.HK
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("compare_akshare_yahoo")

MARKET_DIRS = {
    "HK": "/data/quanthk",
    "US": "/data/quantus",
}


def _yahoo_valuation(market: str, symbol: str) -> dict:
    """从雅虎估值分区读某标的估值。"""
    import duckdb

    root = Path(MARKET_DIRS[market]) / "5_technical_derived/valuation"
    if not root.is_dir():
        return {}
    try:
        con = duckdb.connect(":memory:")
        glob = str(root / "**" / "*.parquet")
        con.execute(f"CREATE VIEW v AS SELECT * FROM read_parquet('{glob}', hive_partitioning=1)")
        df = con.execute(f"SELECT * FROM v WHERE symbol='{symbol}' ORDER BY dt DESC LIMIT 1").df()
        con.close()
        if df.empty:
            return {}
        row = df.iloc[0]
        return {c: row[c] for c in ["pe_ttm", "pb", "total_mv", "close"] if c in row.index}
    except Exception:
        return {}


def _akshare_financial(symbol: str) -> dict:
    """从 akshare 港股财务指标接口读 PE/PB/市值。"""
    import akshare as ak

    code = symbol.replace(".HK", "")
    # akshare 需要 5 位代码（00700），0700.HK -> 00700
    code = code.zfill(5) if code.isdigit() else code
    try:
        df = ak.stock_hk_financial_indicator_em(symbol=code)
        if df is None or df.empty:
            return {}
        row = df.iloc[0]
        out = {}
        for col in df.columns:
            if "市盈率" in col:
                out["pe_ttm"] = row[col]
            elif "市净率" in col:
                out["pb"] = row[col]
            elif "总市值" in col:
                out["total_mv"] = row[col]
        return out
    except Exception as exc:  # noqa: BLE001
        log.debug("%s akshare 财务失败: %s", symbol, exc)
        return {}


def _yahoo_dividend(market: str, symbol: str) -> pd.DataFrame:
    """从雅虎/akshare 分红文件读。"""
    root = Path(MARKET_DIRS[market]) / "3_financial_data/dividend"
    f = root / f"{symbol}.parquet"
    if f.exists():
        return pd.read_parquet(f)
    return pd.DataFrame()


def compare_valuation(market: str, symbols: list[str]) -> None:
    """对比估值字段。"""
    print(f"=== {market} 估值对比 (akshare vs 雅虎) ===")
    for sym in symbols:
        yh = _yahoo_valuation(market, sym)
        ak = _akshare_financial(sym)
        print(f"\n{sym}:")
        print(f"  雅虎: pe_ttm={yh.get('pe_ttm')}, pb={yh.get('pb')}, total_mv={yh.get('total_mv')}")
        print(f"  akshare: pe_ttm={ak.get('pe_ttm')}, pb={ak.get('pb')}, total_mv={ak.get('total_mv')}")
        # 一致率（都有值才算可比）
        for k in ["pe_ttm", "pb", "total_mv"]:
            yv = yh.get(k)
            av = ak.get(k)
            if yv is not None and av is not None:
                try:
                    yf = float(yv)
                    af = float(av)
                    ratio = abs(yf / af - 1) if af else 999
                    tag = "一致" if ratio < 0.05 else f"差异 {ratio*100:.1f}%"
                except (TypeError, ValueError, ZeroDivisionError):
                    tag = "不可比"
                print(f"    {k}: 雅虎={yv}, akshare={av} -> {tag}")
            else:
                missing = "雅虎空" if yv is None else "akshare空"
                print(f"    {k}: {missing}")


def compare_dividend(market: str, symbols: list[str]) -> None:
    """对比分红字段（akshare 结构化 vs 雅虎金额序列）。"""
    print(f"=== {market} 分红对比 ===")
    for sym in symbols:
        df = _yahoo_dividend(market, sym)
        print(f"\n{sym}: 分红文件 {len(df)} 行, source={df['source'].unique() if len(df) and 'source' in df.columns else '无'}")
        if len(df):
            print(f"  列: {list(df.columns)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="akshare vs 雅虎对比")
    parser.add_argument("--market", required=True, choices=["HK", "US"])
    parser.add_argument("--field", required=True, choices=["valuation", "dividend", "financial"])
    parser.add_argument("--symbols", required=True, help="逗号分隔标的")
    args = parser.parse_args()

    syms = [s.strip() for s in args.symbols.split(",") if s.strip()]
    if args.field in ("valuation", "financial"):
        compare_valuation(args.market, syms)
    elif args.field == "dividend":
        compare_dividend(args.market, syms)
    return 0


if __name__ == "__main__":
    sys.exit(main())
