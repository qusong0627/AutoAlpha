#!/usr/bin/env python3
"""akshare 期货数据 → QuantFutures 同步脚本。

用 akshare 抓取国内/国际期货、贵金属数据，按 QuantDB 的 Hive 分区格式
落盘到 data/quantfutures。

数据段:
  foreign_realtime  国际期货实时（原油/黄金/白银/铜等）→ 2_base_sector/futures_realtime
  foreign_daily     国际期货日K（主力连续）          → 1_kline_data/daily_forward
  cn_realtime       国内期货实时（828 只合约）       → 2_base_sector/futures_realtime
  cn_daily          国内主力连续日K                  → 1_kline_data/daily_forward
  sge_daily         上金所贵金属现货日K              → 1_kline_data/daily_forward

落盘格式:
  {quantfutures}/1_kline_data/daily_forward/dt=YYYYMMDD/data.parquet
  {quantfutures}/2_base_sector/futures_realtime/{symbol}.parquet

symbol 命名:
  国际实时: CL  → CL.FUT（NYMEX原油）
  国内实时: 合约代码原样（ag2608）
  日K: 品种代码（CL / GC / AU9999）

用法:
  # 全部数据段
  python backend/scripts/akshare_futures_sync.py --field all

  # 国际期货实时
  python backend/scripts/akshare_futures_sync.py --field foreign_realtime
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
log = logging.getLogger("akshare_futures_sync")

# 数据目录：环境变量 QM_QUANTFUTURES_DATA_DIR，默认 data/quantfutures
_FUTURES_DATA_DIR_ENV = "QM_QUANTFUTURES_DATA_DIR"
_FUTURES_DEFAULT_DIRS = [
    "/data/quantfutures",  # Docker 容器内
    str(PROJECT_ROOT / "data" / "quantfutures"),  # 项目根
]

KLINE_COLS = [
    "symbol", "time", "open", "high", "low", "close",
    "volume", "amount", "release_id", "published_at",
]

# 国际期货品种: (akshare 代码, 名称)
# 池子 = 新浪外盘订阅清单全集（futures_foreign_commodity_subscribe_exchange_symbol，2026-08 共 30 种），
# 全部经实时 + 日K 双接口实测可用。旧池中的 WTI/BRENT/PL/PA/AL/CU/ZN/NI/SN 已被新浪移除，
# 实时/日K 双双报错，由替代代码覆盖（原油→CL/OIL、贵金属→GC/SI/XAU/XAG/XPT/XPD、铜→HG/CAD），已剔除。
FOREIGN_SYMBOLS = {
    # 能源
    "CL": "NYMEX原油",
    "OIL": "布伦特原油(II)",
    "NG": "NYMEX天然气",
    # 贵金属 (COMEX 期货 + 伦敦现货)
    "GC": "COMEX黄金",
    "SI": "COMEX白银",
    "XAU": "伦敦金现货",
    "XAG": "伦敦银现货",
    "XPT": "伦敦铂现货",
    "XPD": "伦敦钯现货",
    # 基本金属 (COMEX + LME + LME迷你)
    "HG": "COMEX铜",
    "AHD": "LME铝迷你",
    "CAD": "LME铜迷你",
    "ZSD": "LME锌迷你",
    "NID": "LME镍迷你",
    "SND": "LME锡迷你",
    "PBD": "LME铅迷你",
    # 农产品/软商品
    "S": "美大豆",
    "W": "美小麦",
    "C": "美玉米",
    "BO": "美豆油",
    "SM": "美豆粕",
    "LHC": "美活牛",
    "CT": "ICE棉花",
    "FCPO": "马来棕榈油",
    "RSS3": "日胶RSS3",
    "RS": "国际菜籽",
    "TRB": "国际废钢",
    # 金融/其他
    "FEF": "欧元外汇期货",
    "EUA": "欧盟碳配额",
    "BTC": "比特币期货",
}

# 国内主力连续: (新浪代码, 品种名) — 由 futures_display_main_sina 全量生成(2026-08, 82品种) + CFFEX T0/TL0
CN_MAIN = {
    # 大商所 dce
    "V0": "PVC", "P0": "棕榈油", "B0": "豆二", "M0": "豆粕", "I0": "铁矿石",
    "JD0": "鸡蛋", "L0": "塑料", "PP0": "聚丙烯", "FB0": "纤维板", "Y0": "豆油",
    "C0": "玉米", "A0": "豆一", "J0": "焦炭", "JM0": "焦煤", "CS0": "淀粉",
    "EG0": "乙二醇", "RR0": "粳米", "EB0": "苯乙烯", "PG0": "液化石油气",
    "LH0": "生猪", "LG0": "原木", "BZ0": "纯苯",
    # 郑商所 czce
    "TA0": "PTA", "OI0": "菜油", "RS0": "菜籽", "RM0": "菜粕", "WH0": "强麦",
    "JR0": "粳稻", "SR0": "白糖", "CF0": "棉花", "RI0": "早籼稻", "MA0": "甲醇",
    "FG0": "玻璃", "LR0": "晚籼稻", "SF0": "硅铁", "SM0": "锰硅", "CY0": "棉纱",
    "AP0": "苹果", "CJ0": "红枣", "UR0": "尿素", "SA0": "纯碱", "PF0": "短纤",
    "PK0": "花生", "SH0": "烧碱", "PX0": "对二甲苯", "PR0": "瓶片", "PL0": "丙烯",
    # 上期所 shfe / 能源中心 ine
    "FU0": "燃料油", "SC0": "上海原油", "AL0": "沪铝", "RU0": "天然橡胶",
    "ZN0": "沪锌", "CU0": "沪铜", "AU0": "沪金", "RB0": "螺纹钢", "PB0": "沪铅",
    "AG0": "沪银", "BU0": "沥青", "HC0": "热轧卷板", "SN0": "沪锡", "NI0": "沪镍",
    "SP0": "纸浆", "NR0": "20号胶", "SS0": "不锈钢", "LU0": "低硫燃料油",
    "BC0": "国际铜", "AO0": "氧化铝", "BR0": "丁二烯橡胶", "EC0": "集运指数欧线",
    "AD0": "铸造铝合金", "OP0": "胶版印刷纸",
    # 中金所 cffex
    "IF0": "沪深300指数", "IH0": "上证50指数", "IC0": "中证500指数",
    "IM0": "中证1000指数", "TF0": "5年期国债", "TS0": "2年期国债",
    "T0": "10年期国债", "TL0": "30年期国债",
    # 广期所 gfex
    "SI0": "工业硅", "LC0": "碳酸锂", "PS0": "多晶硅", "PT0": "铂", "PD0": "钯",
}

# 上金所贵金属现货
SGE_SYMBOLS = ["Au99.99", "Au99.95", "Au100g", "Au(T+D)", "Ag(T+D)", "mAu(T+D)", "Ag99.99", "Pt99.95"]


def _data_dir() -> Path:
    env_val = os.getenv(_FUTURES_DATA_DIR_ENV, "").strip()
    if env_val:
        p = Path(env_val)
        p.mkdir(parents=True, exist_ok=True)
        return p
    for d in _FUTURES_DEFAULT_DIRS:
        p = Path(d)
        if p.is_dir():
            return p
    p = Path(_FUTURES_DEFAULT_DIRS[-1])
    p.mkdir(parents=True, exist_ok=True)
    return p


def _write_kline_partition(root: Path, all_df: pd.DataFrame) -> int:
    """按交易日分区写入日K，增量合并去重。"""
    root.mkdir(parents=True, exist_ok=True)
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
    return written


# akshare 不同接口返回中英文两套列名，统一映射到标准列
_COL_MAP = {
    "日期": "time", "date": "time",
    "开盘价": "open", "open": "open",
    "最高价": "high", "high": "high",
    "最低价": "low", "low": "low",
    "收盘价": "close", "close": "close",
    "成交量": "volume", "volume": "volume",
    "持仓量": "position", "position": "position",
}


def _normalise_daily(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """日K标准化 → QuantDB kline schema（兼容 akshare 中英文列名）。"""
    df = df.rename(columns=_COL_MAP)
    df["time"] = pd.to_datetime(df["time"], errors="coerce")
    df = df.dropna(subset=["time"])
    df["symbol"] = symbol
    for c in ("open", "high", "low", "close"):
        if c not in df.columns:
            df[c] = None
        df[c] = pd.to_numeric(df[c], errors="coerce")
    # volume 别名处理
    if "volume" not in df.columns:
        if "position" in df.columns:
            df["volume"] = pd.to_numeric(df["position"], errors="coerce")
        else:
            df["volume"] = 0
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0).astype("int64")
    df["amount"] = pd.to_numeric(df["close"], errors="coerce") * pd.to_numeric(df["volume"], errors="coerce").fillna(0.0)
    df["release_id"] = "akshare"
    df["published_at"] = datetime.now().isoformat(timespec="seconds")
    return df[KLINE_COLS].dropna(subset=["close"])


def sync_foreign_realtime(symbols: list[str] | None = None) -> dict:
    """国际期货实时行情。"""
    import akshare as ak

    syms = symbols or list(FOREIGN_SYMBOLS.keys())
    root = _data_dir() / "2_base_sector" / "futures_realtime"
    root.mkdir(parents=True, exist_ok=True)

    ok = 0
    err = 0
    for code in syms:
        try:
            df = ak.futures_foreign_commodity_realtime(symbol=code)
            if df is None or df.empty:
                err += 1
                continue
            df = df.copy()
            df["symbol"] = f"{code}.FUT"
            df["release_id"] = "akshare"
            df["published_at"] = datetime.now().isoformat(timespec="seconds")
            df.to_parquet(root / f"{code}.FUT.parquet", index=False)
            ok += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("国际实时 %s 失败: %s", code, exc)
            err += 1

    return {"field": "foreign_realtime", "symbols": len(syms), "ok": ok, "err": err, "dir": str(root)}


def sync_foreign_daily(symbols: list[str] | None = None) -> dict:
    """国际期货日K（主力连续）。"""
    import akshare as ak

    syms = symbols or list(FOREIGN_SYMBOLS.keys())
    root = _data_dir() / "1_kline_data" / "daily_forward"

    frames = []
    for code in syms:
        try:
            df = ak.futures_foreign_hist(symbol=code)
            if df is None or df.empty:
                continue
            frames.append(_normalise_daily(df, f"{code}.FUT"))
            log.info("国际日K %s -> %s.FUT: %d 行", code, code, len(df))
        except Exception as exc:  # noqa: BLE001
            log.warning("国际日K %s 失败: %s", code, exc)

    if not frames:
        return {"field": "foreign_daily", "rows": 0}
    all_df = pd.concat(frames, ignore_index=True)
    written = _write_kline_partition(root, all_df)
    return {"field": "foreign_daily", "rows": int(len(all_df)), "partitions": written}


def sync_cn_realtime(symbols: list[str] | None = None) -> dict:
    """国内期货实时行情（futures_comm_info）。"""
    import akshare as ak

    root = _data_dir() / "2_base_sector" / "futures_realtime"
    root.mkdir(parents=True, exist_ok=True)

    try:
        df = ak.futures_comm_info()
        if df is None or df.empty:
            return {"field": "cn_realtime", "rows": 0}
        df = df.copy()
        # 合约代码在「合约代码」列，作为 symbol
        df = df.rename(columns={"合约代码": "symbol"})
        df["release_id"] = "akshare"
        df["published_at"] = datetime.now().isoformat(timespec="seconds")
        df.to_parquet(root / "cn_all.parquet", index=False)
        return {"field": "cn_realtime", "rows": int(len(df)), "dir": str(root)}
    except Exception as exc:  # noqa: BLE001
        log.warning("国内实时行情失败: %s", exc)
        return {"field": "cn_realtime", "error": str(exc)}


def sync_cn_daily(symbols: list[str] | None = None) -> dict:
    """国内主力连续日K。"""
    import akshare as ak

    syms = symbols or list(CN_MAIN.keys())
    root = _data_dir() / "1_kline_data" / "daily_forward"

    frames = []
    for code in syms:
        try:
            df = ak.futures_main_sina(symbol=code)
            if df is None or df.empty:
                continue
            frames.append(_normalise_daily(df, f"{code}.CN"))
            log.info("国内主力 %s -> %s.CN: %d 行", code, code, len(df))
        except Exception as exc:  # noqa: BLE001
            log.warning("国内主力 %s 失败: %s", code, exc)

    if not frames:
        return {"field": "cn_daily", "rows": 0}
    all_df = pd.concat(frames, ignore_index=True)
    written = _write_kline_partition(root, all_df)
    return {"field": "cn_daily", "rows": int(len(all_df)), "partitions": written}


def sync_sge_daily(symbols: list[str] | None = None) -> dict:
    """上金所贵金属现货日K。"""
    import akshare as ak

    syms = symbols or SGE_SYMBOLS
    root = _data_dir() / "1_kline_data" / "daily_forward"

    frames = []
    for sym in syms:
        try:
            df = ak.spot_hist_sge(symbol=sym)
            if df is None or df.empty:
                continue
            frames.append(_normalise_daily(df, sym))
            log.info("上金所 %s: %d 行", sym, len(df))
        except Exception as exc:  # noqa: BLE001
            log.warning("上金所 %s 失败: %s", sym, exc)

    if not frames:
        return {"field": "sge_daily", "rows": 0}
    all_df = pd.concat(frames, ignore_index=True)
    written = _write_kline_partition(root, all_df)
    return {"field": "sge_daily", "rows": int(len(all_df)), "partitions": written}


FIELDS = {
    "foreign_realtime": sync_foreign_realtime,
    "foreign_daily": sync_foreign_daily,
    "cn_realtime": sync_cn_realtime,
    "cn_daily": sync_cn_daily,
    "sge_daily": sync_sge_daily,
}


def run(*, field: str = "all", symbols: str | None = None, **kwargs) -> dict:
    """供后台管理 API 调用的编程接口。"""
    return sync(field, symbols=symbols)


def sync(field: str = "all", *, symbols: str | None = None) -> dict:
    """同步指定期货数据段。"""
    if field not in FIELDS and field != "all":
        raise ValueError(f"field 必须是 {'/'.join(FIELDS)}/all，收到 {field}")

    syms = [s.strip() for s in symbols.split(",") if s.strip()] if symbols else None
    if field == "all":
        result = {}
        for name in FIELDS:
            result[name] = FIELDS[name](syms)
        return result
    return {field: FIELDS[field](syms)}


def main() -> int:
    parser = argparse.ArgumentParser(description="akshare 期货数据 → QuantFutures")
    parser.add_argument("--field", default="all", choices=[*FIELDS.keys(), "all"], help="数据段")
    parser.add_argument("--symbols", default=None, help="指定品种代码，逗号分隔")
    args = parser.parse_args()

    try:
        result = sync(args.field, symbols=args.symbols)
        print(result)
        return 0
    except Exception as exc:  # noqa: BLE001
        log.error("同步失败: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
