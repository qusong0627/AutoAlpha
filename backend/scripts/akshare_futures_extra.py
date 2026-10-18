#!/usr/bin/env python3
"""QuantFutures 扩展数据集 — 交易所仓单 / 会员持仓排名 / 分合约日K / CFTC持仓 / 汇率。

数据集:
  2_base_sector/warehouse_receipts/dt=YYYYMMDD/data.parquet   四所仓单 (SHFE/DCE/CZCE/GFEX)
  2_base_sector/member_positions/dt=YYYYMMDD/data.parquet     会员持仓排名 (DCE/GFEX, akshare 1.18 仅此两所)
  2_base_sector/contracts_daily/{contract}.parquet            分合约日K (含真实结算价/持仓量)
  2_base_sector/cftc/cftc_{kind}.parquet                      CFTC COT 周度持仓 (国际品种)
  2_base_sector/fx_daily/{code}.parquet                       中行外汇牌价日度 (每100外币兑人民币)

用法:
  python backend/scripts/akshare_futures_extra.py --task receipts --days 90
  python backend/scripts/akshare_futures_extra.py --task member_positions --days 90
  python backend/scripts/akshare_futures_extra.py --task contracts_daily
  python backend/scripts/akshare_futures_extra.py --task cftc
  python backend/scripts/akshare_futures_extra.py --task fx
"""
from __future__ import annotations

import argparse
import inspect
import logging
import os
import sys
import time
from datetime import date
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("futures_extra")

FUT_DATA_DIR = Path(os.getenv("QM_QUANTFUTURES_DATA_DIR", str(PROJECT_ROOT / "data" / "quantfutures")))
BASE_DIR = FUT_DATA_DIR / "2_base_sector"

from backend.scripts.akshare_futures_sync import CN_MAIN  # noqa: E402

KLINE_BASE_COLS = ["symbol", "time", "open", "high", "low", "close", "volume", "amount", "release_id", "published_at"]


def _recent_trading_days(days: int) -> list[str]:
    kdir = FUT_DATA_DIR / "1_kline_data" / "daily_forward"
    dts = sorted(d.name[3:] for d in kdir.iterdir() if d.name.startswith("dt="))
    return dts[-days:]


# ---------------- receipts ----------------

def _flatten_cols(df: pd.DataFrame) -> pd.DataFrame:
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = ["|".join(str(x) for x in tup) for tup in df.columns]
    return df.loc[:, ~df.columns.duplicated()]


def _norm_receipt_df(df: pd.DataFrame, venue: str, d: str) -> pd.DataFrame:
    if df is None or len(df) == 0:
        return pd.DataFrame()
    df = _flatten_cols(df.copy())
    ren = {}
    for c in df.columns:
        cs = str(c)
        if "品种" in cs or cs.lower() == "variety":
            ren[c] = "variety"
        elif cs == "仓库编号":
            ren[c] = "warehouse_code"
        elif cs == "仓库简称" or cs == "仓库/分库":
            ren[c] = "warehouse"
        elif "仓库" in cs or "地区" in cs:
            ren[c] = "warehouse"
        elif cs in ("今日仓单", "仓单", "仓单量", "仓单数量", "今日仓单量"):
            ren[c] = "receipt_qty"
        elif cs in ("昨日仓单量", "昨日仓单"):
            ren[c] = "prev_receipt_qty"
        elif "增减" in cs:
            ren[c] = "delta_qty"
        elif "有效预报" in cs:
            ren[c] = "forward_forecast"
        elif "日期" in cs or cs.lower() == "date":
            ren[c] = "date"
    df = df.rename(columns=ren)
    df = df.loc[:, ~df.columns.duplicated()]
    df.insert(0, "venue", venue)
    df["trade_date"] = d
    return df


def task_receipts(days: int) -> dict:
    import akshare as ak
    out_dir = BASE_DIR / "warehouse_receipts"
    interfaces = []
    for name, venue in [("futures_shfe_warehouse_receipt", "SHFE"), ("futures_warehouse_receipt_dce", "DCE"),
                        ("futures_warehouse_receipt_czce", "CZCE"), ("futures_gfex_warehouse_receipt", "GFEX")]:
        fn = getattr(ak, name, None)
        if fn is None:
            log.warning("akshare 缺少 %s, 跳过", name)
            continue
        kw = {}
        sig = inspect.signature(fn).parameters
        if "date" in sig:
            kw["date"] = None
        interfaces.append((venue, fn, kw))

    dates = _recent_trading_days(days)
    ok = empty = 0
    for d in dates:
        frames = []
        for venue, fn, kw in interfaces:
            call = dict(kw)
            if "date" in call:
                call["date"] = d
            try:
                res = fn(**call)
            except Exception:  # noqa: BLE001
                time.sleep(0.3)
                continue
            if isinstance(res, dict):
                clean = []
                for k, v in res.items():
                    if isinstance(v, pd.DataFrame) and len(v):
                        clean.append(_flatten_cols(v).assign(variety=k))
                res = pd.concat(clean, ignore_index=True) if clean else pd.DataFrame()
            else:
                res = _flatten_cols(res)
            norm = _norm_receipt_df(res, venue, d)
            if len(norm):
                frames.append(norm)
            else:
                empty += 1
        if frames:
            out = pd.concat(frames, ignore_index=True)
            for c in out.columns:  # 混合类型列(如"升贴水"含数字+中文)统一清洗
                if out[c].dtype == object:
                    try:
                        out[c] = pd.to_numeric(out[c])
                    except (ValueError, TypeError):
                        out[c] = out[c].astype(str)
            pdir = out_dir / f"dt={d}"
            pdir.mkdir(parents=True, exist_ok=True)
            out.to_parquet(pdir / "data.parquet", index=False)
            ok += 1
        time.sleep(0.4)
    return {"task": "receipts", "days": len(dates), "written": ok, "empty": empty}


# ---------------- member_positions ----------------

def _norm_rank_frames(res: dict | pd.DataFrame, venue: str, d: str) -> pd.DataFrame:
    if isinstance(res, pd.DataFrame):
        res = {"ALL": res}
    frames = []
    for variety, df in res.items():
        if not isinstance(df, pd.DataFrame) or df.empty:
            continue
        df = df.copy()
        df = df.loc[:, ~df.columns.duplicated()]
        ren = {}
        for c in df.columns:
            cs = str(c)
            if "会员" in cs or "会员名称" in cs or "席位" in cs:
                ren[c] = "member"
            elif cs in ("持买量", "持买单量"):
                ren[c] = "long_vol"
            elif "增仓" in cs and "买" in cs:
                ren[c] = "long_delta"
            elif cs in ("持卖量", "持卖单量"):
                ren[c] = "short_vol"
            elif "增仓" in cs and "卖" in cs:
                ren[c] = "short_delta"
            elif cs in ("排名", "名次"):
                ren[c] = "rank"
        df = df.rename(columns=ren)
        df.insert(0, "variety", str(variety))
        df.insert(1, "venue", venue)
        df["trade_date"] = d
        frames.append(df)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def task_member_positions(days: int) -> dict:
    import akshare as ak
    out_dir = BASE_DIR / "member_positions"
    fns = []
    for name, venue in [("futures_dce_position_rank", "DCE"), ("futures_gfex_position_rank", "GFEX")]:
        fn = getattr(ak, name, None)
        if fn is not None:
            fns.append((venue, fn))
        else:
            log.warning("akshare 缺少 %s, 跳过", name)
    if not fns:
        return {"task": "member_positions", "error": "no interface"}
    dates = _recent_trading_days(days)
    ok = 0
    for d in dates:
        frames = []
        for venue, fn in fns:
            kw = {}
            sig = inspect.signature(fn).parameters
            if "date" in sig:
                kw["date"] = d
            try:
                res = fn(**kw)
                norm = _norm_rank_frames(res, venue, d)
                if len(norm):
                    frames.append(norm)
            except Exception:  # noqa: BLE001
                time.sleep(0.3)
        if frames:
            pdir = out_dir / f"dt={d}"
            pdir.mkdir(parents=True, exist_ok=True)
            pd.concat(frames, ignore_index=True).to_parquet(pdir / "data.parquet", index=False)
            ok += 1
        time.sleep(0.4)
    return {"task": "member_positions", "days": len(dates), "written": ok}


# ---------------- contracts_daily ----------------

# 分合约品种 stem（合约代码前缀，如 RB2509 → RB）— 由扩展后的国内主力清单推导：
# CN_MAIN 键为 `{stem}0` 主力连续代码，去掉尾部 0 即合约代码前缀（RB0 → RB）
_CN_MAIN_STEMS = sorted({k[:-1] for k in CN_MAIN})


def _norm_contract_kline(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    colmap = {"日期": "time", "开盘价": "open", "最高价": "high", "最低价": "low", "收盘价": "close",
              "成交量": "volume", "持仓量": "open_interest", "动态结算价": "settlement",
              "date": "time", "open": "open", "high": "high", "low": "low", "close": "close"}
    df = df.rename(columns=colmap)
    for c in ("open", "high", "low", "close", "volume", "open_interest", "settlement"):
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    df["symbol"] = symbol
    df["amount"] = df.get("close") * df.get("volume")
    df["release_id"] = "akshare"
    df["published_at"] = pd.Timestamp.now().isoformat()
    cols = [c for c in KLINE_BASE_COLS if c in df.columns] + \
           [c for c in ("settlement", "open_interest") if c in df.columns]
    return df[cols].dropna(subset=["close"])


def task_contracts_daily() -> dict:
    import akshare as ak
    out_dir = BASE_DIR / "contracts_daily"
    out_dir.mkdir(parents=True, exist_ok=True)
    today = date.today()
    months = []
    y, m = today.year, today.month
    for back in range(12, -3, -1):  # 过去12个月 + 未来3个月
        tot = y * 12 + (m - 1) - back
        months.append(f"{tot // 12 % 100:02d}{tot % 12 + 1:02d}")
    written, empty = 0, 0
    for stem in _CN_MAIN_STEMS:
        for ym in months:
            contract = f"{stem}{ym}"
            try:
                df = ak.futures_zh_daily_sina(symbol=contract)
            except Exception:  # noqa: BLE001
                time.sleep(0.3)
                continue
            if df is None or df.empty:
                empty += 1
                continue
            norm = _norm_contract_kline(df, f"{contract}.CN")
            if len(norm):
                norm.to_parquet(out_dir / f"{contract}.CN.parquet", index=False)
                written += 1
            time.sleep(0.25)
        log.info("stem %s 完成", stem)
    return {"task": "contracts_daily", "written": written, "empty": empty}


# ---------------- cftc ----------------

def task_cftc() -> dict:
    import akshare as ak
    out_dir = BASE_DIR / "cftc"
    out_dir.mkdir(parents=True, exist_ok=True)
    results = {}
    for kind, fn_name in [("c", "macro_usa_cftc_c_holding"), ("merchant_goods", "macro_usa_cftc_merchant_goods_holding")]:
        fn = getattr(ak, fn_name, None)
        if fn is None:
            continue
        try:
            df = fn()
        except Exception as exc:  # noqa: BLE001
            log.warning("cftc %s 失败: %s", kind, str(exc)[:80])
            continue
        df.to_parquet(out_dir / f"cftc_{kind}.parquet", index=False)
        results[kind] = len(df)
    return {"task": "cftc", "rows": results}


# ---------------- fx_daily ----------------

# 中行外汇牌价(每100外币兑人民币) — akshare currency_boc_sina 支持的币种子集
_FX_CURRENCIES = {
    "美元": "USD", "英镑": "GBP", "欧元": "EUR", "港币": "HKD", "日元": "JPY",
    "澳大利亚元": "AUD", "加拿大元": "CAD", "瑞士法郎": "CHF", "新加坡元": "SGD",
    "新西兰元": "NZD", "瑞典克朗": "SEK", "挪威克朗": "NOK", "丹麦克朗": "DKK", "澳门元": "MOP",
}


def task_fx() -> dict:
    import akshare as ak
    out_dir = BASE_DIR / "fx_daily"
    out_dir.mkdir(parents=True, exist_ok=True)
    results = {}
    for cn, code in _FX_CURRENCIES.items():
        try:
            df = ak.currency_boc_sina(
                symbol=cn, start_date="20000101", end_date=date.today().strftime("%Y%m%d")
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("汇率 %s 失败: %s", cn, str(exc)[:50])
            continue
        df = df.rename(columns={"日期": "time", "中行汇买价": "bid", "中行钞买价": "bid_cash",
                                "中行钞卖价/汇卖价": "sell", "央行中间价": "pbc_mid", "中行折算价": "mid"})
        df["time"] = pd.to_datetime(df["time"], errors="coerce")
        df = df.dropna(subset=["time"])
        for c in ("bid", "bid_cash", "sell", "pbc_mid", "mid"):
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.dropna(subset=["mid"]).sort_values("time")
        df["pair"] = f"{code}CNY"
        df["quote_unit"] = "100外币"
        df["release_id"] = "akshare"
        df["published_at"] = pd.Timestamp.now().isoformat()
        df.to_parquet(out_dir / f"{code}.parquet", index=False)
        results[code] = (len(df), str(df["time"].min().date()), str(df["time"].max().date()))
        log.info("fx %sCNY: %d 行 %s ~ %s", code, len(df), df["time"].min().date(), df["time"].max().date())
    return {"task": "fx", "pairs": results}


def main() -> int:
    ap = argparse.ArgumentParser(description="QuantFutures 扩展数据集")
    ap.add_argument("--task", required=True,
                    choices=["receipts", "member_positions", "contracts_daily", "cftc", "fx"])
    ap.add_argument("--days", type=int, default=90)
    args = ap.parse_args()
    if args.task == "receipts":
        r = task_receipts(args.days)
    elif args.task == "member_positions":
        r = task_member_positions(args.days)
    elif args.task == "contracts_daily":
        r = task_contracts_daily()
    elif args.task == "fx":
        r = task_fx()
    else:
        r = task_cftc()
    print(r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
