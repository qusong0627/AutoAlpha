#!/usr/bin/env python3
"""QuantHK 扩展数据集同步 — AH比价 / 港股通成分 / 中证港股通指数权重 / 估值快照(Hive)。

数据集:
  2_base_sector/ah_membership.parquet      A/H 配对清单 (stock_zh_ah_name)
  2_base_sector/ah_premium/dt=YYYYMMDD/    AH 溢价率日截面 (A收盘=本地QuantDB, H收盘=akshare, 汇率=中行折算价)
  2_base_sector/hsgt_membership.parquet    港股通标的名单 (stock_hk_ggt_components_em)
  2_base_sector/index_weights/{code}.parquet  中证港股通系列指数成分权重 (csindex)
  5_technical_derived/valuation/dt=YYYYMMDD/  标准估值快照 (由 akshare_valuation + akshare_financial 构建)

用法:
  python backend/scripts/quanthk_extra_datasets.py --task ah_membership
  python backend/scripts/quanthk_extra_datasets.py --task ah_premium --history
  python backend/scripts/quanthk_extra_datasets.py --task ggt_membership
  python backend/scripts/quanthk_extra_datasets.py --task index_weights
  python backend/scripts/quanthk_extra_datasets.py --task valuation_snapshot [--dt 20260828] [--cleanup-yahoo]
"""
from __future__ import annotations

import argparse
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
log = logging.getLogger("quanthk_extra")

QUANTHK_DATA_DIR = Path(os.getenv("QM_QUANTHK_DATA_DIR", str(PROJECT_ROOT / "data" / "quanthk")))
QUANTDB_DATA_DIR = Path(os.getenv("QM_QUANTDB_DATA_DIR", str(PROJECT_ROOT / "data" / "quantdb")))

BASE_DIR = QUANTHK_DATA_DIR / "2_base_sector"
TECH_DIR = QUANTHK_DATA_DIR / "5_technical_derived"

# 中证港股通系列指数(2026-08 实测 csindex 接口可用, 交易所=香港证券交易所)
CSI_HK_INDICES = [
    "931722",  # 国新港股通央企红利
    "930962",  # 港股通工业C
    "930963",  # 港股通可选C
    "930965",  # 港股通医药C
    "930966",  # 港股通金融C
    "930967",  # 港股通信息C
    "931573",  # 港股通科技
    "931574",  # 港股科技
    "930959",  # 港股通海外50
    "930960",  # 港股通能源C
]

VAL_COLS = ["symbol", "time", "close", "total_capital", "circulating_capital", "total_mv", "float_mv",
            "net_profit_ttm", "revenue_ttm", "equity", "annual_net_profit", "pe_ttm", "pe_static",
            "pb", "ps_ttm", "dividend_rate", "release_id", "published_at"]


def _ak_retry(fn, retries=4, sleep=8):
    last = None
    for i in range(retries):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last = exc
            log.warning("retry %s/%s: %s", i + 1, retries, str(exc)[:80])
            time.sleep(sleep * (i + 1))
    raise last


def _latest_daily_forward_close() -> pd.DataFrame:
    """daily_forward 最新分区的 close, 用于估值快照的价格列。"""
    import duckdb
    kdir = QUANTHK_DATA_DIR / "1_kline_data" / "daily_forward"
    dts = sorted(d.name[3:] for d in kdir.iterdir() if d.name.startswith("dt="))
    if not dts:
        return pd.DataFrame(columns=["symbol", "close"])
    latest = dts[-1]
    df = pd.read_parquet(kdir / f"dt={latest}", columns=["symbol", "close"])
    df = df.sort_values("time").groupby("symbol").tail(1) if "time" in df.columns else df
    log.info("daily_forward 最新分区 %s: %d 只", latest, len(df))
    return df[["symbol", "close"]]


# ---------------- ah_membership ----------------

def task_ah_membership() -> dict:
    import akshare as ak
    df = _ak_retry(lambda: ak.stock_zh_ah_name())
    # 列: 名称/H股代码/目前是否可买卖/证券代码/名称(H) 等, 以实际为准做列名兼容
    cols = {c: c for c in df.columns}
    log.info("stock_zh_ah_name cols: %s", list(df.columns))
    out_path = BASE_DIR / "ah_membership.parquet"
    df = df.assign(source="akshare", updated_at=pd.Timestamp.now().isoformat())
    df.to_parquet(out_path, index=False)
    return {"task": "ah_membership", "rows": len(df), "path": str(out_path)}


# ---------------- ah_premium ----------------

def _load_a_close_for(codes: list[str]) -> pd.DataFrame:
    """从本地 QuantDB A股 daily_forward 扫一次, 取指定 A 股代码的收盘序列。

    codes: 6位数字代码; QuantDB symbol 为前缀格式(SH600036/SZ000002)。
    """
    import duckdb
    con = duckdb.connect()
    con.execute("SET threads=4")
    like = [_a_symbol(c) for c in codes]
    q = """
      SELECT symbol, CAST(time AS DATE) AS d, close
      FROM read_parquet(?, hive_partitioning=false, filename=true)
      WHERE symbol IN (SELECT unnest(?::VARCHAR[]))
    """
    df = con.execute(q, [str(QUANTDB_DATA_DIR / "1_kline_data" / "daily_forward" / "dt=*" / "data.parquet"),
                         like]).fetchdf()
    con.close()
    return df


def _a_symbol(code6: str) -> str:
    """6位代码 → QuantDB 后缀格式 (600876.SH / 001236.SZ)"""
    suf = "SH" if code6.startswith(("6", "9")) else "BJ" if code6.startswith(("4", "8")) else "SZ"
    return f"{code6}.{suf}"


def task_ah_premium(history: bool = True) -> dict:
    import akshare as ak
    from backend.shared.stock_utils import StockCodeUtil
    mem_path = BASE_DIR / "ah_membership.parquet"
    if not mem_path.exists():
        task_ah_membership()
    mem = pd.read_parquet(mem_path)
    # 幂等: 首次用 代码(H股5位)/名称匹配; 重跑直接复用已 enrich 的 h_symbol/a_symbol
    if "代码" in mem.columns:
        mem["h5"] = mem["代码"].astype(str).str.zfill(5)
    elif "h_symbol" in mem.columns:
        mem["h5"] = mem["h_symbol"].astype(str).str.split(".").str[0].str.zfill(5)
    else:
        raise RuntimeError("ah_membership 缺少代码列")
    if "a_symbol" not in mem.columns:
        inst = pd.read_parquet(QUANTDB_DATA_DIR / "2_base_sector" / "instrument_detail" / "instrument_detail.parquet",
                               columns=["Symbol", "Name"])
        inst["name_norm"] = inst["Name"].astype(str).str.strip()
        name2sym = inst.drop_duplicates("name_norm").set_index("name_norm")["Symbol"].to_dict()
        mem["a_symbol"] = mem["名称"].astype(str).str.strip().map(name2sym)
        log.info("A/H 名称匹配: %d/%d", mem["a_symbol"].notna().sum(), len(mem))
    mem["h_symbol"] = mem["h5"].map(lambda s: StockCodeUtil.to_hk_suffix(s))
    mem = mem.dropna(subset=["a_symbol"]).copy()
    if mem.empty:
        raise RuntimeError("A/H 名称匹配全部失败")
    mem["a_code6"] = mem["a_symbol"].astype(str).str.split(".").str[0]
    mem_out = mem[["h_symbol", "a_symbol", "名称", "source", "updated_at"]]
    mem_out.to_parquet(mem_path, index=False)

    fx = _ak_retry(lambda: ak.currency_boc_sina(symbol="港币", start_date="20180101", end_date="20261231"))
    fx["d"] = pd.to_datetime(fx["日期"]).dt.date
    fx = fx.set_index("d")["中行折算价"].dropna()
    fx_per_hkd = fx.astype(float) / 100.0

    out_dir = BASE_DIR / "ah_premium"
    frames = []
    start_year = "2018" if history else "2024"
    for _, row in mem.iterrows():
        h5 = str(row["h_symbol"]).split(".")[0].zfill(5)
        a6 = str(row["a_code6"])
        try:
            hdf = _ak_retry(lambda h=h5: ak.stock_zh_ah_daily(symbol=h5, start_year=start_year,
                                                              end_year=str(date.today().year), adjust=""))
        except Exception as exc:  # noqa: BLE001
            log.warning("AH %s 日线失败: %s", h5, str(exc)[:60])
            continue
        hdf = hdf.rename(columns={"日期": "d", "收盘": "h_close"})
        hdf["d"] = pd.to_datetime(hdf["d"]).dt.date
        frames.append(pd.DataFrame({"h_symbol": row["h_symbol"], "a_symbol": row["a_symbol"], "a_code6": a6,
                                    "d": hdf["d"], "h_close": hdf["h_close"].astype(float)}))
    if not frames:
        raise RuntimeError("AH 日线全部抓取失败")
    ah = pd.concat(frames, ignore_index=True)

    log.info("扫描本地 A股 daily_forward (%d 只 A股)...", ah["a_code6"].nunique())
    aclose = _load_a_close_for(sorted(ah["a_code6"].unique()))
    aclose["d"] = pd.to_datetime(aclose["d"]).dt.date
    aclose = aclose.rename(columns={"symbol": "a_symbol_full"})
    aclose["a_code6"] = aclose["a_symbol_full"].astype(str).str.split(".").str[0]
    ah = ah.merge(aclose[["a_code6", "d", "close"]], on=["a_code6", "d"], how="left")
    ah["fx_hkd_cny"] = ah["d"].map(fx_per_hkd)
    ah["premium_pct"] = (ah["close"] / (ah["h_close"] * ah["fx_hkd_cny"]) - 1.0) * 100.0
    ah = ah.dropna(subset=["close", "h_close", "fx_hkd_cny"])
    log.info("可计算溢价行数: %d, 覆盖 %d 个交易日", len(ah), ah["d"].nunique())

    n = 0
    for d, g in ah.groupby("d"):
        pdir = out_dir / f"dt={d.strftime('%Y%m%d')}"
        pdir.mkdir(parents=True, exist_ok=True)
        g[["h_symbol", "a_symbol", "close", "h_close", "fx_hkd_cny", "premium_pct"]] \
            .rename(columns={"close": "a_close"}).to_parquet(pdir / "data.parquet", index=False)
        n += 1
    return {"task": "ah_premium", "partitions": n, "rows": len(ah)}


# ---------------- ggt_membership ----------------

def task_ggt_membership() -> dict:
    import akshare as ak
    try:
        df = _ak_retry(lambda: ak.stock_hk_ggt_components_em(), retries=5, sleep=15)
        source = "eastmoney"
    except Exception as exc:  # noqa: BLE001
        log.warning("东财港股通成分持续失败(%s), 退化为南向持仓推导", str(exc)[:60])
        south = BASE_DIR / "hsgt_south"
        syms = set()
        for p in south.glob("*.parquet"):
            syms.add(pd.read_parquet(p).get("symbol", pd.Series(dtype=str)))
        for p in south.glob("dt=*/data.parquet"):
            syms.add(pd.read_parquet(p, columns=["symbol"])["symbol"])
        df = pd.DataFrame({"symbol": sorted(syms)})
        source = "south_holdings_derived"
    if "symbol" not in df.columns:
        code_col = next((c for c in df.columns if "代码" in c or c.lower() == "code"), df.columns[0])
        df["symbol"] = df[code_col].astype(str).str.split(".").str[0].str.zfill(5)
        from backend.shared.stock_utils import StockCodeUtil
        df["symbol"] = df["symbol"].map(lambda s: StockCodeUtil.to_hk_suffix(s) if hasattr(StockCodeUtil, "to_hk_suffix") else s)
    df = df.assign(source=source, updated_at=pd.Timestamp.now().isoformat())
    out_path = BASE_DIR / "hsgt_membership.parquet"
    df.to_parquet(out_path, index=False)
    return {"task": "ggt_membership", "rows": len(df), "source": source, "path": str(out_path)}


# ---------------- index_weights ----------------

def task_index_weights() -> dict:
    import akshare as ak
    from backend.shared.stock_utils import StockCodeUtil
    out_dir = BASE_DIR / "index_weights"
    out_dir.mkdir(parents=True, exist_ok=True)
    results = {}
    for code in CSI_HK_INDICES:
        try:
            df = _ak_retry(lambda c=code: ak.index_stock_cons_weight_csindex(symbol=c), retries=3)
        except Exception as exc:  # noqa: BLE001
            log.warning("csindex %s 失败: %s", code, str(exc)[:60])
            continue
        df = df.rename(columns={"成分券代码": "code_raw", "成分券名称": "name", "权重": "weight",
                                "日期": "index_date", "指数名称": "index_name"})
        df["symbol"] = df["code_raw"].astype(str).map(lambda s: StockCodeUtil.to_hk_suffix(str(s).zfill(5)))
        keep = df[["index_name", "symbol", "name", "weight", "index_date"]].copy()
        keep["index_code"] = code
        keep.to_parquet(out_dir / f"{code}.parquet", index=False)
        results[code] = (df["index_name"].iloc[0], len(df))
        log.info("index_weights %s %s: %d 成分", code, df["index_name"].iloc[0], len(df))
    return {"task": "index_weights", "indices": results}


# ---------------- valuation_snapshot ----------------

def task_valuation_snapshot(dt: str | None = None, cleanup_yahoo: bool = False) -> dict:
    val_dir = BASE_DIR / "akshare_valuation"
    fin_dir = BASE_DIR / "akshare_financial"
    val = pd.concat([pd.read_parquet(p) for p in val_dir.glob("*.parquet")], ignore_index=True)
    fin = pd.concat([pd.read_parquet(p) for p in fin_dir.glob("*.parquet")], ignore_index=True)
    val = val.drop_duplicates("symbol", keep="last").set_index("symbol")
    fin = fin.drop_duplicates("symbol", keep="last").set_index("symbol")
    close = _latest_daily_forward_close().set_index("symbol")["close"]

    idx = val.index.union(fin.index)
    snap = pd.DataFrame(index=idx)
    snap["close"] = close.reindex(idx)
    snap["total_mv"] = pd.to_numeric(fin.get("总市值(港元)"), errors="coerce").reindex(idx)
    snap["float_mv"] = pd.to_numeric(fin.get("港股市值(港元)"), errors="coerce").reindex(idx)
    snap["total_capital"] = pd.to_numeric(fin.get("已发行股本(股)"), errors="coerce").reindex(idx)
    snap["net_profit_ttm"] = pd.to_numeric(fin.get("净利润"), errors="coerce").reindex(idx)
    snap["revenue_ttm"] = pd.to_numeric(fin.get("营业总收入"), errors="coerce").reindex(idx)
    snap["pe_ttm"] = pd.to_numeric(val.get("市盈率-TTM"), errors="coerce").reindex(idx)
    snap["pe_static"] = pd.to_numeric(val.get("市盈率-LYR"), errors="coerce").reindex(idx)
    snap["pb"] = pd.to_numeric(val.get("市净率-MRQ"), errors="coerce").reindex(idx)
    snap["ps_ttm"] = pd.to_numeric(val.get("市销率-TTM"), errors="coerce").reindex(idx)
    snap["dividend_rate"] = pd.to_numeric(fin.get("股息率TTM(%)"), errors="coerce").reindex(idx)
    # equity 由 pb 反推 (仅 pe/pb 有效时)
    snap["equity"] = snap["total_mv"] / snap["pb"].replace(0, pd.NA)

    day = dt or date.today().strftime("%Y%m%d")
    out = snap.reset_index().rename(columns={"index": "symbol"})
    out["time"] = pd.Timestamp(day)
    out["release_id"] = "akshare_local"
    out["published_at"] = pd.Timestamp.now().isoformat()
    for c in VAL_COLS:  # schema 18 列补齐
        if c not in out.columns:
            out[c] = pd.NA
    out = out[VAL_COLS]
    pdir = TECH_DIR / "valuation" / f"dt={day}"
    pdir.mkdir(parents=True, exist_ok=True)
    out.to_parquet(pdir / "data.parquet", index=False)

    removed = 0
    if cleanup_yahoo:
        vroot = TECH_DIR / "valuation"
        for pdir_old in vroot.glob("dt=*"):
            if pdir_old.name == f"dt={day}":
                continue
            f = pdir_old / "data.parquet"
            if not f.exists():
                continue
            dfo = pd.read_parquet(f, columns=["release_id", "close", "pe_ttm"])
            if (dfo["release_id"] == "yahoo").all() and dfo["pe_ttm"].isna().all():
                import shutil
                shutil.rmtree(pdir_old)
                removed += 1
    return {"task": "valuation_snapshot", "rows": len(out), "dt": day, "removed_yahoo_partitions": removed}


def main() -> int:
    ap = argparse.ArgumentParser(description="QuantHK 扩展数据集")
    ap.add_argument("--task", required=True,
                    choices=["ah_membership", "ah_premium", "ggt_membership", "index_weights", "valuation_snapshot"])
    ap.add_argument("--history", action="store_true", help="ah_premium 从 2018 起回补")
    ap.add_argument("--dt", default=None, help="valuation_snapshot 分区日期 YYYYMMDD")
    ap.add_argument("--cleanup-yahoo", action="store_true", help="清理全空 yahoo 估值分区")
    args = ap.parse_args()
    if args.task == "ah_membership":
        r = task_ah_membership()
    elif args.task == "ah_premium":
        r = task_ah_premium(history=args.history)
    elif args.task == "ggt_membership":
        r = task_ggt_membership()
    elif args.task == "index_weights":
        r = task_index_weights()
    else:
        r = task_valuation_snapshot(dt=args.dt, cleanup_yahoo=args.cleanup_yahoo)
    print(r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
