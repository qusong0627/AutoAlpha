#!/usr/bin/env python3
"""万得(Wind) L2 数据导入 — 将逐笔行情/委托/成交导入 QuantDB 本地 parquet。

数据源: 万得 L2 7z 压缩包（20260511.7z），全市场按股票分目录，每只股票含
  行情.csv      — 十档盘口快照（66 列，价格×10000）
  逐笔委托.csv   — 逐笔委托明细
  逐笔成交.csv   — 逐笔成交明细

落盘 (data/quantdb/):
  1_kline_data/tick_data/{symbol}_{YYYYMMDD}.parquet       — 行情快照（对齐 QuantDB tick 格式，10 档）
  1_kline_data/l2_data/order_{symbol}_{YYYYMMDD}.parquet   — 逐笔委托
  1_kline_data/l2_data/trade_{symbol}_{YYYYMMDD}.parquet   — 逐笔成交

策略:
  - 流式解压：逐股票目录从 7z 提取 → 导入 → 删除临时 csv，避免一次性解压全量
  - 增量：已存在的 {symbol}_{date}.parquet 跳过（可用 --force 覆盖）
  - 可断点续跑：跳过已处理标的

用法:
  python backend/scripts/wind_l2_import.py --archive /media/.../20260511.7z
  python backend/scripts/wind_l2_import.py --archive ... --limit 50          # 只导前 50 只
  python backend/scripts/wind_l2_import.py --archive ... --symbols 000001.SZ,600519.SH
  python backend/scripts/wind_l2_import.py --archive ... --force            # 覆盖已存在
  python backend/scripts/wind_l2_import.py --archive ... --temp /tmp/l2x    # 解压临时目录
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("wind_l2_import")

# 数据目录（容器内 /data/quantdb，本地 data/quantdb）
_DEFAULT_DATA_DIRS = ["/data/quantdb", str(PROJECT_ROOT / "data" / "quantdb")]

# 万得 L2 三类文件名
QUOTE_FILE = "行情.csv"
ORDER_FILE = "逐笔委托.csv"
TRADE_FILE = "逐笔成交.csv"

# 价格精度（万得 int 表示 ×10000）
PRICE_SCALE = 10000

# 单只股票导入超时（秒）
STOCK_TIMEOUT = 300


def _data_dir() -> Path:
    env = os.getenv("QM_QUANTDB_DATA_DIR", "").strip()
    if env:
        return Path(env)
    for d in _DEFAULT_DATA_DIRS:
        p = Path(d)
        if p.is_dir():
            return p
    p = Path(_DEFAULT_DATA_DIRS[-1])
    p.mkdir(parents=True, exist_ok=True)
    return p


def _wind_time_to_ms(t: int, date_str: str) -> int:
    """万得 hhmmssmmm → 完整 UTC 毫秒时间戳（对齐 QuantDB tick 格式）。

    WD 时间 91500000 = 09:15:00.000（北京时间）。QuantDB tick 用 UTC 毫秒
    完整时间戳（如 1784510100000）。基准 = 当日 UTC 00:00 + 北京日内偏移。
    """
    s = str(int(t)).zfill(9)
    hh, mm, ss, mmm = int(s[0:2]), int(s[2:4]), int(s[4:6]), int(s[6:9])
    # 北京时间 → UTC（-8 小时），以当日 UTC 00:00 为基准的毫秒
    bj_seconds = hh * 3600 + mm * 60 + ss
    utc_seconds = bj_seconds - 8 * 3600
    if utc_seconds < 0:
        utc_seconds += 24 * 3600
    # 当日 UTC 00:00 的毫秒时间戳（date_str = YYYYMMDD）
    base_ms = int(datetime(
        int(date_str[0:4]), int(date_str[4:6]), int(date_str[6:8]),
        tzinfo=timezone.utc,
    ).timestamp() * 1000)
    return base_ms + utc_seconds * 1000 + mmm


def _parse_quote_csv(path: Path, date_str: str) -> pd.DataFrame:
    """解析行情.csv → QuantDB tick 格式（10 档盘口）。"""
    df = pd.read_csv(path, encoding="gbk")

    out = pd.DataFrame()
    out["time"] = df["时间"].apply(_wind_time_to_ms, date_str=date_str).astype("int64")

    def to_price(col: str) -> pd.Series:
        return pd.to_numeric(df[col], errors="coerce") / PRICE_SCALE

    out["lastPrice"] = to_price("成交价")
    out["open"] = to_price("开盘价")
    out["high"] = to_price("最高价")
    out["low"] = to_price("最低价")
    out["lastClose"] = to_price("前收盘")
    out["amount"] = to_price("当日成交额")
    out["volume"] = pd.to_numeric(df["当日累计成交量"], errors="coerce").fillna(0).astype("int64")
    out["pvolume"] = pd.to_numeric(df["成交笔数"], errors="coerce").fillna(0).astype("int64")
    out["transactionNum"] = pd.to_numeric(df["成交笔数"], errors="coerce").fillna(0).astype("int64")
    out["stockStatus"] = pd.to_numeric(df["BS标志"], errors="coerce").fillna(0).astype("int32")
    out["openInt"] = 0
    out["lastSettlementPrice"] = 0.0
    out["settlementPrice"] = 0.0

    # 十档盘口（万得列名：申卖价1..10 / 申卖量1..10 / 申买价1..10 / 申买量1..10）
    def to_level(col: str) -> pd.Series:
        return pd.to_numeric(df[col], errors="coerce").fillna(0.0)

    def build_levels(prefix: str, suffix: str) -> np.ndarray:
        cols = [to_level(f"{prefix}{i}{suffix}").values for i in range(1, 11)]
        return np.stack(cols, axis=1)  # (rows, 10)

    out["askPrice"] = [row for row in build_levels("申卖价", "")]
    out["bidPrice"] = [row for row in build_levels("申买价", "")]
    out["askVol"] = [row for row in build_levels("申卖量", "")]
    out["bidVol"] = [row for row in build_levels("申买量", "")]

    # 归一价格档（×10000 → 元）
    for col in ("askPrice", "bidPrice"):
        out[col] = out[col].apply(lambda arr: np.round(np.asarray(arr, dtype="float64") / PRICE_SCALE, 4))
    for col in ("askVol", "bidVol"):
        out[col] = out[col].apply(lambda arr: np.asarray(arr, dtype="int64"))

    # 过滤掉无效快照（成交价为 0 且盘口全空的开盘前集合竞价段）
    out = out.dropna(subset=["lastPrice"])
    return out.sort_values("time").reset_index(drop=True)


def _parse_order_csv(path: Path, date_str: str) -> pd.DataFrame:
    """解析逐笔委托.csv。"""
    df = pd.read_csv(path, encoding="gbk")
    out = pd.DataFrame({
        "time": df["时间"].apply(_wind_time_to_ms, date_str=date_str).astype("int64"),
        "order_id": pd.to_numeric(df["交易所委托号"], errors="coerce").fillna(0).astype("int64"),
        "channel": df["委托编号"].astype(str),
        "order_type": df["委托类型"].astype(str),
        "direction": df["委托代码"].astype(str),
        "price": pd.to_numeric(df["委托价格"], errors="coerce") / PRICE_SCALE,
        "volume": pd.to_numeric(df["委托数量"], errors="coerce").fillna(0).astype("int64"),
    })
    return out.sort_values("time").reset_index(drop=True)


def _parse_trade_csv(path: Path, date_str: str) -> pd.DataFrame:
    """解析逐笔成交.csv。"""
    df = pd.read_csv(path, encoding="gbk")
    out = pd.DataFrame({
        "time": df["时间"].apply(_wind_time_to_ms, date_str=date_str).astype("int64"),
        "trade_id": pd.to_numeric(df["成交编号"], errors="coerce").fillna(0).astype("int64"),
        "trade_type": df["成交代码"].astype(str),
        "direction": df["BS标志"].astype(str),
        "price": pd.to_numeric(df["成交价格"], errors="coerce") / PRICE_SCALE,
        "volume": pd.to_numeric(df["成交数量"], errors="coerce").fillna(0).astype("int64"),
        "ask_order_id": pd.to_numeric(df["叫卖序号"], errors="coerce").fillna(0).astype("int64"),
        "bid_order_id": pd.to_numeric(df["叫买序号"], errors="coerce").fillna(0).astype("int64"),
    })
    return out.sort_values("time").reset_index(drop=True)


def _list_stocks_from_archive(archive: str, seven_zip: str = "7z") -> list[str]:
    """列出 7z 中所有股票目录（形如 20260511/000001.SZ）。"""
    proc = subprocess.run(
        [seven_zip, "l", archive],
        capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"7z list failed: {proc.stderr}")
    stocks: set[str] = set()
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and "/" in parts[-1]:
            rel = parts[-1]
            # 形如 20260511/000001.SZ
            if rel.count("/") == 1 and "." in rel:
                code = rel.split("/")[1]
                if code[:6].isdigit():
                    stocks.add(code)
    return sorted(stocks)


def _extract_stock(archive: str, date_str: str, code: str, temp_dir: Path, seven_zip: str) -> Path:
    """从 7z 提取单只股票目录到临时目录，返回目录路径。"""
    target = temp_dir / date_str / code
    if target.is_dir() and (target / QUOTE_FILE).exists():
        return target
    target.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [seven_zip, "x", f"-o{temp_dir}", archive, f"{date_str}/{code}/*", "-y"],
        capture_output=True, timeout=STOCK_TIMEOUT, check=True,
    )
    return target


def _import_one_stock(archive: str, date_str: str, code: str, temp_dir: Path, root: Path, force: bool, seven_zip: str) -> dict:
    """导入单只股票的三类 L2 数据。"""
    qdb_code = code.replace(".", "_")  # 000001.SZ -> 000001_SZ
    results: dict[str, str] = {}

    # 增量跳过：三类目标文件都已存在且非 force
    tick_path = root / "1_kline_data" / "tick_data" / f"{qdb_code}_{date_str}.parquet"
    order_path = root / "1_kline_data" / "l2_data" / f"order_{qdb_code}_{date_str}.parquet"
    trade_path = root / "1_kline_data" / "l2_data" / f"trade_{qdb_code}_{date_str}.parquet"
    if not force and tick_path.exists() and order_path.exists() and trade_path.exists():
        return {"symbol": code, "status": "skipped"}

    stock_dir = _extract_stock(archive, date_str, code, temp_dir, seven_zip)

    # 行情快照 → tick_data/
    qfile = stock_dir / QUOTE_FILE
    if qfile.exists():
        q = _parse_quote_csv(qfile, date_str)
        tick_path.parent.mkdir(parents=True, exist_ok=True)
        q.to_parquet(tick_path, index=False)
        results["quote"] = str(len(q))

    # 逐笔委托 → l2_data/order_
    ofile = stock_dir / ORDER_FILE
    if ofile.exists():
        o = _parse_order_csv(ofile, date_str)
        order_path.parent.mkdir(parents=True, exist_ok=True)
        o.to_parquet(order_path, index=False)
        results["order"] = str(len(o))

    # 逐笔成交 → l2_data/trade_
    tfile = stock_dir / TRADE_FILE
    if tfile.exists():
        t = _parse_trade_csv(tfile, date_str)
        trade_path.parent.mkdir(parents=True, exist_ok=True)
        t.to_parquet(trade_path, index=False)
        results["trade"] = str(len(t))

    # 清理当前股票的临时解压目录（只删本股票，不能删整个日期目录，
    # 否则并发 worker 会互相清空对方正在解压的文件）
    shutil.rmtree(stock_dir, ignore_errors=True)

    results["status"] = "ok"
    return {"symbol": code, **results}


def run(
    *,
    archive: str,
    limit: int | None = None,
    symbols: str | None = None,
    force: bool = False,
    temp_dir: str | None = None,
    workers: int = 4,
    seven_zip: str = "7z",
) -> dict:
    """全市场 L2 导入入口。

    Args:
        archive: 7z 文件路径
        limit: 只导前 N 只股票（调试）
        symbols: 逗号分隔指定股票
        force: 覆盖已存在文件
        temp_dir: 解压临时目录（默认 /tmp/wind_l2_import）
        workers: 并发线程数
    """
    root = _data_dir()
    root.mkdir(parents=True, exist_ok=True)
    tmp = Path(temp_dir or "/tmp/wind_l2_import")
    tmp.mkdir(parents=True, exist_ok=True)

    # 从文件名推断日期：20260511.7z -> 20260511
    date_str = Path(archive).stem
    if not (len(date_str) == 8 and date_str.isdigit()):
        raise ValueError(f"无法从文件名推断日期: {archive}")

    stocks = _list_stocks_from_archive(archive, seven_zip)
    if symbols:
        want = {s.strip().upper() for s in symbols.split(",") if s.strip()}
        stocks = [s for s in stocks if s in want]
    if limit:
        stocks = stocks[:limit]

    log.info("导入 %d 只股票 → %s (date=%s)", len(stocks), root, date_str)

    started = time.time()
    done = ok = skipped = failed = 0
    total_quote = total_order = total_trade = 0
    errors: list[dict] = []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_import_one_stock, archive, date_str, s, tmp, root, force, seven_zip): s
            for s in stocks
        }
        for future in as_completed(futures):
            s = futures[future]
            done += 1
            try:
                res = future.result()
                if res.get("status") == "skipped":
                    skipped += 1
                else:
                    ok += 1
                    total_quote += int(res.get("quote", 0))
                    total_order += int(res.get("order", 0))
                    total_trade += int(res.get("trade", 0))
                if done % 100 == 0 or done == len(stocks):
                    elapsed = time.time() - started
                    rate = done / elapsed if elapsed > 0 else 0
                    log.info(
                        "进度 %d/%d (ok=%d skip=%d fail=%d) %.1f/s, quote=%d order=%d trade=%d",
                        done, len(stocks), ok, skipped, failed, rate,
                        total_quote, total_order, total_trade,
                    )
            except Exception as exc:  # noqa: BLE001
                failed += 1
                log.error("导入 %s 失败: %s", s, exc)
                errors.append({"symbol": s, "error": str(exc)})

    elapsed = time.time() - started
    log.info(
        "完成: %d 只, ok=%d skipped=%d failed=%d, 耗时 %.1fs (%.1f/s)",
        done, ok, skipped, failed, elapsed, done / elapsed if elapsed > 0 else 0,
    )
    return {
        "archive": archive,
        "date": date_str,
        "data_dir": str(root),
        "total": len(stocks),
        "ok": ok,
        "skipped": skipped,
        "failed": failed,
        "rows": {"quote": total_quote, "order": total_order, "trade": total_trade},
        "errors": errors[:50],
        "elapsed_s": round(elapsed, 1),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="万得 L2 数据导入 QuantDB")
    parser.add_argument("--archive", required=True, help="7z 压缩包路径")
    parser.add_argument("--limit", type=int, default=None, help="只导前 N 只股票（调试）")
    parser.add_argument("--symbols", default=None, help="逗号分隔指定股票，如 000001.SZ,600519.SH")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的 parquet")
    parser.add_argument("--temp", default="/tmp/wind_l2_import", help="解压临时目录")
    parser.add_argument("--workers", type=int, default=4, help="并发线程数")
    parser.add_argument("--7z", dest="seven_zip", default="/opt/p7zip-legacy/bin/7z", help="7z 可执行路径")
    args = parser.parse_args()

    try:
        result = run(
            archive=args.archive,
            limit=args.limit,
            symbols=args.symbols,
            force=args.force,
            temp_dir=args.temp,
            workers=args.workers,
            seven_zip=args.seven_zip,
        )
        print(result)
        return 0
    except Exception as exc:  # noqa: BLE001
        log.error("导入失败: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
