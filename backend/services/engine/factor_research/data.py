"""因子研究模块 —— QuantDB 数据加载层。

本层只负责把 QuantDB parquet 读成宽表面板（index=交易日, columns=symbol），
供 engine / financials 计算因子；不做任何因子语义。
所有路径经 ``backend.shared.quantdb_paths`` 解析（禁硬编码）。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from backend.shared.quantdb_paths import resolve_quantdb_subdir

logger = logging.getLogger(__name__)

# 数据目录（相对 QuantDB 根）
DAILY_PARTS = ("1_kline_data", "daily_forward")
INDEX_PARTS = ("1_kline_data", "index_daily")
VALUATION_PARTS = ("5_technical_derived", "valuation")
INSTRUMENT_PARTS = ("2_base_sector", "instrument_detail")
INCOME_PARTS = ("3_financial_data", "income")
BALANCE_PARTS = ("3_financial_data", "balance")
CASHFLOW_PARTS = ("3_financial_data", "cashflow")
PERSHARE_PARTS = ("3_financial_data", "pershare_index")
HOLDER_PARTS = ("3_financial_data", "holder_num")

# 基准指数（demo 口径用中证500；回归市场组合同样用它做基准）
BENCHMARK_SYMBOL = "000905.SH"


def _partition_files(
    dir_parts: tuple[str, ...], start: str | None, end: str | None
) -> list[Path]:
    """列出 dt=YYYYMMDD 分区目录（按 dt 过滤，升序）。"""
    root = resolve_quantdb_subdir(*dir_parts)
    out = []
    for p in sorted(root.glob("dt=*")):
        if not p.is_dir():
            continue
        dt = p.name[3:]
        if start and dt < start:
            continue
        if end and dt > end:
            continue
        f = p / "data.parquet"
        if f.exists():
            out.append(f)
    return out


def _read_files(files: list[Path], columns: list[str] | None = None) -> pd.DataFrame:
    parts = []
    for f in files:
        try:
            df = pd.read_parquet(f, columns=columns)
        except Exception as e:  # 单分区损坏不拖垮全量（记日志跳过）
            logger.warning("读取失败跳过 %s: %s", f, e)
            continue
        parts.append(df)
    if not parts:
        return pd.DataFrame()
    return pd.concat(parts, ignore_index=True)


def load_daily_panel(
    start: str,
    end: str,
    fields: tuple[str, ...] = ("open", "high", "low", "close", "volume", "amount"),
) -> dict[str, pd.DataFrame]:
    """读取 daily_forward → {字段: 宽表(交易日 × symbol)}。前复权价格 + 股/万元。"""
    files = _partition_files(DAILY_PARTS, start, end)
    if not files:
        raise FileNotFoundError(f"daily_forward 无分区: {start}~{end}")
    cols = ["time", "symbol", *fields]
    df = _read_files(files, columns=cols)
    df["symbol"] = df["symbol"].astype("category")
    out: dict[str, pd.DataFrame] = {}
    for fld in fields:
        out[fld] = df.pivot_table(
            index="time", columns="symbol", values=fld, aggfunc="last"
        )
    for fld, df_ in list(out.items()):
        df_.index = pd.to_datetime(df_.index, format="%Y%m%d")
        df_.sort_index(inplace=True)
        if df_.index.has_duplicates:  # 分区 time 格式不一致时 to_datetime 可能撞出重复
            logger.warning("daily_forward 索引存在重复日期，已去重（keep=last）")
            out[fld] = df_[~df_.index.duplicated(keep="last")]
    logger.info(
        "daily_forward 载入 %d 交易日 × %d 只 (%s~%s)",
        len(out["close"]),
        out["close"].shape[1],
        start,
        end,
    )
    return out


def load_valuation_panel(
    start: str,
    end: str,
    fields: tuple[str, ...] = (
        "close",
        "total_capital",
        "circulating_capital",
        "total_mv",
        "float_mv",
        "net_profit_ttm",
        "revenue_ttm",
        "equity",
        "pe_ttm",
        "pb",
        "ps_ttm",
        "dividend_rate",
    ),
) -> dict[str, pd.DataFrame]:
    """读取 valuation → {字段: 宽表}。total_mv/float_mv 单位元。"""
    files = _partition_files(VALUATION_PARTS, start, end)
    if not files:
        raise FileNotFoundError(f"valuation 无分区: {start}~{end}")
    cols = ["time", "symbol", *fields]
    df = _read_files(files, columns=cols)
    df["symbol"] = df["symbol"].astype("category")
    out: dict[str, pd.DataFrame] = {}
    for fld in fields:
        out[fld] = df.pivot_table(
            index="time", columns="symbol", values=fld, aggfunc="last"
        )
    for fld, df_ in list(out.items()):
        df_.index = pd.to_datetime(df_.index, format="%Y%m%d")
        df_.sort_index(inplace=True)
        if df_.index.has_duplicates:  # 同上：防御分区 time 格式不一致
            logger.warning("valuation 索引存在重复日期，已去重（keep=last）")
            out[fld] = df_[~df_.index.duplicated(keep="last")]
    return out


def load_index_close(start: str, end: str, symbol: str = BENCHMARK_SYMBOL) -> pd.Series:
    """读取指数收盘价序列（默认中证500）。"""
    files = _partition_files(INDEX_PARTS, start, end)
    df = _read_files(files, columns=["time", "symbol", "close"])
    if df.empty:
        return pd.Series(dtype=float)
    df = df[df["symbol"] == symbol]
    s = df.set_index("time")["close"].sort_index()
    s.index = pd.to_datetime(s.index, format="%Y%m%d")
    return s.astype(float)


def load_instrument() -> pd.DataFrame:
    """读取证券主表快照（ST/退市/行业/名称）。"""
    root = resolve_quantdb_subdir(*INSTRUMENT_PARTS)
    f = root / "instrument_detail.parquet"
    df = pd.read_parquet(
        f,
        columns=["Symbol", "Name", "IsSTGP", "IsQuitGP", "rs_hycode_sim", "rs_hyname"],
    )
    df = df.rename(
        columns={
            "Symbol": "symbol",
            "Name": "name",
            "IsSTGP": "is_st",
            "IsQuitGP": "is_quit",
            "rs_hycode_sim": "industry_code",
            "rs_hyname": "industry",
        }
    )
    return df


def month_end_dates(index: pd.DatetimeIndex) -> list[pd.Timestamp]:
    """月度调仓采样日 = 每月最后交易日；末尾追加最新交易日（当期快照）。"""
    s = pd.Series(index, index=index)
    me = s.groupby([index.year, index.month]).last().tolist()
    dates = [pd.Timestamp(d) for d in me]
    if dates and dates[-1] != index[-1]:
        dates.append(index[-1])
    return dates


def build_universe(
    instr: pd.DataFrame, close: pd.DataFrame, min_hist: int = 120
) -> pd.DataFrame:
    """股票池布尔掩码（交易日 × symbol）。

    规则：非 ST、非退市（instrument 快照，非 PIT——demo 同口径）；
    当日有价格、近 min_hist 日有效历史 ≥ min_hist*0.6（滤新股/长停）。
    """
    syms = close.columns
    meta = instr.set_index("symbol").reindex(syms)
    base_ok = (
        ((meta["is_st"].astype(str) == "0") & (meta["is_quit"].astype(str) == "0"))
        .reindex(syms)
        .fillna(False)
    )
    valid = close.notna()
    hist = valid.rolling(min_hist, min_periods=min_hist).sum() >= int(min_hist * 0.6)
    mask = hist.copy()
    for col in mask.columns:
        if not bool(base_ok.get(col, False)):
            mask[col] = False
    return mask


def industry_map(instr: pd.DataFrame) -> pd.Series:
    """symbol → 行业名（申万细分）。"""
    return instr.set_index("symbol")["industry"].reindex(sorted(set(instr["symbol"])))
