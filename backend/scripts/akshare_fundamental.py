#!/usr/bin/env python3
"""akshare 港美股其他维度数据 → QuantUS/QuantHK 同步脚本。

用 akshare 获取分红/财务/估值等维度，按 QuantDB 格式落盘本地 parquet。

数据段:
  dividend  分红（时间序列）→ 3_financial_data/dividend/{symbol}.parquet
  financial 财务/估值快照（PE/PB/市值等）→ 5_technical_derived/valuation/{symbol}.parquet

市场:
  HK → data/quanthk/（代码 00700 → 0700.HK）
  US → data/quantus/（代码原样 AAPL）

用法:
  python backend/scripts/akshare_fundamental.py --market HK --field dividend
  python backend/scripts/akshare_fundamental.py --market HK --field financial --symbol 00700
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
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
log = logging.getLogger("akshare_fundamental")

DEFAULT_THREADS = 4

MARKET_CONFIG = {
    "HK": {"env": "QM_QUANTHK_DATA_DIR", "default_dir": "/data/quanthk"},
    "US": {"env": "QM_QUANTUS_DATA_DIR", "default_dir": "/data/quantus"},
}


def _data_dir(market: str) -> Path:
    cfg = MARKET_CONFIG[market]
    env_val = os.getenv(cfg["env"], "").strip()
    if env_val:
        p = Path(env_val)
        p.mkdir(parents=True, exist_ok=True)
        return p
    p = Path(cfg["default_dir"])
    if p.is_dir():
        return p
    local = PROJECT_ROOT / "data" / ("quanthk" if market == "HK" else "quantus")
    local.mkdir(parents=True, exist_ok=True)
    return local


def _to_symbol(market: str, code: str) -> str:
    code = code.strip()
    if market == "HK":
        code = code.zfill(5)
        if code.startswith("0") and len(code) == 5:
            code = code[1:]
        return f"{code}.HK"
    return code.upper()


def _stock_list(market: str) -> list[str]:
    if market == "HK":
        for csv_path in (Path(__file__).parent / "hk.csv", Path("/data/hk.csv"), Path("/app/backend/scripts/hk.csv")):
            if csv_path.is_file():
                try:
                    df = pd.read_csv(csv_path, encoding="utf-8-sig")
                    if "id" in df.columns:
                        return df["id"].astype(str).str.zfill(5).tolist()
                except Exception:
                    pass
        return ["00001", "00005", "00700", "00939", "00941", "01299"]
    else:
        try:
            from backend.services.engine.rd_agent.data_pipeline.us_data import US_SYMBOLS

            if US_SYMBOLS:
                return list(US_SYMBOLS)
        except Exception:
            pass
        return ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA"]


def _fetch_dividend_hk(symbol: str) -> pd.DataFrame | None:
    """港股分红（东财接口，时间序列）。"""
    import akshare as ak

    try:
        df = ak.stock_hk_dividend_payout_em(symbol=symbol)
        if df is None or df.empty:
            return None
        # 列：最新公告日期/财政年度/分红方案/分配类型/除净日/截至过户日/发放日
        df = df.rename(columns={
            "最新公告日期": "announce_date",
            "财政年度": "fiscal_year",
            "分红方案": "plan",
            "分配类型": "dist_type",
            "除净日": "ex_date",
            "截至过户日": "book_close",
            "发放日": "pay_date",
        })
        df["symbol"] = _to_symbol("HK", symbol)
        df["source"] = "akshare"
        return df
    except Exception as exc:  # noqa: BLE001
        log.debug("分红 %s 失败: %s", symbol, exc)
        return None


def _fetch_financial_hk(symbol: str) -> pd.DataFrame | None:
    """港股财务/估值快照（东财接口，最新1行）。"""
    import akshare as ak

    try:
        df = ak.stock_hk_financial_indicator_em(symbol=symbol)
        if df is None or df.empty:
            return None
        df = df.copy()
        df["symbol"] = _to_symbol("HK", symbol)
        df["source"] = "akshare"
        df["published_at"] = datetime.now().isoformat(timespec="seconds")
        return df
    except Exception as exc:  # noqa: BLE001
        log.debug("财务 %s 失败: %s", symbol, exc)
        return None


def sync(
    market: str,
    field: str,
    *,
    symbols: list[str] | None = None,
    concurrent: int = DEFAULT_THREADS,
    dry_run: bool = False,
) -> dict:
    """同步 akshare 分红/财务数据。"""
    market = market.upper()
    if market not in MARKET_CONFIG:
        raise ValueError(f"market 必须是 HK/US，收到 {market}")

    syms = symbols or _stock_list(market)
    log.info("[%s] %s 待同步 %d 只", market, field, len(syms))
    if dry_run:
        return {"market": market, "field": field, "stocks": len(syms), "dry_run": True}

    if field == "dividend":
        fetch_fn = _fetch_dividend_hk
        rel_dir = "3_financial_data/dividend"
    elif field == "financial":
        fetch_fn = _fetch_financial_hk
        rel_dir = "5_technical_derived/valuation"
    else:
        raise ValueError(f"field 必须是 dividend/financial，收到 {field}")

    root = _data_dir(market) / rel_dir
    root.mkdir(parents=True, exist_ok=True)

    ok = 0
    err = 0
    with ThreadPoolExecutor(max_workers=concurrent) as pool:
        futures = {pool.submit(fetch_fn, s): s for s in syms}
        for future in as_completed(futures):
            df = future.result()
            if df is not None and not df.empty:
                symbol = df["symbol"].iloc[0]
                try:
                    df.to_parquet(root / f"{symbol}.parquet", index=False)
                    ok += 1
                except Exception as exc:  # noqa: BLE001
                    log.warning("写入 %s 失败: %s", symbol, exc)
                    err += 1
            else:
                err += 1

    return {"market": market, "field": field, "stocks": len(syms), "ok": ok, "err": err, "dir": str(root)}


def main() -> int:
    parser = argparse.ArgumentParser(description="akshare 港美股分红/财务 → QuantUS/QuantHK")
    parser.add_argument("--market", required=True, choices=["HK", "US"], help="市场")
    parser.add_argument("--field", required=True, choices=["dividend", "financial"], help="数据段")
    parser.add_argument("--symbol", default=None, help="指定股票代码，逗号分隔")
    parser.add_argument("--concurrent", type=int, default=DEFAULT_THREADS, help="并发数")
    parser.add_argument("--dry-run", action="store_true", help="仅预览")
    args = parser.parse_args()

    try:
        syms = [s.strip() for s in args.symbol.split(",") if s.strip()] if args.symbol else None
        result = sync(args.market, args.field, symbols=syms, concurrent=args.concurrent, dry_run=args.dry_run)
        print(result)
        return 0
    except Exception as exc:  # noqa: BLE001
        log.error("同步失败: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
