#!/usr/bin/env python3
"""QuantUS/QuantHK 限流恢复后自动补拉脚本。

yahoo 对高频请求限流（Too Many Requests），窗口通常 30-60 分钟。
本脚本：
  1. 定期探测 yahoo 是否恢复（试拉一个字段）
  2. 恢复后自动补拉缺失的数据段（--skip-kline，日线已完整）
  3. 完成后写标记文件

用法:
  python backend/scripts/global_market_sync_retry.py --market US
  python backend/scripts/global_market_sync_retry.py --market HK --probe-only
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("global_market_sync_retry")

PROBE_INTERVAL = 300  # 每 5 分钟探测一次
PROBE_SYMBOL = {"US": "AAPL", "HK": "0700.HK"}
RETRY_MARKER = "retry.done"  # 数据目录下的完成标记


def probe_yahoo(market: str) -> bool:
    """探测 yahoo 是否已恢复限流（试拉一个标的日线）。"""
    from backend.services.engine.data_platform.adapters.yahoo_finance_adapter import (
        YahooFinanceAdapter,
    )

    symbol = PROBE_SYMBOL[market]
    try:
        adapter = YahooFinanceAdapter()
        df = adapter.fetch_daily(symbol, date(2026, 8, 3), date(2026, 8, 7))
        if df is not None and not df.empty:
            log.info("yahoo 已恢复，probe %s 返回 %d 行", symbol, len(df))
            return True
    except Exception as exc:  # noqa: BLE001
        log.info("yahoo 仍限流 (%s)，%.0f 秒后再试", str(exc)[:50], PROBE_INTERVAL)
    return False


def wait_for_recovery(market: str, timeout_hours: float = 3.0) -> bool:
    """等待 yahoo 限流恢复，超时返回 False。"""
    deadline = time.time() + timeout_hours * 3600
    while time.time() < deadline:
        if probe_yahoo(market):
            return True
        time.sleep(PROBE_INTERVAL)
    log.error("等待 %.1f 小时 yahoo 仍未恢复", timeout_hours)
    return False


def run_retry(market: str, *, wait: bool = True, timeout_hours: float = 3.0) -> int:
    """等待限流恢复后补拉缺失数据段。"""
    from backend.scripts.global_market_sync import _data_dir

    market = market.upper()
    data_root = _data_dir(market)

    if wait:
        log.info("[%s] 等待 yahoo 限流恢复...", market)
        if not wait_for_recovery(market, timeout_hours):
            return 1

    log.info("[%s] 限流已恢复，开始补拉数据段（跳过日线）", market)
    from backend.scripts.global_market_sync import run

    result = run(market, days=5, skip_kline=True)
    log.info("[%s] 补拉完成: %s", market, result)

    # 写完成标记
    try:
        (data_root / RETRY_MARKER).write_text(
            f"completed at {time.strftime('%Y-%m-%d %H:%M:%S')}\n", encoding="utf-8"
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("写完成标记失败: %s", exc)

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="QuantUS/QuantHK 限流后补拉")
    parser.add_argument("--market", required=True, choices=["US", "HK"])
    parser.add_argument("--no-wait", action="store_true", help="不等待，立即补拉（已恢复时用）")
    parser.add_argument("--timeout-hours", type=float, default=3.0, help="等待恢复的最大时长")
    args = parser.parse_args()

    return run_retry(args.market, wait=not args.no_wait, timeout_hours=args.timeout_hours)


if __name__ == "__main__":
    sys.exit(main())
