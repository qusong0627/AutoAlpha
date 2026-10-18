#!/usr/bin/env python3
"""QuantUS 扩展数据集 — 估值自算回填 / 标的池扩容。

数据集:
  5_technical_derived/valuation/dt=YYYYMMDD/data.parquet
      PE/PB/PS 由本地 income/balance(488只财报) + daily_forward 收盘 自算回填,
      替换 yahoo pe_ttm 全 NaN 的空快照。release_id='computed_local'。
  2_base_sector/us_universe/universe_YYYYMMDD.parquet
      按市值扩容标的池 (stock_us_spot_em), 输出新增代码清单供 kline/基本面回拉。

用法:
  python backend/scripts/quantus_extra_datasets.py --task valuation_backfill [--days-back 750]
  python backend/scripts/quantus_extra_datasets.py --task universe_expand [--min-mcap 5e9] [--top 1000]
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("quantus_extra")

QUANTUS_DATA_DIR = Path(os.getenv("QM_QUANTUS_DATA_DIR", str(PROJECT_ROOT / "data" / "quantus")))
TECH_DIR = QUANTUS_DATA_DIR / "5_technical_derived"
BASE_DIR = QUANTUS_DATA_DIR / "2_base_sector"

VAL_COLS = ["symbol", "time", "close", "total_capital", "circulating_capital", "total_mv", "float_mv",
            "net_profit_ttm", "revenue_ttm", "equity", "annual_net_profit", "pe_ttm", "pe_static",
            "pb", "ps_ttm", "dividend_rate", "release_id", "published_at"]

_now_iso = pd.Timestamp.now().isoformat()


def _load_financial(kind: str) -> pd.DataFrame:
    d = QUANTUS_DATA_DIR / "3_financial_data" / kind
    frames = []
    for p in sorted(d.glob("*.parquet")):
        try:
            df = pd.read_parquet(p)
        except Exception:  # noqa: BLE001
            continue
        frames.append(df)
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    df["report_date"] = pd.to_datetime(df["report_date"], errors="coerce")
    return df.dropna(subset=["report_date"]).sort_values("report_date")


def _ttm(series: pd.Series, dates: pd.Series, day: pd.Timestamp, window_days: int = 400) -> float:
    """截至 day 的最近 4 期(≤window_days)报告求和; 不足 3 期(如仅年报)用最近一期值做代理。"""
    m = (dates <= day) & (dates >= day - pd.Timedelta(days=window_days))
    vals = series[m]
    dts = dates[m]
    if len(vals) == 0:
        return np.nan
    uniq = {}
    for d, v in zip(dts, vals):  # 保留最近一份
        uniq[d] = v
    last4 = sorted(uniq.items(), key=lambda kv: kv[0])[-4:]
    if len(last4) >= 3:
        return float(np.nansum([v for _, v in last4]))
    if len(last4) >= 1:  # 年报口径代理
        return float(last4[-1][1])
    return np.nan


def task_valuation_backfill(days_back: int = 750) -> dict:
    income = _load_financial("income")
    balance = _load_financial("balance")
    log.info("income %d 行 / balance %d 行", len(income), len(balance))

    inc_col = next((c for c in income.columns if c.startswith("Net Income From Continuing Operation")),
                   None)
    rev_col = next((c for c in income.columns if c in ("Total Revenue", "Reconciled Cost of Revenue") or c == "Total Revenue"),
                   None) or next((c for c in income.columns if "Revenue" in c), None)
    eq_col = next((c for c in balance.columns if c == "Stockholders Equity"), None) \
        or next((c for c in balance.columns if "Common Stock Equity" in c), None)
    sh_col = next((c for c in balance.columns if c in ("Share Issued", "Ordinary Shares Number")), None)
    log.info("列选择: net_income=%s revenue=%s equity=%s shares=%s", inc_col, rev_col, eq_col, sh_col)

    fin_root = QUANTUS_DATA_DIR / "1_kline_data" / "daily_forward"
    import duckdb
    con = duckdb.connect()
    con.execute("SET threads=4")
    px = con.execute("""
        SELECT symbol, CAST(time AS DATE) AS d, close
        FROM read_parquet(?, hive_partitioning=false, filename=true)
        WHERE CAST(time AS DATE) >= current_date - INTERVAL (?) DAY
    """, [str(fin_root / "dt=*" / "data.parquet"), days_back]).fetchdf()
    con.close()
    px["d"] = pd.to_datetime(px["d"])
    log.info("价格行: %d (%s ~ %s)", len(px), px["d"].min().date(), px["d"].max().date())

    out_frames = []
    for sym, g in px.groupby("symbol"):
        gi = income[income["symbol"] == sym] if inc_col else pd.DataFrame()
        gb = balance[balance["symbol"] == sym]
        if len(g) == 0:
            continue
        dates_i = gi["report_date"].reset_index(drop=True)
        vals_i = pd.to_numeric(gi[inc_col], errors="coerce").reset_index(drop=True) if inc_col else pd.Series(dtype=float)
        dates_r = gi["report_date"].reset_index(drop=True)
        vals_r = pd.to_numeric(gi[rev_col], errors="coerce").reset_index(drop=True) if rev_col else pd.Series(dtype=float)
        dates_b = gb["report_date"].reset_index(drop=True)

        shares_series = pd.to_numeric(gb[sh_col], errors="coerce").reset_index(drop=True) if sh_col else pd.Series(dtype=float)
        eq_series = pd.to_numeric(gb[eq_col], errors="coerce").reset_index(drop=True) if eq_col else pd.Series(dtype=float)

        rows = []
        for _, r in g.iterrows():
            day = r["d"]
            shares = shares_series[dates_b <= day].iloc[-1] if len(shares_series) and (dates_b <= day).any() else np.nan
            equity = eq_series[dates_b <= day].iloc[-1] if len(eq_series) and (dates_b <= day).any() else np.nan
            np_ttm = _ttm(vals_i, dates_i, day) if len(vals_i) else np.nan
            rev_ttm = _ttm(vals_r, dates_r, day) if len(vals_r) else np.nan
            mv = r["close"] * shares if pd.notna(shares) else np.nan
            rows.append({
                "close": r["close"], "total_capital": shares, "total_mv": mv, "float_mv": np.nan,
                "net_profit_ttm": np_ttm, "revenue_ttm": rev_ttm, "equity": equity,
                "pe_ttm": mv / np_ttm if pd.notna(mv) and pd.notna(np_ttm) and np_ttm > 0 else np.nan,
                "ps_ttm": mv / rev_ttm if pd.notna(mv) and pd.notna(rev_ttm) and rev_ttm > 0 else np.nan,
                "pb": mv / equity if pd.notna(mv) and pd.notna(equity) and equity > 0 else np.nan,
            })
        df = pd.DataFrame(rows)
        df.insert(0, "symbol", sym)
        df["time"] = g["d"].values
        out_frames.append(df)
    if not out_frames:
        return {"task": "valuation_backfill", "error": "no data"}

    big = pd.concat(out_frames, ignore_index=True)
    big["total_capital"] = pd.to_numeric(big["total_capital"], errors="coerce")
    big["release_id"] = "computed_local"
    big["published_at"] = _now_iso
    n_parts = 0
    for d, g2 in big.groupby(big["time"].dt.strftime("%Y%m%d")):
        pdir = TECH_DIR / "valuation" / f"dt={d}"
        pdir.mkdir(parents=True, exist_ok=True)
        out = g2.copy()
        out["time"] = pd.Timestamp(d)
        out = out.reindex(columns=VAL_COLS)
        out.to_parquet(pdir / "data.parquet", index=False)
        n_parts += 1
    log.info("valuation 回填分区: %d, 行: %d", n_parts, len(big))
    return {"task": "valuation_backfill", "partitions": n_parts, "rows": len(big)}


def task_universe_expand(min_mcap: float = 5e9, top: int = 1000) -> dict:
    import akshare as ak
    import time
    df = None
    for attempt in range(5):
        try:
            df = ak.stock_us_spot_em()
            break
        except Exception as exc:  # noqa: BLE001
            log.warning("stock_us_spot_em 第%s次失败: %s", attempt + 1, str(exc)[:80])
            time.sleep(15 * (attempt + 1))
    if df is None or df.empty:
        raise RuntimeError("stock_us_spot_em 持续失败")
    log.info("stock_us_spot_em: %d 行, cols=%s", len(df), list(df.columns))
    code_col = next(c for c in df.columns if "代码" in c)
    name_col = next(c for c in df.columns if "名称" in c)
    mcap_col = next((c for c in df.columns if "总市值" in c), None)
    if mcap_col is None:
        raise RuntimeError(f"缺少总市值列: {list(df.columns)}")
    df["ticker"] = df[code_col].astype(str).str.split(".").str[-1].str.upper()
    df["mcap"] = pd.to_numeric(df[mcap_col], errors="coerce")
    uni = df.dropna(subset=["mcap"]).query("mcap >= @min_mcap").sort_values("mcap", ascending=False).head(top)
    uni = uni.rename(columns={name_col: "cn_name", mcap_col: "market_cap"})
    uni["price"] = pd.to_numeric(df.loc[uni.index, "最新价"], errors="coerce")
    uni = uni.assign(source="em_spot", updated_at=_now_iso)

    existing = set()
    import duckdb
    con = duckdb.connect()
    existing = set(con.execute(
        f"SELECT DISTINCT symbol FROM read_parquet('{QUANTUS_DATA_DIR / '1_kline_data' / 'daily_forward' / 'dt=20260827' / 'data.parquet'}')"
    ).fetchall())
    existing = {r[0] for r in existing}
    con.close()
    uni["is_new"] = ~uni["ticker"].isin(existing)

    out_dir = BASE_DIR / "us_universe"
    out_dir.mkdir(parents=True, exist_ok=True)
    day = date.today().strftime("%Y%m%d")
    uni[["ticker", "cn_name", "mcap", "price", "is_new", "source", "updated_at"]] \
        .to_parquet(out_dir / f"universe_{day}.parquet", index=False)
    new_tickers = sorted(uni.loc[uni["is_new"], "ticker"])
    (out_dir / f"new_symbols_{day}.txt").write_text(",".join(new_tickers))
    log.info("标的池: %d 只 (现有 %d, 新增 %d)", len(uni), len(existing), len(new_tickers))
    return {"task": "universe_expand", "universe": len(uni), "existing": len(existing), "new": len(new_tickers),
            "new_file": str(out_dir / f"new_symbols_{day}.txt")}


def main() -> int:
    ap = argparse.ArgumentParser(description="QuantUS 扩展数据集")
    ap.add_argument("--task", required=True, choices=["valuation_backfill", "universe_expand"])
    ap.add_argument("--days-back", type=int, default=750)
    ap.add_argument("--min-mcap", type=float, default=5e9)
    ap.add_argument("--top", type=int, default=1000)
    args = ap.parse_args()
    if args.task == "valuation_backfill":
        r = task_valuation_backfill(days_back=args.days_back)
    else:
        r = task_universe_expand(min_mcap=args.min_mcap, top=args.top)
    print(r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
