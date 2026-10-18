#!/usr/bin/env python3
"""期货数据同步入口 — 按勾选数据集分发到 akshare 期货同步脚本。

支持的数据集（与后台 catalog 对应）:
  daily_forward     期货/贵金属日K  → foreign_daily + cn_daily + sge_daily
  futures_realtime  实时行情快照    → foreign_realtime + cn_realtime
  warehouse_receipts 交易所仓单     → akshare_futures_extra.task_receipts
  member_positions  会员持仓排名    → akshare_futures_extra.task_member_positions
  contracts_daily   分合约日K       → akshare_futures_extra.task_contracts_daily
  cftc              CFTC持仓        → akshare_futures_extra.task_cftc
  fx_daily          汇率(中行牌价)  → akshare_futures_extra.task_fx

用法:
  python backend/scripts/quantfutures_daily_sync.py --days 5
  python backend/scripts/quantfutures_daily_sync.py --datasets daily_forward,futures_realtime
"""

from __future__ import annotations

import sys
from typing import Any

# catalog 数据集名 → akshare_futures_sync 的 field（一个数据集可对应多个数据段）
DATASET_FIELDS: dict[str, list[str]] = {
    "daily_forward": ["foreign_daily", "cn_daily", "sge_daily"],
    "futures_realtime": ["foreign_realtime", "cn_realtime"],
}

# catalog 数据集名 → akshare_futures_extra 的 task 名（扩展数据集走独立脚本）
EXTRA_DATASET_TASKS: dict[str, str] = {
    "warehouse_receipts": "receipts",
    "member_positions": "member_positions",
    "contracts_daily": "contracts_daily",
    "cftc": "cftc",
    "fx_daily": "fx",
}

# 每日全量同步（datasets=None）时自动附加的扩展数据集；
# contracts_daily 请求量大（82 品种 × 15 合约月 ≈ 1200 次），仅面板显式勾选才运行
DEFAULT_EXTRA_DATASETS: tuple[str, ...] = (
    "warehouse_receipts",
    "member_positions",
    "cftc",
    "fx_daily",
)


def _refresh_l1_dataset(result: dict) -> None:
    """K线更新后增量重算 L1 因子日频分区（训练直读数据集）。"""
    try:
        from backend.scripts.build_ml_l1_dataset import build_l1

        result["l1_dataset"] = build_l1("futures")
    except Exception as exc:  # noqa: BLE001
        result["l1_dataset"] = {"status": "error", "error": str(exc)}


def _run_extra_tasks(datasets: list[str], days: int) -> dict:
    """运行扩展数据集同步（akshare_futures_extra 的 task_*）。"""
    import importlib

    mod = importlib.import_module("backend.scripts.akshare_futures_extra")
    out: dict[str, Any] = {}
    for ds in datasets:
        task = EXTRA_DATASET_TASKS.get(ds)
        if task is None:
            continue
        fn = getattr(mod, f"task_{task}", None)
        if fn is None:
            out[ds] = {"status": "error", "error": f"akshare_futures_extra 缺少 task_{task}"}
            continue
        try:
            out[ds] = fn(days) if task in ("receipts", "member_positions") else fn()
        except Exception as exc:  # noqa: BLE001
            out[ds] = {"status": "error", "error": str(exc)}
    return out


def run(*, days: int = 5, datasets: list[str] | None = None, **kwargs: Any) -> dict:
    """同步期货数据。datasets 为勾选的 catalog 数据集名；None 时全量同步。"""
    from backend.scripts.akshare_futures_sync import sync as ak_futures_sync

    if not datasets:
        result = ak_futures_sync("all")
        if not isinstance(result, dict):
            result = {"result": result}
        # 全量同步附带日频扩展数据集（contracts_daily 请求量大，不默认跑）
        result["extras"] = _run_extra_tasks(list(DEFAULT_EXTRA_DATASETS), days)
        # L1 因子直读数据集随日K落盘后刷新（与港股同口径）
        _refresh_l1_dataset(result)
        return {"market": "futures", "days": days, "result": result}

    fields: list[str] = []
    extra_datasets: list[str] = []
    for ds in datasets:
        if ds in EXTRA_DATASET_TASKS:
            extra_datasets.append(ds)
        else:
            fields.extend(DATASET_FIELDS.get(ds, []))
    if not fields and not extra_datasets:
        return {"market": "futures", "days": days, "datasets": datasets, "result": {}}

    result: dict[str, Any] = {}
    for field in fields:
        result[field] = ak_futures_sync(field)
    if extra_datasets:
        result["extras"] = _run_extra_tasks(extra_datasets, days)

    # L1 因子日频分区（训练直读数据集，随 daily_forward 增量刷新）
    if "daily_forward" in datasets or "l1_factors" in datasets:
        _refresh_l1_dataset(result)
    return {"market": "futures", "days": days, "datasets": datasets, "result": result}


def _cli() -> int:
    import argparse

    parser = argparse.ArgumentParser(description="期货数据同步")
    parser.add_argument("--days", type=int, default=5)
    parser.add_argument("--datasets", default=None, help="逗号分隔数据集名")
    args, _ = parser.parse_known_args()

    ds = [d.strip() for d in args.datasets.split(",") if d.strip()] if args.datasets else None
    result = run(days=args.days, datasets=ds)
    print(result)
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
