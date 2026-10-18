#!/usr/bin/env python3
"""区块链数据同步入口 — 套用 QuantDB 同步流程，从 Binance 拉取并落盘 parquet。

用法:
  python backend/scripts/quantbc_daily_sync.py --days 365
  python backend/scripts/quantbc_daily_sync.py --symbols BTCUSDT,ETHUSDT --days 30
  python backend/scripts/quantbc_daily_sync.py --minute          # 额外同步 5m+1m 分钟线
"""

from __future__ import annotations

import sys

from backend.scripts.blockchain_sync import run as _blockchain_run


def run(
    *,
    days: int = 365,
    symbols: str | None = None,
    skip_valuation: bool = False,
    minute_freqs: tuple[str, ...] | None = None,
    minute_days: int | None = None,
) -> dict:
    """供后台管理 API 调用的编程接口。"""
    result = _blockchain_run(
        days=days,
        symbols=symbols,
        skip_valuation=skip_valuation,
        minute_freqs=minute_freqs,
        minute_days=minute_days,
    )
    if not isinstance(result, dict):
        result = {"result": result}
    # L1 因子直读数据集随日K落盘后刷新（与港股同口径）
    try:
        from backend.scripts.build_ml_l1_dataset import build_l1

        result["l1_dataset"] = build_l1("crypto")
    except Exception as exc:  # noqa: BLE001
        result["l1_dataset"] = {"status": "error", "error": str(exc)}
    return result


def _cli() -> int:
    from backend.scripts.blockchain_sync import main

    return main()


if __name__ == "__main__":
    sys.exit(_cli())
