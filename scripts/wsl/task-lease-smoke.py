#!/usr/bin/env python3
"""Exercise task lease takeover and fencing using disposable PostgreSQL rows."""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import text

from backend.services.engine.alpha_agent import task_store as task_store_module
from backend.services.engine.alpha_agent.task_store import (
    MiningTaskStore,
    TaskLeaseLostError,
)
from backend.shared.database_manager_v2 import get_session
from backend.shared.utc_datetime import utc_now


async def _expire(task_id: str) -> None:
    async with get_session() as session:
        await session.execute(
            text(
                "UPDATE rd_agent_mining_tasks SET lease_expires_at = :expired "
                "WHERE task_id = :task_id"
            ),
            {"expired": utc_now() - timedelta(seconds=10), "task_id": task_id},
        )


async def _row(task_id: str) -> tuple[str, str | None, str | None]:
    async with get_session(read_only=True) as session:
        row = (
            await session.execute(
                text(
                    "SELECT status, owner_id, lease_expires_at::text "
                    "FROM rd_agent_mining_tasks WHERE task_id = :task_id"
                ),
                {"task_id": task_id},
            )
        ).one()
    return str(row[0]), row[1], row[2]


async def main() -> None:
    store = MiningTaskStore()
    previous_owner = task_store_module.TASK_OWNER_ID
    prefix = f"codex-lease-probe-{uuid.uuid4().hex}"
    user_id = prefix
    takeover_id, race_id, fenced_id, orphan_id = (
        f"{prefix}-{suffix}" for suffix in ("takeover", "race", "fenced", "orphan")
    )
    task_ids = [takeover_id, race_id, fenced_id, orphan_id]

    try:
        task_store_module.TASK_OWNER_ID = f"{prefix}-old-owner"
        await store.create_task(task_id=takeover_id, user_id=user_id, status="queued")
        await _expire(takeover_id)

        task_store_module.TASK_OWNER_ID = f"{prefix}-new-owner"
        claimed = await store.claim_expired_queued_leases([takeover_id])
        assert claimed == [takeover_id], f"expired lease not adopted: {claimed!r}"
        assert await store.claim_queued_task_for_start(takeover_id) is True
        assert await store.claim_queued_task_for_start(takeover_id) is False
        await store.mark_terminal(takeover_id, status="completed")
        assert (await _row(takeover_id))[0] == "completed"
        print("PASS expired queued lease takeover and owner-fenced start")

        await store.create_task(task_id=race_id, user_id=user_id, status="queued")
        results = await asyncio.gather(
            store.claim_queued_task_for_start(race_id),
            store.claim_queued_task_for_start(race_id),
        )
        assert results.count(True) == 1 and results.count(False) == 1, results
        print("PASS concurrent duplicate-start fence (exactly one claimant)")

        task_store_module.TASK_OWNER_ID = f"{prefix}-old-owner"
        await store.create_task(task_id=fenced_id, user_id=user_id, status="pending")
        task_store_module.TASK_OWNER_ID = f"{prefix}-new-owner"
        try:
            await store.update_progress(
                fenced_id, status="running", progress_pct=1, current_loop=0
            )
        except TaskLeaseLostError:
            pass
        else:
            raise AssertionError("stale owner was allowed to update task progress")
        assert (await _row(fenced_id))[0] == "pending"
        print("PASS stale process cannot write progress after ownership changes")

        task_store_module.TASK_OWNER_ID = f"{prefix}-old-owner"
        await store.create_task(task_id=orphan_id, user_id=user_id, status="pending")
        await _expire(orphan_id)
        task_store_module.TASK_OWNER_ID = f"{prefix}-new-owner"
        recovered = await store.reconcile_orphans(user_id=user_id)
        assert recovered == 1, f"expected one orphan recovery, got {recovered}"
        orphan_row = await _row(orphan_id)
        assert orphan_row[0] == "failed" and orphan_row[2] is None, orphan_row
        print("PASS expired orphan is finalized and its lease is cleared")
    finally:
        task_store_module.TASK_OWNER_ID = previous_owner
        async with get_session() as session:
            await session.execute(
                text(
                    "DELETE FROM rd_agent_mining_tasks "
                    "WHERE task_id = ANY(CAST(:task_ids AS text[]))"
                ),
                {"task_ids": task_ids},
            )
        async with get_session(read_only=True) as session:
            remaining = int(
                (
                    await session.execute(
                        text(
                            "SELECT count(*) FROM rd_agent_mining_tasks "
                            "WHERE task_id = ANY(CAST(:task_ids AS text[]))"
                        ),
                        {"task_ids": task_ids},
                    )
                ).scalar_one()
            )
        if remaining:
            raise RuntimeError(f"failed to clean up {remaining} lease probe rows")
        print("PASS disposable probe rows cleaned up")


if __name__ == "__main__":
    asyncio.run(main())
