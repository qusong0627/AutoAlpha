#!/usr/bin/env python3
"""akshare 港美股指数日线 → QuantUS/QuantHK 同步脚本。

用 akshare 新浪接口抓取港股/美股主要指数日线，按 QuantDB 的 Hive 分区
格式落盘到 1_kline_data/index_daily。

数据源（新浪，实测稳定）:
  HK: stock_hk_index_daily_sina  → HSI/HSCEI/HSCCI
  US: index_us_stock_sina        → .IXIC/.INX/.DJI

落盘格式:
  {market_dir}/1_kline_data/index_daily/dt=YYYYMMDD/data.parquet
  schema 与 K线一致：symbol/time/open/high/low/close/volume/amount/release_id/published_at

symbol 命名:
  HK: HSI.HK / HSCEI.HK / HSCCI.HK
  US: IXIC.US / SPX.US / DJI.US

用法:
  # 港股指数
  python backend/scripts/akshare_index_sync.py --market HK

  # 美股指数
  python backend/scripts/akshare_index_sync.py --market US

  # 全部
  python backend/scripts/akshare_index_sync.py --market all
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
log = logging.getLogger("akshare_index_sync")

REL_DIR = "1_kline_data/index_daily"

OUT_COLS = [
    "symbol", "time", "open", "high", "low", "close",
    "volume", "amount", "release_id", "published_at",
]

MARKET_CONFIG = {
    "HK": {"env": "QM_QUANTHK_DATA_DIR", "default_dir": "/data/quanthk", "local": PROJECT_ROOT / "data" / "quanthk"},
    "US": {"env": "QM_QUANTUS_DATA_DIR", "default_dir": "/data/quantus", "local": PROJECT_ROOT / "data" / "quantus"},
}

# 指数配置: (akshare symbol, 落盘 symbol)
INDEXES = {
    "HK": [
        ("HSI", "HSI.HK"),
        ("HSCEI", "HSCEI.HK"),
        ("HSCCI", "HSCCI.HK"),
        ("HSTECH", "HSTECH.HK"),
    ],
    "US": [
        (".IXIC", "IXIC.US"),
        (".INX", "SPX.US"),
        (".DJI", "DJI.US"),
        (".NDX", "NDX.US"),
        (".SOX", "SOX.US"),
    ],
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
    local = cfg["local"]
    local.mkdir(parents=True, exist_ok=True)
    return local


def _fetch_index(market: str, ak_symbol: str, out_symbol: str) -> pd.DataFrame | None:
    """抓取单只指数全量日线。"""
    import akshare as ak

    try:
        if market == "HK":
            df = ak.stock_hk_index_daily_sina(symbol=ak_symbol)
        else:
            df = ak.index_us_stock_sina(symbol=ak_symbol)
        if df is None or df.empty:
            return None
        df = df.rename(columns={"date": "time"})
        df["time"] = pd.to_datetime(df["time"], errors="coerce")
        df = df.dropna(subset=["time"])
        if df.empty:
            return None
        df["symbol"] = out_symbol
        for c in ("open", "high", "low", "close"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0).astype("int64")
        df["amount"] = pd.to_numeric(df["amount"], errors="coerce").fillna(0.0)
        df["release_id"] = "akshare"
        df["published_at"] = datetime.now().isoformat(timespec="seconds")
        return df[OUT_COLS].dropna(subset=["close"])
    except Exception as exc:  # noqa: BLE001
        log.debug("抓取指数 %s %s 失败: %s", market, ak_symbol, exc)
        return None


def sync(market: str, *, dry_run: bool = False) -> dict:
    """同步指定市场指数日线。"""
    market_key = market.upper()
    if market_key not in INDEXES and market_key != "ALL":
        raise ValueError(f"market 必须是 HK/US/all，收到 {market}")

    targets = []
    if market_key == "ALL":
        for m in ("HK", "US"):
            targets.append((m, INDEXES[m]))
    else:
        targets.append((market_key, INDEXES[market_key]))

    for m, idx_list in targets:
        root = _data_dir(m) / REL_DIR
        root.mkdir(parents=True, exist_ok=True)
        log.info("[%s] 待同步指数: %d 只 (%s)", m, len(idx_list), [s[0] for s in idx_list])

        frames = []
        for ak_symbol, out_symbol in idx_list:
            df = _fetch_index(m, ak_symbol, out_symbol)
            if df is None or df.empty:
                log.warning("[%s] %s 抓取失败或无数据", m, ak_symbol)
                continue
            frames.append(df)
            log.info("[%s] %s -> %s: %d 行", m, ak_symbol, out_symbol, len(df))

        if not frames:
            continue

        all_df = pd.concat(frames, ignore_index=True)
        grouped = {ts.strftime("%Y%m%d"): g for ts, g in all_df.groupby(all_df["time"].dt.date)}
        written = 0
        for date_str, chunk in sorted(grouped.items()):
            dt_dir = root / f"dt={date_str}"
            dt_dir.mkdir(parents=True, exist_ok=True)
            out = dt_dir / "data.parquet"
            if out.exists():
                old = pd.read_parquet(out)
                combined = pd.concat([old, chunk], ignore_index=True)
                combined = combined.drop_duplicates(subset=["symbol", "time"], keep="last")
                combined.to_parquet(out, index=False)
            else:
                chunk.to_parquet(out, index=False)
            written += 1

        log.info("[%s] 指数落盘完成: %d 分区, %d 行 (from %s to %s)", m, written, len(all_df), min(grouped), max(grouped))

    return {"markets": [m for m, _ in targets]}


def run(*, market: str = "all", dry_run: bool = False, **kwargs) -> dict:
    """供后台管理 API 调用的编程接口。"""
    return sync(market, dry_run=dry_run)


def main() -> int:
    parser = argparse.ArgumentParser(description="akshare 港美股指数日线 → QuantUS/QuantHK")
    parser.add_argument("--market", required=True, choices=["HK", "US", "all"], help="市场")
    parser.add_argument("--dry-run", action="store_true", help="仅预览，不抓取")
    args = parser.parse_args()

    try:
        result = sync(args.market, dry_run=args.dry_run)
        print(result)
        return 0
    except Exception as exc:  # noqa: BLE001
        log.error("同步失败: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
