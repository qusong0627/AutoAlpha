#!/usr/bin/env python3
"""QuantHK 复权因子链 + daily_backward(后复权日线)。

原理: daily_forward 存的是不复权价; 除权除息日 preclose != 前一日收盘,
  adj_step_t = preclose_t / close_{t-1}   (正常日=1)
  adj_factor_t = adj_factor_{t-1} × adj_step_t   (上市=1, 累乘)
后复权价 = 不复权价 × adj_factor。

preclose 来源:
  - 2026-05-08 之前: 付费 CSV (_paid_raw/{code}.csv 的 昨收盘 列)
  - 2026-05-09 之后: akshare stock_hk_hist 涨跌幅反推 (preclose = close/(1+涨跌幅))

落盘:
  1_kline_data/daily_backward/dt=YYYYMMDD/data.parquet   10列标准K线, 价格×因子
  2_base_sector/adjust_factors/{symbol}.parquet          (time, close_raw, preclose, adj_step, adj_factor)

用法:
  python backend/scripts/quanthk_adjust_factor.py                # 全量
  python backend/scripts/quanthk_adjust_factor.py --workers 6
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("quanthk_adj")

QUANTHK_DATA_DIR = Path(os.getenv("QM_QUANTHK_DATA_DIR", str(PROJECT_ROOT / "data" / "quanthk")))
PAID_DIR = QUANTHK_DATA_DIR / "_paid_raw"
BACKWARD_DIR = QUANTHK_DATA_DIR / "1_kline_data" / "daily_backward"
FACTOR_DIR = QUANTHK_DATA_DIR / "2_base_sector" / "adjust_factors"

KLINE_COLS = ["symbol", "time", "open", "high", "low", "close", "volume", "amount", "release_id", "published_at"]
AKSHARE_START = "20260501"  # 与付费数据(至20260508)重叠几天, 合并时付费优先

_now_iso = pd.Timestamp.now().isoformat()


def _load_paid(code5: str) -> pd.DataFrame | None:
    f = PAID_DIR / f"{code5}.csv"
    if not f.exists():
        return None
    try:
        df = pd.read_csv(f, encoding="gbk", low_memory=False)
    except Exception:  # noqa: BLE001
        df = pd.read_csv(f, encoding="utf-8", low_memory=False)
    df = df.rename(columns={"交易日期": "time", "开盘价": "open", "最高价": "high", "最低价": "low",
                            "收盘价": "close", "昨收盘": "preclose", "成交量": "volume", "成交额": "amount"})
    df["time"] = pd.to_datetime(df["time"], errors="coerce")
    for c in ("open", "high", "low", "close", "preclose", "volume", "amount"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["time", "close"]).sort_values("time")


def _load_akshare_recent(code5: str, retries: int = 3) -> pd.DataFrame | None:
    import akshare as ak
    import time
    for i in range(retries):
        try:
            df = ak.stock_hk_hist(symbol=code5, period="daily", start_date=AKSHARE_START,
                                  end_date=str(date.today()).replace("-", ""), adjust="")
        except Exception:  # noqa: BLE001
            time.sleep(3 * (i + 1))
            continue
        if df is None or df.empty:
            return None
        df = df.rename(columns={"日期": "time", "开盘": "open", "最高": "high", "最低": "low",
                                "收盘": "close", "成交量": "volume", "成交额": "amount", "涨跌幅": "pct"})
        df["time"] = pd.to_datetime(df["time"], errors="coerce")
        for c in ("open", "high", "low", "close", "volume", "amount", "pct"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df["preclose"] = df["close"] / (1.0 + df["pct"].fillna(0.0) / 100.0)
        return df.dropna(subset=["time", "close"])
    return None


def build_symbol(code5: str) -> pd.DataFrame | None:
    paid = _load_paid(code5)
    recent = _load_akshare_recent(code5)
    if paid is None:
        return None
    if recent is not None:
        cut = paid["time"].max()
        recent = recent[recent["time"] > cut]
        df = pd.concat([paid, recent], ignore_index=True)
    else:
        df = paid
    df = df.drop_duplicates("time", keep="first").sort_values("time").reset_index(drop=True)

    prev_close = df["close"].shift(1)
    step = df["preclose"] / prev_close
    step = step.where(prev_close.notna() & df["preclose"].notna() & (step > 0), 1.0)
    # 正常日容差: 防止浮点/分价误差把 1.0000001 当除权
    step = np.where(np.abs(step - 1.0) < 1e-4, 1.0, step)
    factor = step.cumprod()

    out = pd.DataFrame({
        "time": df["time"].dt.date,
        "close_raw": df["close"],
        "preclose": df["preclose"],
        "adj_step": step,
        "adj_factor": factor,
    })
    out.insert(0, "symbol", code5)
    kline = df[["time", "open", "high", "low", "close", "volume", "amount"]].copy()
    kline["symbol_h5"] = code5
    return out, kline


def main() -> int:
    ap = argparse.ArgumentParser(description="QuantHK 复权因子 + daily_backward")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--full", action="store_true", help="忽略断点续跑, 全量重算")
    args = ap.parse_args()

    from backend.shared.stock_utils import StockCodeUtil

    codes = sorted(p.stem for p in PAID_DIR.glob("*.csv"))
    if not args.full:  # 断点续跑: 因子已覆盖近月的标的跳过
        resume_cut = pd.Timestamp("2026-08-15").date()
        skipped = 0
        fresh = []
        for c in codes:
            fp = FACTOR_DIR / f"{StockCodeUtil.to_hk_suffix(c)}.parquet"
            if fp.exists():
                end = pd.read_parquet(fp, columns=["time"])["time"].max()
                if pd.Timestamp(end).date() >= resume_cut:
                    skipped += 1
                    continue
            fresh.append(c)
        log.info("断点续跑: 跳过 %d, 待跑 %d", skipped, len(fresh))
        codes = fresh
    log.info("付费 CSV: %d 只", len(codes))
    FACTOR_DIR.mkdir(parents=True, exist_ok=True)
    BACKWARD_DIR.mkdir(parents=True, exist_ok=True)

    from backend.shared.stock_utils import StockCodeUtil

    frames, kline_frames, ok, fail = [], [], 0, 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(build_symbol, c): c for c in codes}
        for i, fut in enumerate(as_completed(futs), 1):
            code = futs[fut]
            try:
                res, kline = fut.result()
            except Exception as exc:  # noqa: BLE001
                fail += 1
                log.warning("%s 失败: %s", code, str(exc)[:80])
                continue
            if res is None or res.empty:
                fail += 1
                continue
            ok += 1
            frames.append(res)
            kline_frames.append(kline)
            sym = StockCodeUtil.to_hk_suffix(code)
            res.assign(source="paid+akshare").to_parquet(FACTOR_DIR / f"{sym}.parquet", index=False)
            if i % 200 == 0:
                log.info("进度 %d/%d (ok=%d fail=%d)", i, len(codes), ok, fail)

    if not frames:
        log.error("无任何复权因子产出")
        return 1
    allf = pd.concat(frames, ignore_index=True)
    allf["factor"] = allf["adj_factor"]

    # daily_backward: 每交易日分区 (价格×因子, 量/额保留不复权); K线直接复用内存中已合并的付费+akshare数据
    raw = pd.concat(kline_frames, ignore_index=True)
    from backend.shared.stock_utils import StockCodeUtil as SU
    raw["time"] = pd.to_datetime(raw["time"]).dt.date
    raw = raw.merge(allf[["symbol", "time", "factor"]], left_on=["symbol_h5", "time"],
                    right_on=["symbol", "time"], how="inner")
    raw["symbol"] = raw["symbol_h5"].map(lambda s: SU.to_hk_suffix(s))
    raw["release_id"] = "backward_adjusted"
    raw["published_at"] = _now_iso
    n_parts = 0
    for d, g in raw.groupby("time"):
        pdir = BACKWARD_DIR / f"dt={pd.Timestamp(d).strftime('%Y%m%d')}"
        pdir.mkdir(parents=True, exist_ok=True)
        out = pd.DataFrame({
            "symbol": g["symbol"], "time": g["time"],
            "open": g["open"] * g["factor"], "high": g["high"] * g["factor"],
            "low": g["low"] * g["factor"], "close": g["close"] * g["factor"],
            "volume": g["volume"], "amount": g["amount"],
            "release_id": g["release_id"], "published_at": g["published_at"],
        })[KLINE_COLS]
        out.to_parquet(pdir / "data.parquet", index=False)
        n_parts += 1
    log.info("daily_backward 分区: %d", n_parts)
    print({"ok": ok, "fail": fail, "rows": len(raw), "partitions": n_parts})
    return 0


if __name__ == "__main__":
    sys.exit(main())
