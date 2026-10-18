#!/usr/bin/env python3
"""对指定挖掘任务的已落库因子批量补充 AI 中文显示名。"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.services.engine.alpha_agent.factor_naming import generate_factor_names_cn
from backend.services.engine.qlib_app.services.rd_agent_persistence import (
    RDAgentFactorPersistence,
)


async def _run(task_id: str, user_id: str) -> int:
    persistence = RDAgentFactorPersistence()
    await persistence.ensure_tables()
    factors = await persistence.list_factors(
        user_id=user_id,
        task_id=task_id,
        limit=500,
    )
    candidates = []
    for factor in factors:
        metadata = factor.get("metadata") or {}
        candidates.append({
            "factor_id": factor.get("factor_id"),
            "factor_name": factor.get("factor_name"),
            "factor_formulation": factor.get("factor_formulation"),
            "description": metadata.get("description") or metadata.get("formulation"),
        })
    names = await generate_factor_names_cn(candidates)
    saved = await persistence.update_factor_names_cn(names)
    logging.info("task=%s factors=%d generated=%d saved=%d", task_id, len(candidates), len(names), saved)
    return saved


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--user-id", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(_run(args.task_id, args.user_id))
        return 0
    except Exception:  # noqa: BLE001 — logged for retry/debug without changing mining status
        logging.exception("task=%s factor naming batch failed", args.task_id)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
