#!/usr/bin/env python3
"""QuantDB 本地离线数据扫描 — 建立 SQLite 同步状态库
====================================================

针对用户离线下载（网盘包 / quant_data 归档）落地的 QuantDB 数据目录，
扫描已有 parquet 并把 md5/sha256/size 登记进同步状态库（objects/releases
表），使配置 QuantDB API key 后的首次同步直接命中增量 fast-path，
只下载缺失/更新的分区，避免全量重拉（几万文件 × 数十 GB）。

写入的状态库（schema 相同，均为 objects + releases 两表）：
  1. QuantMind 同步状态库：QUANTDB_STATE_DIR/quantdb_sync_<数据根>.sqlite
     —— quantdb_daily_sync.py 使用（管理台「按数据集同步」/ Celery 每日同步）
  2. SDK 状态库：<数据根>/quantdb_sync.sqlite
     —— quantdb_sdk sync_dataset / mount_local_dataset 使用
  3. 扫描目录 ≠ 当前数据目录时，额外写一份以扫描目录命名的状态库，
     覆盖「先扫描外部目录、之后把 QM_QUANTDB_DATA_DIR 指向它」的场景。

关键约定（与云端一致）：
  - object key == 落盘相对路径（posix 分隔），如
    ``1_kline_data/daily_forward/dt=20260804/data.parquet``
  - V2 分区对象校验用 sha256，V1/SDK 304 对账用 md5（COS ETag），
    因此两类哈希都在扫描时单遍算好。
  - parquet 首尾 magic（PAR1）校验失败的文件不登记，
    下次同步会重下自愈。

用法:
  python backend/scripts/quantdb_local_scan.py                 # 扫描默认数据目录
  python backend/scripts/quantdb_local_scan.py --root D:\\quant_data
  python backend/scripts/quantdb_local_scan.py --datasets daily_forward,balance
  python backend/scripts/quantdb_local_scan.py --force         # 忽略已有登记重算哈希
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional
from collections.abc import Callable

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("quantdb_local_scan")

SCAN_WORKERS = 8
FILE_PROGRESS_EVERY = 500

INSERT_OBJECT_SQL = (
    "INSERT OR REPLACE INTO objects(key, etag, sha256, size, path, layout, dataset)"
    " VALUES(?,?,?,?,?,?,?)"
)


def _default_root() -> Path:
    """与 quantdb_daily_sync.QUANTDB_DATA_DIR 同源的数据根目录解析。"""
    return Path(os.getenv("QM_QUANTDB_DATA_DIR", str(PROJECT_ROOT / "data" / "quantdb")))


def _open_state_db(path: Path):
    """打开（必要时创建）一个同步状态库，与 quantdb_daily_sync._open_state 同构。"""
    import sqlite3

    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS objects ("
        "key TEXT PRIMARY KEY, etag TEXT, sha256 TEXT, size INTEGER,"
        " path TEXT, layout TEXT, dataset TEXT)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS releases (dataset TEXT PRIMARY KEY, release_id TEXT NOT NULL)"
    )
    return conn


def iter_dataset_files(root: Path, rel_dir: str, layout: str) -> list[Path]:
    """按数据集 layout 枚举属于云端对象集的本地文件。

    partition 只取 dt=*/quarter= 分区内的 data.parquet——kline 类数据集目录下
    与分区并存的按标的采集文件（000001.SZ.parquet 等）不是云端 V2 对象，
    登记它们纯属浪费哈希时间。tick_data 实际落盘为按日分片的平铺文件
    （{safe_symbol}_{YYYYMMDD}.parquet，无 dt= 目录），此时回退平铺枚举。
    """
    base = root / rel_dir
    if not base.is_dir():
        return []
    if layout == "partition":
        out = []
        for part in base.iterdir():
            if part.is_dir() and (
                part.name.startswith("dt=") or part.name.startswith("quarter=")
            ):
                pq = part / "data.parquet"
                if pq.is_file():
                    out.append(pq)
        if out:
            return sorted(out)
        # 无分区目录（如 tick_data 按日分片平铺）→ 回退平铺枚举
        return sorted(base.glob("*.parquet"))
    # symbol / single：数据集目录顶层的 parquet
    return sorted(base.glob("*.parquet"))


def hash_parquet(path: Path) -> tuple[str, str, int] | None:
    """单遍流式计算 md5 + sha256，并校验 parquet 首尾 magic。

    返回 (md5_hex, sha256_hex, size)；文件不可读或非法 parquet 返回 None。
    """
    try:
        size = path.stat().st_size
        if size < 8:
            return None
        h_md5, h_sha = hashlib.md5(), hashlib.sha256()
        head = tail = b""
        first = True
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(1 << 20)
                if not chunk:
                    break
                if first:
                    head, first = chunk[:4], False
                tail = chunk[-4:]
                h_md5.update(chunk)
                h_sha.update(chunk)
        if head != b"PAR1" or tail != b"PAR1":
            return None
        return h_md5.hexdigest(), h_sha.hexdigest(), size
    except OSError:
        return None


def _seed_releases(root: Path, conns: list) -> dict:
    """从 <root>/releases/*/manifest.json 提取每个数据集的最新 release_id。

    发布目录名（YYYYMMDD.seq）按字典序即时间序。seed 后 SDK sync_dataset
    以 cursor 增量拉取云端 release 列表；quantdb_daily_sync 的 V2 路径每次
    都全量拉 release 清单，releases 表仅作记录，写它无副作用。
    """
    rel_dir = root / "releases"
    if not rel_dir.is_dir():
        return {"manifests": 0, "latest_by_dataset": {}}
    manifests = sorted(rel_dir.glob("*/manifest.json"))
    latest: dict[str, str] = {}
    parsed = 0
    for m in manifests:
        release_id = m.parent.name
        try:
            data = json.loads(m.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log.warning("[RELEASES] 跳过无法解析的清单 %s: %s", m.name, exc)
            continue
        parsed += 1
        for obj in data.get("objects") or []:
            ds = obj.get("dataset")
            if ds and (ds not in latest or release_id > latest[ds]):
                latest[ds] = release_id
    for conn in conns:
        conn.executemany(
            "INSERT OR REPLACE INTO releases(dataset, release_id) VALUES(?,?)",
            sorted(latest.items()),
        )
        conn.commit()
    return {"manifests": parsed, "latest_by_dataset": latest}


def scan_local_data(
    root: str | Path | None = None,
    datasets: list[str] | None = None,
    force: bool = False,
    progress_cb: Callable | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> dict:
    """扫描本地 QuantDB 数据目录，登记进同步状态库。

    root 缺省为 QM_QUANTDB_DATA_DIR；datasets 为 None 时扫描全部已知数据集。
    默认增量：size 未变且已有 sha256 登记的文件直接复用旧哈希（repeat-scan
    秒级完成），force=True 强制重算。
    """
    from backend.shared.quantdb_datasets import DATASETS
    from backend.scripts.quantdb_daily_sync import _state_path

    root_path = Path(os.path.abspath(Path(root).expanduser())) if root else _default_root()
    if not root_path.is_dir():
        raise NotADirectoryError(f"数据目录不存在: {root_path}")

    specs = list(DATASETS)
    if datasets:
        want = set(datasets)
        unknown = want - {s.dataset for s in specs}
        if unknown:
            raise ValueError(f"未知数据集: {', '.join(sorted(unknown))}")
        specs = [s for s in specs if s.dataset in want]

    active_dir = _default_root()
    same_root = os.path.normcase(str(root_path)) == os.path.normcase(str(active_dir))

    # QuantMind 同步状态库永远以「活跃数据目录」命名，保证 sync 能找到；
    # 扫描外部目录时额外写一份以扫描目录命名的库，覆盖后续切换 env 的场景。
    targets: list[tuple[str, Path]] = [("quantmind", _state_path(active_dir))]
    if not same_root:
        targets.append(("quantmind_scan_root", _state_path(root_path)))
    targets.append(("sdk", root_path / "quantdb_sync.sqlite"))

    conns = [(label, _open_state_db(p)) for label, p in targets]
    started = time.time()
    warnings: list[str] = []
    if not same_root:
        warnings.append(
            f"扫描目录 {root_path} 与当前数据目录 {active_dir} 不一致："
            "状态库 path 指向扫描目录；请把数据移动/链接到数据目录，"
            "或将 QM_QUANTDB_DATA_DIR 指向扫描目录后重启，否则业务读不到这些数据。"
        )

    per_dataset: dict[str, dict] = {}
    total_registered = total_files = total_bytes = total_invalid = total_reused = 0
    cancelled = False

    try:
        for idx, spec in enumerate(specs):
            if should_cancel is not None and should_cancel():
                cancelled = True
                break
            files = iter_dataset_files(root_path, spec.rel_dir, spec.layout)
            if progress_cb:
                progress_cb(
                    "dataset_start",
                    dataset=spec.dataset,
                    name=spec.name,
                    index=idx,
                    total=len(specs),
                    files=len(files),
                )

            # 已登记且 size/etag/sha256 齐全的文件复用旧哈希（repeat-scan 免重读）；
            # 缺任一字段的行回退重算，避免把 sync 写入的 etag 抹成空。
            reused_hashes: dict[str, tuple[str, str, str]] = {}
            if not force:
                first_conn = conns[0][1]
                for key, size, etag, sha in first_conn.execute(
                    "SELECT key, size, etag, sha256 FROM objects WHERE dataset=?",
                    (spec.dataset,),
                ):
                    if size and etag and sha:
                        reused_hashes[key] = (str(size), etag, sha)

            layout_col = "v2_daily_partition" if spec.layout == "partition" else "v1_symbol"
            rows: list[tuple] = []
            ds_bytes = ds_invalid = ds_reused = 0

            def _work(p: Path):
                return p, hash_parquet(p)

            pending = []
            for p in files:
                key = p.relative_to(root_path).as_posix()
                cached = reused_hashes.get(key)
                if cached and p.stat().st_size == int(cached[0]):
                    ds_reused += 1
                    ds_bytes += int(cached[0])
                    rows.append(
                        (key, cached[1], cached[2], int(cached[0]),
                         str(p), layout_col, spec.dataset)
                    )
                    continue
                pending.append(p)

            # md5 未存库（旧库只有 sha）的复用行需要补算 md5，交线程池统一处理
            with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as pool:
                hashed = 0
                for p, h in pool.map(_work, pending):
                    hashed += 1
                    if h is None:
                        ds_invalid += 1
                    else:
                        md5_hex, sha_hex, size = h
                        ds_bytes += size
                        rows.append(
                            (p.relative_to(root_path).as_posix(), md5_hex, sha_hex,
                             size, str(p), layout_col, spec.dataset)
                        )
                    if progress_cb and hashed % FILE_PROGRESS_EVERY == 0 and pending:
                        progress_cb(
                            "file",
                            dataset=spec.dataset,
                            done=hashed,
                            total=len(pending),
                        )

            for _, conn in conns:
                conn.executemany(INSERT_OBJECT_SQL, rows)
                conn.commit()

            per_dataset[spec.dataset] = {
                "files": len(files),
                "registered": len(rows),
                "reused": ds_reused,
                "invalid": ds_invalid,
                "bytes": ds_bytes,
            }
            total_registered += len(rows)
            total_files += len(files)
            total_bytes += ds_bytes
            total_invalid += ds_invalid
            total_reused += ds_reused
            if progress_cb:
                progress_cb(
                    "dataset_done",
                    dataset=spec.dataset,
                    registered=len(rows),
                    files=len(files),
                    invalid=ds_invalid,
                )
            log.info(
                "[SCAN] %s: %d 文件登记 %d（复用 %d，无效 %d）",
                spec.dataset, len(files), len(rows), ds_reused, ds_invalid,
            )

        releases_info = (
            _seed_releases(root_path, [c for _, c in conns]) if not cancelled
            else {"manifests": 0, "latest_by_dataset": {}}
        )
    finally:
        for _, conn in conns:
            conn.close()

    return {
        "root": str(root_path),
        "active_data_dir": str(active_dir),
        "same_root": same_root,
        "scanned_datasets": len(specs),
        "registered": total_registered,
        "reused": total_reused,
        "invalid_files": total_invalid,
        "total_files": total_files,
        "total_bytes": total_bytes,
        "releases": releases_info,
        "state_dbs": {label: str(p) for label, p in targets},
        "elapsed_sec": round(time.time() - started, 1),
        "per_dataset": per_dataset,
        "warnings": warnings,
        "cancelled": cancelled,
        "finished": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="QuantDB 本地离线数据扫描（建立同步状态库）")
    parser.add_argument("--root", type=str, help="数据根目录（默认 QM_QUANTDB_DATA_DIR）")
    parser.add_argument("--datasets", type=str, help="数据集名（逗号分隔，默认全部）")
    parser.add_argument("--force", action="store_true", help="忽略已有登记，强制重算哈希")
    args = parser.parse_args()

    summary = scan_local_data(
        root=args.root,
        datasets=args.datasets.split(",") if args.datasets else None,
        force=args.force,
    )
    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted")
        sys.exit(1)
    except Exception as e:  # noqa: BLE001
        log.error("FATAL: %s", e, exc_info=True)
        sys.exit(1)
