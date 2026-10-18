#!/usr/bin/env python3
"""Blockchain (QuantBC) full sync — 区块链/加密货币本地数据摄取核心。

从 Binance 公开 API 拉取加密货币日线 K 线，按 QuantDB 的格式落盘本地 parquet。
复用 rd_agent data_pipeline 的 Binance 下载逻辑（无需翻墙的 data-api.binance.vision）。

数据段:
  1. daily_kline  — 日线 OHLCV → 1_kline_data/daily_forward/dt=YYYYMMDD/
  2. valuation    — 估值快照 → 5_technical_derived/valuation/dt=YYYYMMDD/

标的池:
  复用 crypto_data 的 top-35 币种交易对（BTCUSDT/ETHUSDT/...）。

用法:
  # 全量同步最近 365 个自然日
  python backend/scripts/blockchain_sync.py --days 365

  # 指定交易对
  python backend/scripts/blockchain_sync.py --symbols BTCUSDT,ETHUSDT --days 30

数据目录:
  QM_QUANTBC_DATA_DIR  (默认 data/quantbc/)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("blockchain_sync")

_QUANTBC_DATA_DIR_ENV = "QM_QUANTBC_DATA_DIR"
_QUANTBC_DEFAULT_DIR = "/data/quantbc"
_QUANTBC_LOCAL_DIR = str(PROJECT_ROOT / "data" / "quantbc")

# QuantDB 日线 schema（10 列，与其他市场一致）
KLINE_COLS = ["symbol", "time", "open", "high", "low", "close", "volume", "amount", "release_id", "published_at"]

# 默认交易对 (top-35 by volume, 与 rd_agent crypto_data 一致)
DEFAULT_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "DOTUSDT", "LINKUSDT",
    "MATICUSDT", "LTCUSDT", "UNIUSDT", "ATOMUSDT", "ETCUSDT",
    "FILUSDT", "APTUSDT", "NEARUSDT", "ARBUSDT", "OPUSDT",
    "MKRUSDT", "AAVEUSDT", "GRTUSDT", "INJUSDT", "SEIUSDT",
    "TIAUSDT", "SUIUSDT", "RUNEUSDT", "FETUSDT", "RENDERUSDT",
    "PEPEUSDT", "SHIBUSDT", "FLOKIUSDT", "WIFUSDT", "BONKUSDT",
]


def _data_dir() -> Path:
    env_val = os.getenv(_QUANTBC_DATA_DIR_ENV, "").strip()
    if env_val:
        return Path(env_val)
    container_dir = Path(_QUANTBC_DEFAULT_DIR)
    if container_dir.is_dir():
        return container_dir
    local_dir = Path(_QUANTBC_LOCAL_DIR)
    local_dir.mkdir(parents=True, exist_ok=True)
    return local_dir


def _fetch_binance_daily(symbol: str, start: date, end: date) -> pd.DataFrame | None:
    """从 Binance 公开 API 拉取单个交易对日线。

    Returns:
        DataFrame: datetime/open/high/low/close/volume/instrument，失败返回 None
    """
    from backend.services.engine.rd_agent.data_pipeline.crypto_data import (
        download_binance_klines,
    )

    try:
        df = download_binance_klines(
            symbol,
            interval="1d",
            start_date=start.strftime("%Y-%m-%d"),
            end_date=end.strftime("%Y-%m-%d"),
        )
        if df is None or df.empty:
            return None
        return df
    except Exception as exc:  # noqa: BLE001
        log.warning("拉取 %s 日线失败: %s", symbol, exc)
        return None


def _fetch_binance_minute(symbol: str, interval: str, start: date, end: date) -> pd.DataFrame | None:
    """从 Binance 公开 API 拉取单个交易对分钟 K 线。

    Args:
        interval: "5m" 或 "1m"

    Returns:
        DataFrame: datetime/open/high/low/close/volume/instrument，失败返回 None
    """
    from backend.services.engine.rd_agent.data_pipeline.crypto_data import (
        download_binance_klines,
    )

    try:
        df = download_binance_klines(
            symbol,
            interval=interval,
            start_date=start.strftime("%Y-%m-%d"),
            end_date=end.strftime("%Y-%m-%d"),
        )
        if df is None or df.empty:
            return None
        return df
    except Exception as exc:  # noqa: BLE001
        log.warning("拉取 %s %s 失败: %s", symbol, interval, exc)
        return None


def _normalise_kline(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Binance 输出 → QuantDB 日线 schema。"""
    out = pd.DataFrame({
        "symbol": symbol,
        "time": pd.to_datetime(df["datetime"]).dt.date,
        "open": pd.to_numeric(df["open"], errors="coerce"),
        "high": pd.to_numeric(df["high"], errors="coerce"),
        "low": pd.to_numeric(df["low"], errors="coerce"),
        "close": pd.to_numeric(df["close"], errors="coerce"),
        "volume": pd.to_numeric(df["volume"], errors="coerce").fillna(0.0),
        "amount": pd.to_numeric(df["close"], errors="coerce")
        * pd.to_numeric(df["volume"], errors="coerce").fillna(0.0),
    })
    out["release_id"] = "binance"
    out["published_at"] = datetime.now().isoformat(timespec="seconds")
    return out[KLINE_COLS].dropna(subset=["close"])


def _normalise_minute_kline(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """Binance 分钟 K 线 → QuantDB 分钟 schema。

    与日线不同：time 保留完整时间戳（datetime），否则同日多根 K 线会被去重。
    """
    out = pd.DataFrame({
        "symbol": symbol,
        "time": pd.to_datetime(df["datetime"]),
        "open": pd.to_numeric(df["open"], errors="coerce"),
        "high": pd.to_numeric(df["high"], errors="coerce"),
        "low": pd.to_numeric(df["low"], errors="coerce"),
        "close": pd.to_numeric(df["close"], errors="coerce"),
        "volume": pd.to_numeric(df["volume"], errors="coerce").fillna(0.0),
        "amount": pd.to_numeric(df["close"], errors="coerce")
        * pd.to_numeric(df["volume"], errors="coerce").fillna(0.0),
    })
    out["release_id"] = "binance"
    out["published_at"] = datetime.now().isoformat(timespec="seconds")
    return out[KLINE_COLS].dropna(subset=["close"])


def _write_partition(root: Path, date_str: str, chunk: pd.DataFrame) -> Path:
    """写单个 Hive 分区 dt=YYYYMMDD/data.parquet（增量追加去重）。"""
    dt_dir = root / f"dt={date_str}"
    dt_dir.mkdir(parents=True, exist_ok=True)
    target = dt_dir / "data.parquet"
    if target.exists():
        old = pd.read_parquet(target)
        combined = pd.concat([old, chunk], ignore_index=True)
        combined = combined.drop_duplicates(subset=["symbol", "time"], keep="last")
        combined.to_parquet(target, index=False)
    else:
        chunk.to_parquet(target, index=False)
    return target


def sync_kline(symbols: list[str], start: date, end: date) -> dict:
    """拉取日线并按交易日分区落盘。"""
    root = _data_dir() / "1_kline_data" / "daily_forward"
    root.mkdir(parents=True, exist_ok=True)

    frames = []
    errors = 0
    for symbol in symbols:
        df = _fetch_binance_daily(symbol, start, end)
        if df is not None and not df.empty:
            frames.append(_normalise_kline(df, symbol))
        else:
            errors += 1

    if not frames:
        return {"symbols": len(symbols), "rows": 0, "partitions": 0, "errors": errors}

    all_df = pd.concat(frames, ignore_index=True)
    grouped = {ts.strftime("%Y%m%d"): g for ts, g in all_df.groupby(all_df["time"])}
    written = 0
    for date_str, chunk in sorted(grouped.items()):
        _write_partition(root, date_str, chunk)
        written += 1

    return {
        "symbols": len(symbols),
        "rows": int(len(all_df)),
        "partitions": written,
        "errors": errors,
        "start_date": min(grouped),
        "end_date": max(grouped),
    }


def sync_minute_kline(symbols: list[str], start: date, end: date, *, freq: str = "5m") -> dict:
    """拉取分钟 K 线并按标的落盘（每标的一文件 {symbol}.parquet，增量去重）。

    Args:
        freq: "5m" 或 "1m"，对应落盘目录 min5_kline / min1_kline
    """
    interval_map = {"5m": "5m", "1m": "1m"}
    subdir_map = {"5m": "min5_kline", "1m": "min1_kline"}
    interval = interval_map.get(freq)
    if interval is None:
        raise ValueError(f"freq 必须是 5m/1m，收到 {freq}")

    root = _data_dir() / "1_kline_data" / subdir_map[freq]
    root.mkdir(parents=True, exist_ok=True)

    written = 0
    errors = 0
    total_rows = 0
    for symbol in symbols:
        df = _fetch_binance_minute(symbol, interval, start, end)
        if df is None or df.empty:
            errors += 1
            continue
        try:
            norm = _normalise_minute_kline(df, symbol)
            _write_symbol_file(root, symbol, norm)
            written += 1
            total_rows += len(norm)
        except Exception as exc:  # noqa: BLE001
            log.warning("写入 %s %s 失败: %s", symbol, freq, exc)
            errors += 1

    return {
        "freq": freq,
        "symbols": len(symbols),
        "written": written,
        "errors": errors,
        "rows": total_rows,
        "dir": subdir_map[freq],
        "start_date": start.strftime("%Y%m%d"),
        "end_date": end.strftime("%Y%m%d"),
    }


def _write_symbol_file(root: Path, symbol: str, df: pd.DataFrame) -> Path:
    """写标的级分钟 K 线文件 {root}/{symbol}.parquet，增量追加去重。"""
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{symbol}.parquet"
    if target.exists():
        old = pd.read_parquet(target)
        combined = pd.concat([old, df], ignore_index=True)
        combined = combined.drop_duplicates(subset=["symbol", "time"], keep="last")
        combined.to_parquet(target, index=False)
    else:
        df.to_parquet(target, index=False)
    return target


def sync_valuation(symbols: list[str], end: date) -> dict:
    """拉取币种当前估值快照（close×total supply 近似市值），按日落盘。"""
    from backend.services.engine.rd_agent.data_pipeline.crypto_data import (
        download_binance_klines,
    )

    root = _data_dir() / "5_technical_derived" / "valuation"
    root.mkdir(parents=True, exist_ok=True)

    frames = []
    errors = 0
    for symbol in symbols:
        try:
            # 单日窗口因 endTime 排他会返回空，拉最近 7 天取最后一根已收盘 K 线
            start7 = end - timedelta(days=7)
            df = download_binance_klines(
                symbol,
                interval="1d",
                start_date=start7.strftime("%Y-%m-%d"),
                end_date=end.strftime("%Y-%m-%d"),
            )
            if df is None or df.empty:
                errors += 1
                continue
            last = df.iloc[-1]
            close = float(last["close"])
            row = {
                "symbol": symbol,
                "time": pd.Timestamp(last["datetime"]).date(),
                "close": close,
                "total_mv": None,  # Binance 不提供流通市值，仅记录收盘价
                "float_mv": None,
                "net_profit_ttm": None,
                "revenue_ttm": None,
                "pe_ttm": None,
                "pb": None,
                "ps_ttm": None,
                "release_id": "binance",
                "published_at": datetime.now().isoformat(timespec="seconds"),
            }
            frames.append(pd.DataFrame([row]))
        except Exception as exc:  # noqa: BLE001
            log.warning("拉取 %s 估值失败: %s", symbol, exc)
            errors += 1

    if not frames:
        return {"written": 0, "errors": errors}

    all_df = pd.concat(frames, ignore_index=True)
    max_date = all_df["time"].max().strftime("%Y%m%d")
    _write_partition(root, max_date, all_df)
    return {"written": len(all_df), "partition": max_date, "errors": errors}


def _calendar_days(end: date, n: int) -> list[date]:
    """取最近 n 个自然日。"""
    return [end - timedelta(days=i) for i in range(n - 1, -1, -1)]


def run(
    *,
    days: int = 365,
    symbols: str | None = None,
    skip_valuation: bool = False,
    minute_freqs: tuple[str, ...] | None = None,
    minute_days: int | None = None,
) -> dict:
    """全量同步：日线 + 分钟线 + 估值快照。

    Args:
        days: 同步最近多少个自然日（日线，加密市场 7×24 无交易日概念）
        symbols: 指定交易对（默认 top-35）
        skip_valuation: 跳过估值快照
        minute_freqs: 需要同步的分钟频率（"5m"/"1m"），默认不同步分钟
        minute_days: 分钟数据拉取多少天（默认取 min(days, 90)，Binance 分钟拉取量大）
    """
    syms = [s.strip().upper() for s in symbols.split(",") if s.strip()] if symbols else list(DEFAULT_SYMBOLS)
    end = date.today()
    start = min(_calendar_days(end, days))
    log.info("同步 %d 个交易对，%s ~ %s", len(syms), start, end)

    result: dict = {"symbols": len(syms)}
    kline = sync_kline(syms, start, end)
    result["kline"] = kline
    log.info("K线同步完成: %s", kline)

    # 分钟线：单独的时间范围（默认最近 90 天），避免 5m/1m 数据量过大
    if minute_freqs:
        mdays = minute_days or min(days, 90)
        mstart = min(_calendar_days(end, mdays))
        result["minute"] = {}
        for freq in minute_freqs:
            m = sync_minute_kline(syms, mstart, end, freq=freq)
            result["minute"][freq] = m
            log.info("%s 分钟线同步完成: %s", freq, m)

    if not skip_valuation:
        valuation = sync_valuation(syms, end)
        result["valuation"] = valuation
        log.info("估值快照完成: %s", valuation)

    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="QuantBC 区块链/加密货币数据摄取")
    parser.add_argument("--days", type=int, default=365, help="同步最近多少个自然日（日线）")
    parser.add_argument("--symbols", default=None, help="逗号分隔交易对（默认 top-35）")
    parser.add_argument("--skip-valuation", action="store_true", help="跳过估值快照")
    parser.add_argument("--minute", action="store_true", help="同步分钟线（5m+1m，最近 90 天）")
    parser.add_argument("--minute-days", type=int, default=None, help="分钟线拉取天数（默认 min(days,90)）")
    args = parser.parse_args()

    try:
        minute_freqs = ("5m", "1m") if args.minute else None
        result = run(
            days=args.days,
            symbols=args.symbols,
            skip_valuation=args.skip_valuation,
            minute_freqs=minute_freqs,
            minute_days=args.minute_days,
        )
        print(result)
        return 0
    except Exception as exc:  # noqa: BLE001
        log.error("同步失败: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
