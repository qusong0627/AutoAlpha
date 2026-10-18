"""全局股票池 - 内置池 seed 与 TXT 刷新（幂等）。

- `seed_builtin_pools*`：把 `builtins.BUILTIN_POOLS` upsert 进 `qm_stock_pool`
  （scope=global, is_system=true），只刷新系统拥有字段，不动人工状态；
- `refresh_builtin_txts*`：从 QuantDB 拉内置池成分写成成员 TXT（唯一事实源），
  并同步 DB 计数/校验和。QuantDB 未就绪时跳过（resolver 有自愈兜底）；
- `run_builtin_pool_refresh_worker`：每日（每 6h 检查、按日期去重）刷新一次，
  指数成分调整自动跟进，无需人工发布。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import text

from .builtins import BUILTIN_POOLS, seed_rows
from .materializer import pool_txt_path, to_api_members, write_pool_txt
from .normalize import checksum_symbols, normalize_symbols
from .repository import ensure_tables, ensure_tables_sync
from .resolver import _fetch_builtin_symbols

logger = logging.getLogger(__name__)

_UPSERT_SQL = """
INSERT INTO qm_stock_pool (
    pool_id, code, name, description, market, pool_type, scope,
    tenant_id, owner_user_id, status, source_kind, source_ref,
    is_system, created_by, updated_by
) VALUES (
    :pool_id, :code, :name, :description, :market, :pool_type, :scope,
    :tenant_id, :owner_user_id, 'active',
    :source_kind, :source_ref, :is_system, :created_by, :updated_by
)
ON CONFLICT (pool_id) DO UPDATE SET
    name = EXCLUDED.name,
    description = EXCLUDED.description,
    market = EXCLUDED.market,
    pool_type = EXCLUDED.pool_type,
    source_kind = EXCLUDED.source_kind,
    source_ref = EXCLUDED.source_ref,
    is_system = EXCLUDED.is_system,
    updated_at = NOW(),
    updated_by = EXCLUDED.updated_by
"""


def _pool_row_updates_sync(session, pool_id: str, api_symbols: list[str]) -> None:
    session.execute(
        text(
            """
            UPDATE qm_stock_pool
               SET file_path = :path, symbol_count = :count, checksum = :checksum,
                   updated_at = NOW()
             WHERE pool_id = :pid
            """
        ),
        {
            "pid": pool_id,
            "path": pool_txt_path("global", pool_id.removeprefix("sys_")),
            "count": len(api_symbols),
            "checksum": checksum_symbols(sorted(api_symbols)) if api_symbols else None,
        },
    )


def refresh_builtin_txts_sync() -> int:
    """刷新全部内置池成分 TXT（同步，启动期 / worker 使用）。返回成功刷新数。"""
    from backend.shared.database_pool import get_db

    ok = 0
    try:
        with get_db() as session:
            for builtin in BUILTIN_POOLS:
                symbols = _fetch_builtin_symbols(builtin)
                if not symbols:
                    logger.info("内置池 %s 暂无成分（数据源未就绪，保留现状）", builtin.code)
                    continue
                api = to_api_members(normalize_symbols(symbols, builtin.market), builtin.market)
                path = pool_txt_path("global", builtin.code)
                try:
                    write_pool_txt(path, api, header=f"builtin {builtin.code} ({builtin.name})")
                except OSError as exc:
                    logger.warning("内置池 TXT 写入失败 %s: %s", path, exc)
                    continue
                _pool_row_updates_sync(session, f"sys_{builtin.code}", api)
                ok += 1
            session.commit()
    except Exception as exc:  # noqa: BLE001
        logger.warning("内置池 TXT 刷新失败（不影响启动）: %s", exc)
        return 0
    logger.info("内置池成分 TXT 刷新完成: %d/%d", ok, len(BUILTIN_POOLS))
    return ok


async def refresh_builtin_txts() -> int:
    return await asyncio.to_thread(refresh_builtin_txts_sync)


async def seed_builtin_pools(session) -> int:
    """异步 seed（admin / engine 启动期使用）。返回成功处理的池数量。"""
    await ensure_tables(session)
    rows = seed_rows()
    ok = 0
    for row in rows:
        try:
            # 逐行 savepoint：单行失败（如 code 已被非系统池占用）不拖垮整批
            async with session.begin_nested():
                await session.execute(text(_UPSERT_SQL), row)
            ok += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("内置池 seed 跳过 %s: %s", row.get("pool_id"), exc)
    await session.commit()
    logger.info("内置股票池 seed 完成: %d/%d", ok, len(rows))
    return ok


def seed_builtin_pools_sync() -> int:
    """同步 seed（`main_oss.py` 启动期使用）。

    失败仅告警，不影响主流程启动（与 `_ensure_seed_admin` 同策略）。
    """
    try:
        from backend.shared.database_pool import get_db
    except ImportError:  # pragma: no cover
        from shared.database_pool import get_db  # type: ignore

    rows = seed_rows()
    ok = 0
    try:
        with get_db() as session:
            ensure_tables_sync(session)
            for row in rows:
                try:
                    with session.begin_nested():
                        session.execute(text(_UPSERT_SQL), row)
                    ok += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning("内置池 seed 跳过 %s: %s", row.get("pool_id"), exc)
            session.commit()
        logger.info("内置股票池 seed 完成(同步): %d/%d", ok, len(rows))
        return ok
    except Exception as exc:  # noqa: BLE001
        logger.warning("内置股票池 seed 失败（不影响启动）: %s", exc)
        return 0


def pool_storage_ready() -> str | None:
    """确保成员 TXT 目录存在（启动期调用），返回路径。"""
    from .materializer import pool_dir

    target = pool_dir()
    try:
        target.mkdir(parents=True, exist_ok=True)
        return str(target)
    except OSError as exc:
        logger.warning("股票池 TXT 目录创建失败 %s: %s", target, exc)
        return None


async def run_builtin_pool_refresh_worker(check_interval_seconds: int = 6 * 3600) -> None:
    """每日刷新内置池 TXT（按日期去重；启动后先跑一次）。"""
    last_run_date = ""
    while True:
        try:
            now = datetime.now(timezone.utc)
            today = now.strftime("%Y%m%d")
            if today != last_run_date:
                last_run_date = today
                await refresh_builtin_txts()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("内置池 TXT 刷新 worker 失败: %s", exc)
        await asyncio.sleep(max(300, int(check_interval_seconds or 21600)))
