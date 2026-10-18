#!/usr/bin/env python3
"""清理已下线功能的历史数据：多周期训练（parent/child）+ 模型融合（ensemble）。

背景：多周期训练、多周期对比回测、手工融合模型能力已从代码中移除。
本脚本清理它们遗留在库里的记录（可选连同磁盘模型目录），使其不再出现在
训练历史与模型注册表中。

清理对象：
  1. qm_user_models 中的融合模型行
     （metadata_json.model_type='ensemble' / framework='ensemble'
       / model_file='ensemble_config.json' / model_id 形如 mdl_<mkt>_ensemble_*）
  2. admin_training_jobs 中的多周期父/子任务行
     （request_payload 含 _parent / _parent_run_id / _child_run_ids）
  3. 磁盘上对应的模型目录（含 ensemble_config.json，或目录名等于被删 model_id）

用法（容器内执行；docker-compose 将 ./models 挂载到 /app/models）：
    docker exec quantmind python backend/scripts/cleanup_multi_horizon_ensemble.py
    docker exec quantmind python backend/scripts/cleanup_multi_horizon_ensemble.py --dry-run
    docker exec quantmind python backend/scripts/cleanup_multi_horizon_ensemble.py --keep-files

幂等：重复执行不会误删普通模型；默认保留磁盘文件，仅加 --purge-files 才删除。
"""

import argparse
import asyncio
import json
import logging
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("cleanup_multi_horizon_ensemble")

if Path("/app").exists():
    MODELS_USERS_ROOT = Path("/app") / "models" / "users"
else:
    MODELS_USERS_ROOT = PROJECT_ROOT / "models" / "users"

# 融合模型判定：metadata 里任一标识命中即为融合模型
_ENSEMBLE_PREDICATE = (
    "(metadata_json->>'model_type' = 'ensemble' "
    " OR metadata_json->>'framework' = 'ensemble' "
    " OR metadata_json->>'model_file' = 'ensemble_config.json' "
    " OR model_id LIKE 'mdl\\_%\\_ensemble\\_%')"
)

# 多周期训练父子任务判定：request_payload JSONB 含任一标记键
_MULTI_HORIZON_PREDICATE = (
    "(request_payload ? '_parent' "
    " OR request_payload ? '_parent_run_id' "
    " OR request_payload ? '_child_run_ids')"
)


async def _collect_ensemble_model_ids(session) -> list[dict]:
    from sqlalchemy import text

    rows = (
        (
            await session.execute(
                text(
                    "SELECT tenant_id, user_id, model_id, storage_path "
                    f"FROM qm_user_models WHERE {_ENSEMBLE_PREDICATE}"
                )
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


async def _collect_multi_horizon_jobs(session) -> list[str]:
    from sqlalchemy import text

    rows = (
        (
            await session.execute(
                text(
                    f"SELECT id FROM admin_training_jobs WHERE {_MULTI_HORIZON_PREDICATE}"
                )
            )
        )
        .mappings()
        .all()
    )
    return [str(r["id"]) for r in rows]


def _purge_model_dirs(model_ids: set[str], *, dry_run: bool) -> list[str]:
    """删除磁盘上被登记的融合模型目录，以及任何含 ensemble_config.json 的目录。"""
    removed: list[str] = []
    if not MODELS_USERS_ROOT.exists():
        return removed
    for model_dir in MODELS_USERS_ROOT.glob("*/*/*"):
        if not model_dir.is_dir():
            continue
        is_ensemble = (
            model_dir / "ensemble_config.json"
        ).exists() or model_dir.name in model_ids
        if not is_ensemble:
            continue
        if dry_run:
            log.info("[DRY-RUN] 将删除目录 %s", model_dir)
        else:
            try:
                shutil.rmtree(model_dir)
                log.info("[PURGE] %s", model_dir)
            except Exception as exc:  # noqa: BLE001
                log.error("[PURGE-FAIL] %s: %s", model_dir, exc)
                continue
        removed.append(str(model_dir))
    return removed


async def cleanup(*, dry_run: bool = False, purge_files: bool = False) -> dict:
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    summary: dict = {
        "ensemble_models": 0,
        "multi_horizon_jobs": 0,
        "purged_dirs": [],
        "dry_run": dry_run,
    }

    async with get_session() as session:
        models = await _collect_ensemble_model_ids(session)
        jobs = await _collect_multi_horizon_jobs(session)

    summary["ensemble_models"] = len(models)
    summary["multi_horizon_jobs"] = len(jobs)
    for m in models:
        log.info("[MODEL] %s / %s / %s", m["tenant_id"], m["user_id"], m["model_id"])
    for j in jobs:
        log.info("[JOB] %s", j)

    if purge_files:
        summary["purged_dirs"] = _purge_model_dirs(
            {str(m["model_id"]) for m in models}, dry_run=dry_run
        )

    if dry_run:
        return summary

    async with get_session() as session:
        if models:
            await session.execute(
                text(f"DELETE FROM qm_user_models WHERE {_ENSEMBLE_PREDICATE}")
            )
        if jobs:
            await session.execute(
                text(
                    f"DELETE FROM admin_training_jobs WHERE {_MULTI_HORIZON_PREDICATE}"
                )
            )
        await session.commit()

    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="清理多周期训练与融合模型历史数据")
    parser.add_argument("--dry-run", action="store_true", help="仅统计不写库/不删文件")
    parser.add_argument(
        "--purge-files",
        action="store_true",
        help="同时删除磁盘上的融合模型目录（默认只清库记录）",
    )
    args = parser.parse_args()

    summary = asyncio.run(cleanup(dry_run=args.dry_run, purge_files=args.purge_files))
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
