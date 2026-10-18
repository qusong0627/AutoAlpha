#!/usr/bin/env python3
"""akshare 港股基本面全量抓取 → QuantHK。

以 akshare 为准，抓取港股估值、财务指标、公司资料等基本面数据，落盘
quanthk 本地 parquet，替换雅虎空/弱数据。

数据段:
  valuation  估值（PE/PB/PS/PCF + 排名）→ 2_base_sector/akshare_valuation/{symbol}.parquet
  financial  财务指标（ROE/市值/股息率等21项）→ 2_base_sector/akshare_financial/{symbol}.parquet
  profile    公司资料 → 2_base_sector/akshare_profile/{symbol}.parquet
  dividend   分红 → 3_financial_data/dividend/{symbol}.parquet（akshare 为准）

用法:
  python backend/scripts/akshare_hk_fundamental.py --field valuation
  python backend/scripts/akshare_hk_fundamental.py --field financial --symbols 00700
  python backend/scripts/akshare_hk_fundamental.py --field all
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("akshare_hk_fundamental")

QUANTHK_DATA_DIR = Path(os.getenv("QM_QUANTHK_DATA_DIR", str(PROJECT_ROOT / "data" / "quanthk")))
DEFAULT_THREADS = 4

# 字段 → 落盘目录
FIELD_DIRS = {
    "valuation": "2_base_sector/akshare_valuation",
    "financial": "2_base_sector/akshare_financial",
    "profile": "2_base_sector/akshare_profile",
    "dividend": "3_financial_data/dividend",
}


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


def _to_symbol(code: str) -> str:
    """5位港股代码 → 4位+.HK。00700 → 0700.HK。"""
    code = code.strip().zfill(5)
    if code.startswith("0") and len(code) == 5:
        code = code[1:]
    return f"{code}.HK"


def _stock_list() -> list[str]:
    for csv_path in (Path(__file__).parent / "hk.csv", Path("/data/hk.csv"), Path("/app/backend/scripts/hk.csv")):
        if csv_path.is_file():
            try:
                df = pd.read_csv(csv_path, encoding="utf-8-sig")
                if "id" in df.columns:
                    return df["id"].astype(str).str.zfill(5).tolist()
            except Exception:
                pass
    return ["00700", "00005", "00939", "00941", "00998", "01299", "02318", "03888", "01810", "09988"]


def _code5(symbol: str) -> str:
    """0700.HK → 00700（akshare 用5位）。"""
    return symbol.replace(".HK", "").zfill(5)


def _fetch_valuation(symbol: str) -> pd.DataFrame | None:
    """估值对比（PE/PB/PS/PCF + 排名）。"""
    import akshare as ak

    try:
        df = ak.stock_hk_valuation_comparison_em(symbol=_code5(symbol))
        if df is None or df.empty:
            return None
        df["symbol"] = _to_symbol(symbol)
        df["source"] = "akshare"
        df["published_at"] = datetime.now().isoformat(timespec="seconds")
        return df
    except Exception as exc:  # noqa: BLE001
        log.debug("%s 估值失败: %s", symbol, exc)
        return None


def _fetch_financial(symbol: str) -> pd.DataFrame | None:
    """财务指标（PE/PB/市值/ROE等21项）。"""
    import akshare as ak

    try:
        df = ak.stock_hk_financial_indicator_em(symbol=_code5(symbol))
        if df is None or df.empty:
            return None
        df["symbol"] = _to_symbol(symbol)
        df["source"] = "akshare"
        df["published_at"] = datetime.now().isoformat(timespec="seconds")
        return df
    except Exception as exc:  # noqa: BLE001
        log.debug("%s 财务失败: %s", symbol, exc)
        return None


def _fetch_profile(symbol: str) -> pd.DataFrame | None:
    """公司资料。"""
    import akshare as ak

    try:
        df = ak.stock_hk_company_profile_em(symbol=_code5(symbol))
        if df is None or df.empty:
            return None
        df["symbol"] = _to_symbol(symbol)
        df["source"] = "akshare"
        return df
    except Exception as exc:  # noqa: BLE001
        log.debug("%s 公司资料失败: %s", symbol, exc)
        return None


def _fetch_dividend(symbol: str) -> pd.DataFrame | None:
    """分红（akshare 结构化）。"""
    import akshare as ak

    try:
        df = ak.stock_hk_dividend_payout_em(symbol=_code5(symbol))
        if df is None or df.empty:
            return None
        df = df.rename(columns={
            "最新公告日期": "announce_date", "财政年度": "fiscal_year", "分红方案": "plan",
            "分配类型": "dist_type", "除净日": "ex_date", "截至过户日": "book_close", "发放日": "pay_date",
        })
        df["symbol"] = _to_symbol(symbol)
        df["source"] = "akshare"
        return df
    except Exception as exc:  # noqa: BLE001
        log.debug("%s 分红失败: %s", symbol, exc)
        return None


FETCHERS = {
    "valuation": _fetch_valuation,
    "financial": _fetch_financial,
    "profile": _fetch_profile,
    "dividend": _fetch_dividend,
}


def sync(field: str, *, symbols: list[str] | None = None, concurrent: int = DEFAULT_THREADS, dry_run: bool = False) -> dict:
    """抓取指定字段。"""
    if field not in FETCHERS:
        raise ValueError(f"field 必须是 {'/'.join(FETCHERS)}，收到 {field}")

    syms = symbols or _stock_list()
    log.info("[%s] 待抓取 %d 只", field, len(syms))
    if dry_run:
        return {"field": field, "stocks": len(syms), "dry_run": True}

    fetch_fn = FETCHERS[field]
    root = _quanthk_root() / FIELD_DIRS[field]
    root.mkdir(parents=True, exist_ok=True)

    ok = 0
    err = 0
    with ThreadPoolExecutor(max_workers=concurrent) as pool:
        futures = {pool.submit(fetch_fn, s): s for s in syms}
        for future in as_completed(futures):
            symbol = futures[future]
            df = future.result()
            if df is not None and not df.empty:
                try:
                    out_symbol = _to_symbol(symbol)
                    df.to_parquet(root / f"{out_symbol}.parquet", index=False)
                    ok += 1
                except Exception as exc:  # noqa: BLE001
                    log.warning("写入 %s 失败: %s", symbol, exc)
                    err += 1
            else:
                err += 1

    return {"field": field, "stocks": len(syms), "ok": ok, "err": err, "dir": str(root)}


def main() -> int:
    parser = argparse.ArgumentParser(description="akshare 港股基本面 → QuantHK")
    parser.add_argument("--field", required=True, choices=["valuation", "financial", "profile", "dividend", "all"])
    parser.add_argument("--symbols", default=None, help="逗号分隔标的（默认全市场）")
    parser.add_argument("--concurrent", type=int, default=DEFAULT_THREADS)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    syms = [s.strip() for s in args.symbols.split(",") if s.strip()] if args.symbols else None
    fields = list(FETCHERS.keys()) if args.field == "all" else [args.field]
    try:
        for f in fields:
            result = sync(f, symbols=syms, concurrent=args.concurrent, dry_run=args.dry_run)
            print(result)
        return 0
    except Exception as exc:  # noqa: BLE001
        log.error("抓取失败: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
