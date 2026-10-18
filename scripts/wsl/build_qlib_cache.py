#!/usr/bin/env python3
"""从 QuantDB parquet 构建 CN 市场 Qlib 缓存。

为什么要单独跑一次：
    `qlib_paths.is_qlib_provider_ready` 要求 `calendars/day.txt` +
    `instruments/all.txt` + `features/`，三者齐备才算就绪。
    E:\\quantdb 里没有 `.qlib_cache`，`/data/qlib/cn_data` 也只是空目录，
    所以 `resolve_qlib_provider_uri("CN")` 会一路回退到仓库 `db/qlib_data`。

    `ensure_qlib_cache()` 的写入目标正是 `resolve_qlib_provider_uri(market)`，
    因此只要把 env `QLIB_PROVIDER_URI=/data/qlib/cn_data` 钉住（该 env 在解析里
    最高优先级），缓存就会建在 WSL 本地 ext4 上——比写回 9p 的 E: 快得多。

用法（WSL）：
    QLIB_PROVIDER_URI=/data/qlib/cn_data \
    QM_QUANTDB_DATA_DIR=/data/quantdb \
    /home/qusong/autoalpha-venv312/bin/python \
        /mnt/d/git/autoalpha/scripts/wsl/build_qlib_cache.py [--incremental]
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, "/mnt/d/git/autoalpha")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--market", default="CN")
    ap.add_argument("--incremental", action="store_true", help="已建则增量更新")
    args = ap.parse_args()

    from backend.services.engine.qlib_data_builder import (
        QlibDataBuilder,
        ensure_qlib_cache,
    )
    from backend.shared.qlib_paths import (
        is_qlib_provider_ready,
        resolve_qlib_provider_uri,
    )

    quantdb = os.getenv("QM_QUANTDB_DATA_DIR", "/data/quantdb")
    target = os.getenv("QLIB_PROVIDER_URI") or resolve_qlib_provider_uri(args.market)

    print(f"quantdb_dir : {quantdb}")
    print(f"qlib target : {target}")
    print(f"ready before: {is_qlib_provider_ready(target)}")

    if not Path(quantdb).is_dir():
        print(f"FAILED: quantdb_dir 不存在 {quantdb}")
        return 2

    t0 = time.time()
    if args.incremental:
        result = ensure_qlib_cache(args.market, quantdb_dir=quantdb, qlib_dir=target)
    else:
        builder = QlibDataBuilder.for_market(
            args.market, data_dir=quantdb, qlib_dir=target
        )
        print(f"is_built    : {builder.is_built()}")
        builder.build_all(incremental=builder.is_built())
        result = str(builder.qlib_dir)

    print(f"\nbuilt in {time.time() - t0:.1f}s -> {result}")
    print(f"ready after : {is_qlib_provider_ready(result)}")

    for rel in ("calendars/day.txt", "instruments/all.txt"):
        p = Path(result) / rel
        print(f"  {rel:24s} {'ok' if p.is_file() else 'MISSING'}  {p}")
    feats = Path(result) / "features"
    n = len(list(feats.rglob("*.bin"))) if feats.is_dir() else 0
    print(f"  features/*.bin          {n} files")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
