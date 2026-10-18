#!/usr/bin/env python3
"""一键导入模型：扫描 models/users/*/*/ 目录，把整包下载的模型注册进 qm_user_models。

背景：模型注册表（推理中心 / API 列表）只读 PostgreSQL 的 qm_user_models 表，
磁盘上的模型文件不会自动出现在列表里。仓库随包内置的测试模型
（如 models/users/default/00000001/mdl_cn_train_*）需要本脚本登记一次。

用法（容器内执行；docker-compose 将 ./models 挂载到 /app/models）：
    docker exec quantmind python backend/scripts/import_user_models.py
    docker exec quantmind python backend/scripts/import_user_models.py --dry-run
    docker exec quantmind python backend/scripts/import_user_models.py --refresh

幂等：已登记的模型默认跳过；--refresh 会用磁盘上的 metadata/result 覆盖更新。
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("import_user_models")

# 与 model_registry.ModelRegistryManager 约定一致；
# 部署形态是单容器，注册表里存 /app/models/... 容器路径。
if Path("/app").exists():
    MODELS_USERS_ROOT = Path("/app") / "models" / "users"
else:
    MODELS_USERS_ROOT = PROJECT_ROOT / "models" / "users"

MODEL_FILE_EXTENSIONS = (".lgb", ".xgb", ".cbm", ".pkl", ".pth")


def _find_model_file(model_dir: Path, metadata: dict) -> str:
    """metadata.model_file 优先，否则取目录中第一个已知扩展名的模型文件。"""
    declared = str(metadata.get("model_file") or "").strip()
    if declared and (model_dir / declared).exists():
        return declared
    for child in sorted(model_dir.iterdir()):
        if child.is_file() and child.suffix in MODEL_FILE_EXTENSIONS:
            return child.name
    return ""


async def import_models(root: Path, *, dry_run: bool = False, refresh: bool = False) -> dict:
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    summary = {"scanned": 0, "imported": 0, "skipped": 0, "updated": 0,
               "errors": [], "details": []}

    for model_dir in sorted(root.glob("*/*/*")):
        if not model_dir.is_dir():
            continue
        summary["scanned"] += 1
        try:
            metadata_path = model_dir / "metadata.json"
            if not metadata_path.exists():
                log.warning("[SKIP] %s: 无 metadata.json", model_dir.name)
                summary["skipped"] += 1
                continue

            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            if not isinstance(metadata, dict):
                raise ValueError("metadata.json 不是 JSON 对象")

            # 融合模型能力已下线，不再导入
            if str(metadata.get("model_type") or "").lower() == "ensemble" or str(
                metadata.get("framework") or ""
            ).lower() == "ensemble":
                log.info("[SKIP] %s: 融合模型已下线", model_dir.name)
                summary["skipped"] += 1
                continue

            model_file = _find_model_file(model_dir, metadata)
            if not model_file:
                raise ValueError(f"未找到模型文件（{MODEL_FILE_EXTENSIONS}）")

            tenant_id = model_dir.parent.parent.name
            user_id = model_dir.parent.name
            model_id = model_dir.name
            source_run_id = str(metadata.get("run_id") or "")
            storage_path = f"/app/models/users/{tenant_id}/{user_id}/{model_id}"

            metrics_json = "{}"
            result_path = model_dir / "result.json"
            if result_path.exists():
                try:
                    result = json.loads(result_path.read_text(encoding="utf-8"))
                    if isinstance(result, dict) and isinstance(result.get("metrics"), dict):
                        metrics_json = json.dumps(result["metrics"], ensure_ascii=False)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    log.warning("[WARN] %s: result.json 解析失败，metrics 置空", model_id)

            detail = {"model_id": model_id, "tenant_id": tenant_id, "user_id": user_id,
                      "model_file": model_file, "source_run_id": source_run_id}
            summary["details"].append(detail)

            if dry_run:
                log.info("[DRY] %s -> %s", model_id, storage_path)
                summary["imported"] += 1
                continue

            async with get_session() as session:
                existing = (
                    await session.execute(
                        text(
                            "SELECT 1 FROM qm_user_models "
                            "WHERE tenant_id = :t AND user_id = :u AND model_id = :m"
                        ),
                        {"t": tenant_id, "u": user_id, "m": model_id},
                    )
                ).scalar_one_or_none()

                if existing and not refresh:
                    log.info("[SKIP] %s 已登记", model_id)
                    summary["skipped"] += 1
                    continue

                params = {
                    "t": tenant_id, "u": user_id, "m": model_id,
                    "s": source_run_id, "sp": storage_path, "mf": model_file,
                    "meta": json.dumps(metadata, ensure_ascii=False, default=str),
                    "metrics": metrics_json,
                }
                if existing:
                    await session.execute(
                        text(
                            "UPDATE qm_user_models SET storage_path=:sp, model_file=:mf, "
                            "source_run_id=:s, metadata_json=CAST(:meta AS jsonb), "
                            "metrics_json=CAST(:metrics AS jsonb), "
                            "status = CASE WHEN status='archived' THEN 'archived' ELSE 'ready' END, "
                            "updated_at=now() "
                            "WHERE tenant_id=:t AND user_id=:u AND model_id=:m"
                        ),
                        params,
                    )
                    log.info("[REFRESH] %s", model_id)
                    summary["updated"] += 1
                else:
                    await session.execute(
                        text(
                            "INSERT INTO qm_user_models "
                            "(tenant_id, user_id, model_id, source_run_id, status, storage_path, "
                            " model_file, metadata_json, metrics_json, is_default, created_at, updated_at) "
                            "VALUES (:t, :u, :m, :s, 'ready', :sp, :mf, "
                            " CAST(:meta AS jsonb), CAST(:metrics AS jsonb), false, now(), now())"
                        ),
                        params,
                    )
                    log.info("[IMPORT] %s -> %s", model_id, storage_path)
                    summary["imported"] += 1
                await session.commit()
        except Exception as exc:
            log.error("[FAIL] %s: %s", model_dir.name, exc)
            summary["errors"].append({"model_dir": str(model_dir), "error": str(exc)})

    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="一键导入模型目录到 qm_user_models")
    parser.add_argument("--root", type=str, default=str(MODELS_USERS_ROOT),
                        help=f"models/users 根目录（默认 {MODELS_USERS_ROOT}）")
    parser.add_argument("--dry-run", action="store_true", help="仅扫描不写库")
    parser.add_argument("--refresh", action="store_true", help="已存在的模型用磁盘内容覆盖更新")
    args = parser.parse_args()

    summary = asyncio.run(import_models(Path(args.root), dry_run=args.dry_run, refresh=args.refresh))
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 1 if summary["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())