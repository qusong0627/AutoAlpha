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

from backend.shared.secret_redaction import install_secret_redaction
from backend.services.engine.alpha_agent.factor_naming import (
    name_task_factors_cn,
)


async def _run(task_id: str, user_id: str) -> int:
    return await name_task_factors_cn(task_id, user_id)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--user-id", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    install_secret_redaction()
    try:
        asyncio.run(_run(args.task_id, args.user_id))
        return 0
    except Exception:  # noqa: BLE001 — logged for retry/debug without changing mining status
        logging.exception("task=%s factor naming batch failed", args.task_id)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
