#!/usr/bin/env python3
"""Exercise task lease takeover and fencing using disposable PostgreSQL rows."""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import text

from backend.services.engine.alpha_agent import task_store as task_store_module
from backend.services.engine.alpha_agent import launcher as launcher_module
from backend.services.engine.alpha_agent.launcher import AlphaAgentLauncher, EvolutionTask, TaskStatus
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


async def _check_preparation_lifecycle(
    store: MiningTaskStore, prefix: str, user_id: str, task_ids: list[str]
) -> None:
    """Run real child processes and PG leases without loading market data or LLMs."""
    real_popen = launcher_module.subprocess.Popen
    children = []
    executions = []
    started = asyncio.Event()
    mode = "wait"

    def spawn_probe(command, **kwargs):
        if mode == "success":
            code = (
                "import json,sys; print('preparation complete', flush=True); "
                "open(sys.argv[1], 'w').write(json.dumps({'provider_uri':'/tmp/probe-qlib'}))"
            )
        elif mode == "failure":
            code = "print('synthetic preparation failure', flush=True); raise SystemExit(7)"
        else:
            code = "import time; print('preparing probe data', flush=True); time.sleep(30)"
        child = real_popen([sys.executable, "-c", code, command[-1]], **kwargs)
        children.append(child)
        started.set()
        return child

    with TemporaryDirectory(prefix="autoalpha-lifecycle-") as log_root:
        launcher = AlphaAgentLauncher.__new__(AlphaAgentLauncher)
        launcher._log_dir = Path(log_root)
        launcher._tasks = {}
        launcher._owned_queued_task_ids = set()
        launcher._drain_after_finish = AsyncMock()

        async def execute(task, **kwargs):
            task.status = TaskStatus.RUNNING
            task.phase = "preparing_data"
            await launcher._db_sync_progress(task)
            provider = await launcher._prepare_qlib_data(task)
            if task._cancel_requested:
                assert provider is None
                return
            assert provider == "/tmp/probe-qlib", provider
            task.status = TaskStatus.COMPLETED
            task.progress_pct = 100
            await launcher._db_finish(task)

        async def start(suffix):
            task_id = f"{prefix}-{suffix}"
            task_ids.append(task_id)
            await store.create_task(task_id=task_id, user_id=user_id, status="pending")
            task = EvolutionTask(task_id=task_id, user_id=user_id, progress_pct=2)
            launcher._tasks[task_id] = task
            started.clear()
            execution = asyncio.create_task(launcher._run_evolution(task))
            executions.append(execution)
            await asyncio.wait_for(started.wait(), 5)
            return task, execution

        with (
            patch.object(launcher_module, "_DB_SYNC_INTERVAL_S", 0.1),
            patch.object(task_store_module, "TASK_LEASE_SECONDS", 1),
            patch.object(launcher_module, "_task_store", return_value=store),
            patch.object(launcher_module.subprocess, "Popen", side_effect=spawn_probe),
            patch.object(launcher, "_execute_evolution", new=execute),
        ):
            try:
                task, execution = await start("preparing-heartbeat")
                await asyncio.sleep(1.4)
                await store.reconcile_orphans(user_id=user_id)
                assert (await _row(task.task_id))[0] == "running"
                assert task.process.poll() is None
                print("PASS preparation remains alive beyond its initial lease")

                child = task.process
                await store.mark_terminal(task.task_id, status="failed", error="Task owner lease expired")
                await launcher.reconcile_terminal_tasks()
                await asyncio.wait_for(execution, 5)
                detail = await launcher.get_task_status(task.task_id)
                assert detail["status"] == "failed" and detail["error_message"] == "Task owner lease expired"
                assert child.poll() is not None
                assert (await _row(task.task_id))[0] == detail["status"]
                print("PASS durable failure stops preparation and synchronizes detail status")

                task, execution = await start("preparing-cancel")
                child = task.process
                assert await launcher.cancel_task(task.task_id)
                await asyncio.wait_for(execution, 5)
                assert (await launcher.get_task_status(task.task_id))["status"] == "cancelled"
                assert (await _row(task.task_id))[0] == "cancelled" and child.poll() is not None
                print("PASS cancellation stops preparation and remains cancelled")

                mode = "failure"
                task, execution = await start("preparing-failure")
                await asyncio.wait_for(execution, 5)
                detail = await launcher.get_task_status(task.task_id)
                assert detail["status"] == "failed" and "synthetic preparation failure" in detail["error_message"]
                assert (await _row(task.task_id))[0] == "failed"
                print("PASS startup exceptions are finalized rather than left running")

                mode = "success"
                task, execution = await start("preparing-success")
                await asyncio.wait_for(execution, 5)
                detail = await launcher.get_task_status(task.task_id)
                assert detail["status"] == "completed" and detail["error_message"] is None
                assert "preparation complete" in await launcher.get_task_log(task.task_id)
                assert not (launcher._log_dir / task.task_id / "qlib_provider.json").exists()
                print("PASS preparation result, retained log, and null error on success")
            finally:
                for execution in executions:
                    if not execution.done():
                        execution.cancel()
                await asyncio.gather(*executions, return_exceptions=True)
                for child in children:
                    if child.poll() is None:
                        child.kill()
                        child.wait(timeout=5)


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
        await _check_preparation_lifecycle(store, prefix, user_id, task_ids)
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
