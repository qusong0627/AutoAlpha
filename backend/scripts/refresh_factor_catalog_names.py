#!/usr/bin/env python3
"""按因子字典刷新训练因子目录（catalog）的显示名 —— 幂等、只改 display_name。

为什么需要它：`qm_training_factor_mapping` 的 display_name 是**写库快照**，
字典（`quantdb_factor_dictionary.py`）改了名字之后，已发布/已存在的版本不会自动跟进。
而 `POST /versions/{id}/seed` 用的是 `ON CONFLICT DO NOTHING`，对已有行**不会覆盖**，
且会把 default_selected 重置成默认集（丢掉管理员的勾选）——所以「改名」这件事
必须走本脚本：只 UPDATE display_name，其余列（enabled/default_selected/required/sort_order）一律不动。

标准流程（改名 → 上线）：
  1) 克隆一个已发布版本为草稿（保留勾选）
     curl -X POST .../versions/{published}/clone -d '{"version_name":"..."}'
  2) python backend/scripts/refresh_factor_catalog_names.py --version-id {draft}        # 预演
     python backend/scripts/refresh_factor_catalog_names.py --version-id {draft} --apply
  3) POST .../versions/{draft}/publish

用法：
  --version-id   目标版本；省略时取该数据集最新的 draft
  --dataset      数据集（默认 alpha_library）
  --apply        真正写入（默认只预演并打印差异样例）
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import text  # noqa: E402

from backend.services.engine.data_platform.quantdb_factor_dictionary import definition_for  # noqa: E402
from backend.shared.database_manager_v2 import get_session  # noqa: E402


async def _resolve_version(session, version_id: str | None, dataset: str) -> str:
    if version_id:
        return version_id
    row = (await session.execute(text("""
        SELECT version_id FROM qm_training_factor_catalog_version
        WHERE source_dataset = :ds AND status = 'draft'
        ORDER BY created_at DESC LIMIT 1
    """), {"ds": dataset})).scalar()
    if not row:
        raise SystemExit(f"没有找到 {dataset} 的草稿版本，请先克隆一个已发布版本")
    return str(row)


async def main() -> int:
    ap = argparse.ArgumentParser(description="按字典刷新因子目录显示名")
    ap.add_argument("--version-id", default=None)
    ap.add_argument("--dataset", default="alpha_library")
    ap.add_argument("--apply", action="store_true", help="真正写入；缺省只预演")
    args = ap.parse_args()

    async with get_session() as session:
        version_id = await _resolve_version(session, args.version_id, args.dataset)
        rows = (await session.execute(text("""
            SELECT source_column, display_name FROM qm_training_factor_mapping
            WHERE version_id = :vid ORDER BY source_column
        """), {"vid": version_id})).mappings().all()

        if not rows:
            raise SystemExit(f"版本 {version_id} 没有任何 mapping")

        changes = []
        for r in rows:
            expected = str(definition_for(str(r["source_column"]))["display_name"])
            if expected != str(r["display_name"]):
                changes.append((str(r["source_column"]), str(r["display_name"]), expected))

        print(f"版本 {version_id}：共 {len(rows)} 行，需要改名 {len(changes)} 行")
        for col, old, new in changes[:5]:
            print(f"  {col}: {old!r} → {new!r}")

        if not args.apply:
            print("（预演模式，未写入；确认无误后加 --apply）")
            return 0

        for col, _old, new in changes:
            await session.execute(text("""
                UPDATE qm_training_factor_mapping SET display_name = :name
                WHERE version_id = :vid AND source_column = :col
            """), {"name": new, "vid": version_id, "col": col})
        await session.commit()

        # 复核：同前缀因子的名字必须互不相同（历史上曾全部退化成一个名字）
        dup = (await session.execute(text("""
            SELECT count(*) FROM (
              SELECT display_name FROM qm_training_factor_mapping
              WHERE version_id = :vid GROUP BY display_name HAVING count(*) > 1
            ) t
        """), {"vid": version_id})).scalar()
        print(f"已写入 {len(changes)} 行；重名组数 = {dup}（应为 0，a158_ 系列同名属正常除外时人工确认）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
