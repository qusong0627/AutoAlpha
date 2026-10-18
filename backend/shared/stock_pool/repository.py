"""全局股票池 - 数据访问层（v2：单表元信息 + TXT 成员）。

保存即生效：成员编辑 → 重写 TXT → 更新计数/校验和，没有草稿/发布。
引用守卫：qm_stock_pool_binding 记录长生命周期引用，被引用的池不可归档。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from collections.abc import Sequence

from sqlalchemy import text

from .constants import SCOPE_GLOBAL, STATUS_ACTIVE, STATUS_ARCHIVED
from .normalize import (
    checksum_symbols,
    normalize_market,
    normalize_symbols,
)
from .schemas import StockPool, StockPoolCreate, StockPoolUpdate

logger = logging.getLogger(__name__)

_MIGRATION_SQL = (
    Path(__file__).resolve().parent / "migrations" / "001_create_stock_pool.sql"
)

_TABLES_READY = False


# ---------------------------------------------------------------------------
# 建表
# ---------------------------------------------------------------------------
async def ensure_tables(session) -> None:
    """幂等建表/升级。SQL 文件是唯一事实源（main_oss 启动期执行同一份）。"""
    global _TABLES_READY
    if _TABLES_READY:
        return
    try:
        sql = _MIGRATION_SQL.read_text(encoding="utf-8")
    except OSError as exc:  # pragma: no cover - 仅在文件缺失时触发
        logger.error("股票池建表 SQL 读取失败: %s", exc)
        raise
    for statement in _split_statements(sql):
        await session.execute(text(statement))
    await session.commit()
    _TABLES_READY = True


def ensure_tables_sync(session) -> None:
    """同步版建表（main_oss 启动期使用）。"""
    sql = _MIGRATION_SQL.read_text(encoding="utf-8")
    for statement in _split_statements(sql):
        session.execute(text(statement))
    session.commit()


def _split_statements(sql: str) -> list[str]:
    """按分号拆分 SQL 语句，跳过纯注释行。"""
    lines: list[str] = []
    for line in sql.splitlines():
        stripped = line.strip()
        if stripped.startswith("--") or not stripped:
            continue
        lines.append(line)
    body = "\n".join(lines)
    return [s.strip() for s in body.split(";") if s.strip()]


def _row_to_pool(row) -> StockPool:
    return StockPool(**dict(row))


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# 池 CRUD
# ---------------------------------------------------------------------------
async def list_pools(
    session,
    *,
    market: str | None = None,
    pool_type: str | None = None,
    status: str | None = None,
    scope: str | None = None,
    keyword: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[StockPool], int]:
    where: list[str] = []
    params: dict[str, Any] = {"limit": int(limit), "offset": int(offset)}

    if market:
        where.append("market = :market")
        params["market"] = normalize_market(market)
    if pool_type:
        where.append("pool_type = :pool_type")
        params["pool_type"] = pool_type
    if status:
        where.append("status = :status")
        params["status"] = status
    if scope:
        where.append("scope = :scope")
        params["scope"] = scope
    if keyword:
        where.append("(code ILIKE :kw OR name ILIKE :kw)")
        params["kw"] = f"%{keyword}%"

    clause = f"WHERE {' AND '.join(where)}" if where else ""

    total = (
        await session.execute(
            text(f"SELECT COUNT(*) FROM qm_stock_pool {clause}"), params
        )
    ).scalar() or 0

    rows = (
        (
            await session.execute(
                text(
                    f"""
                SELECT * FROM qm_stock_pool
                {clause}
                ORDER BY is_system DESC, market ASC, code ASC
                LIMIT :limit OFFSET :offset
                """
                ),
                params,
            )
        )
        .mappings()
        .all()
    )

    return [_row_to_pool(r) for r in rows], int(total)


async def get_pool(session, pool_id: str) -> StockPool | None:
    row = (
        (
            await session.execute(
                text("SELECT * FROM qm_stock_pool WHERE pool_id = :pid"),
                {"pid": pool_id},
            )
        )
        .mappings()
        .first()
    )
    return _row_to_pool(row) if row else None


async def get_pool_by_code(
    session,
    code: str,
    *,
    scope: str = SCOPE_GLOBAL,
    tenant_id: str | None = None,
    owner_user_id: str | None = None,
) -> StockPool | None:
    """按 (scope, tenant, code) 查池。

    `owner_user_id` 传值时额外限定 owner —— **scope='user' 必须传**，
    否则不同用户的同名私有池会互相命中。
    """
    sql = """
        SELECT * FROM qm_stock_pool
        WHERE scope = :scope
          AND COALESCE(tenant_id, '') = COALESCE(:tid, '')
          AND code = :code
    """
    params: dict[str, Any] = {"scope": scope, "tid": tenant_id, "code": code}
    if owner_user_id is not None:
        sql += " AND owner_user_id = :owner"
        params["owner"] = str(owner_user_id)

    row = (await session.execute(text(sql), params)).mappings().first()
    return _row_to_pool(row) if row else None


async def create_pool(
    session, payload: StockPoolCreate, actor: str = "system"
) -> StockPool:
    pool_id = _new_pool_id(payload.code)
    await session.execute(
        text(
            """
            INSERT INTO qm_stock_pool (
                pool_id, code, name, description, market, pool_type, scope,
                tenant_id, owner_user_id, status, source_kind, source_ref,
                is_system, created_by, updated_by
            ) VALUES (
                :pool_id, :code, :name, :description, :market, :pool_type, :scope,
                :tenant_id, :owner_user_id, :status,
                :source_kind, :source_ref, FALSE, :actor, :actor
            )
            """
        ),
        {
            "pool_id": pool_id,
            "code": payload.code,
            "name": payload.name,
            "description": payload.description,
            "market": normalize_market(payload.market),
            "pool_type": payload.pool_type,
            "scope": payload.scope,
            "tenant_id": payload.tenant_id,
            "owner_user_id": payload.owner_user_id,
            "status": STATUS_ACTIVE,
            "source_kind": payload.source_kind,
            "source_ref": payload.source_ref,
            "actor": actor,
        },
    )
    await session.commit()
    created = await get_pool(session, pool_id)
    assert created is not None
    return created


async def update_pool(
    session, pool_id: str, payload: StockPoolUpdate, actor: str = "system"
) -> StockPool | None:
    sets: list[str] = ["updated_at = NOW()", "updated_by = :actor"]
    params: dict[str, Any] = {"pid": pool_id, "actor": actor}

    if payload.name is not None:
        sets.append("name = :name")
        params["name"] = payload.name
    if payload.description is not None:
        sets.append("description = :description")
        params["description"] = payload.description
    if payload.status is not None:
        sets.append("status = :status")
        params["status"] = payload.status

    await session.execute(
        text(f"UPDATE qm_stock_pool SET {', '.join(sets)} WHERE pool_id = :pid"), params
    )
    await session.commit()
    return await get_pool(session, pool_id)


async def set_pool_status(
    session, pool_id: str, status: str, actor: str = "system"
) -> None:
    await session.execute(
        text(
            """
            UPDATE qm_stock_pool
               SET status = :status, updated_at = NOW(), updated_by = :actor
             WHERE pool_id = :pid
            """
        ),
        {"pid": pool_id, "status": status, "actor": actor},
    )
    await session.commit()


async def archive_pool(session, pool_id: str, actor: str = "system") -> list[str]:
    """归档。返回阻止归档的引用描述（非空表示已被引用）。"""
    usages = await list_usages(session, pool_id)
    if usages:
        return [f"{u['target_type']}:{u['target_id']}" for u in usages]
    await set_pool_status(session, pool_id, STATUS_ARCHIVED, actor)
    return []


async def delete_pool(session, pool_id: str, *, unlink_txt: bool = True) -> None:
    """硬删（binding 由外键级联删除）。仅允许已归档的池。成员 TXT 一并清理。"""
    pool = await get_pool(session, pool_id)
    await session.execute(
        text("DELETE FROM qm_stock_pool WHERE pool_id = :pid"), {"pid": pool_id}
    )
    await session.commit()
    if pool is not None and unlink_txt and pool.file_path:
        try:
            Path(pool.file_path).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("股票池 TXT 删除失败 %s: %s", pool.file_path, exc)


# ---------------------------------------------------------------------------
# 成员（TXT 即唯一事实源）
# ---------------------------------------------------------------------------
async def save_members(
    session,
    pool: StockPool,
    raw_symbols: Sequence[str],
    *,
    actor: str = "system",
) -> dict[str, Any]:
    """覆盖式保存成员：归一化去重 → 写 TXT → 更新计数/校验和。立即生效。

    返回 {accepted, rejected, duplicates, symbol_count, checksum, file_path}。
    坏代码被拒绝并回传样本（不是静默丢掉）。
    """
    from .materializer import (
        pool_txt_path,
        resolve_pool_txt,
        to_api_members,
        write_pool_txt,
    )

    mk = normalize_market(pool.market)
    seen: set[str] = set()
    accepted: list[str] = []
    rejected: list[str] = []
    duplicates = 0
    for item in raw_symbols or []:
        code = str(item or "").strip()
        if not code:
            continue
        norm = normalize_symbols([code], mk)
        if not norm:
            rejected.append(code[:32])
            continue
        if norm[0] in seen:
            duplicates += 1
            continue
        seen.add(norm[0])
        accepted.append(norm[0])

    path = pool.file_path or pool_txt_path(
        pool.scope,
        pool.code,
        tenant_id=pool.tenant_id,
        owner_user_id=pool.owner_user_id,
    )
    if pool.scope != SCOPE_GLOBAL:
        # 用户 / 租户池强制进隔离子目录；DB 里残留的旧扁平 file_path 仅读兼容，
        # 本次保存即迁移到新位置并回写 file_path。
        isolated = pool_txt_path(
            pool.scope,
            pool.code,
            tenant_id=pool.tenant_id,
            owner_user_id=pool.owner_user_id,
        )
        if Path(path).resolve() != Path(isolated).resolve():
            logger.info("股票池 TXT 迁移到隔离目录: %s -> %s", path, isolated)
        path = isolated
    write_pool_txt(
        path, to_api_members(accepted, mk), header=f"{pool.code} ({pool.name})"
    )

    digest = checksum_symbols(accepted) if accepted else None
    await session.execute(
        text(
            """
            UPDATE qm_stock_pool
               SET file_path = :path,
                   symbol_count = :count,
                   checksum = :checksum,
                   updated_at = NOW(),
                   updated_by = :actor
             WHERE pool_id = :pid
            """
        ),
        {
            "pid": pool.pool_id,
            "path": path,
            "count": len(accepted),
            "checksum": digest,
            "actor": actor,
        },
    )
    await session.commit()
    return {
        "accepted": len(accepted),
        "rejected": len(rejected),
        "duplicates": duplicates,
        "rejected_samples": rejected[:20],
        "symbol_count": len(accepted),
        "checksum": digest,
        "file_path": path,
    }


def read_members(pool: StockPool) -> list[str]:
    """读成员 TXT（前缀式）。池没有 file_path（内置池未刷新）时按规则猜测路径。

    猜测走 `resolve_pool_txt`：新隔离子目录优先，旧扁平路径读兼容。
    """
    from .materializer import read_pool_txt, resolve_pool_txt

    if not pool.file_path:
        pool.file_path = resolve_pool_txt(
            pool.scope,
            pool.code,
            tenant_id=pool.tenant_id,
            owner_user_id=pool.owner_user_id,
        )
    return read_pool_txt(pool.file_path)


# ---------------------------------------------------------------------------
# 绑定 / 引用
# ---------------------------------------------------------------------------
async def list_usages(
    session, pool_id: str, *, target_type: str | None = None
) -> list[dict[str, Any]]:
    sql = """
        SELECT id, pool_id, target_type, target_id, mode, priority,
               tenant_id, user_id, created_at, updated_at
          FROM qm_stock_pool_binding
         WHERE pool_id = :pid
    """
    params: dict[str, Any] = {"pid": pool_id}
    if target_type:
        sql += " AND target_type = :ttype"
        params["ttype"] = target_type
    sql += " ORDER BY target_type, target_id"

    rows = (await session.execute(text(sql), params)).mappings().all()
    return [dict(r) for r in rows]


async def list_pools_for_target(
    session, target_type: str, target_id: str
) -> list[dict[str, Any]]:
    """反查：某个目标（策略/模型/账户）绑了哪些池。"""
    rows = (
        (
            await session.execute(
                text(
                    """
                    SELECT b.pool_id, b.mode, b.priority, p.code, p.name,
                           p.market, p.status, p.symbol_count
                      FROM qm_stock_pool_binding b
                      JOIN qm_stock_pool p ON p.pool_id = b.pool_id
                     WHERE b.target_type = :ttype AND b.target_id = :tid
                     ORDER BY b.priority ASC, p.code ASC
                    """
                ),
                {"ttype": target_type, "tid": target_id},
            )
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


async def unbind_pool(
    session, pool_id: str, target_type: str, target_id: str
) -> int:
    """解除单个引用，返回删除行数。"""
    result = await session.execute(
        text(
            """
            DELETE FROM qm_stock_pool_binding
             WHERE pool_id = :pid AND target_type = :ttype AND target_id = :tid
            """
        ),
        {"pid": pool_id, "ttype": target_type, "tid": target_id},
    )
    await session.commit()
    return int(result.rowcount or 0)


async def count_bindings(session, pool_id: str) -> int:
    return int(
        (
            await session.execute(
                text(
                    "SELECT COUNT(*) FROM qm_stock_pool_binding WHERE pool_id = :pid"
                ),
                {"pid": pool_id},
            )
        ).scalar()
        or 0
    )


async def bind_pool(
    session,
    pool_id: str,
    target_type: str,
    target_id: str,
    *,
    mode: str = "filter",
    priority: int = 100,
    tenant_id: str | None = None,
    user_id: str | None = None,
    actor: str = "system",
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO qm_stock_pool_binding (
                pool_id, target_type, target_id, mode, priority,
                tenant_id, user_id, created_by
            ) VALUES (
                :pid, :ttype, :tid, :mode, :priority,
                :tenant_id, :user_id, :actor
            )
            ON CONFLICT (pool_id, target_type, target_id) DO UPDATE SET
                mode = EXCLUDED.mode,
                priority = EXCLUDED.priority,
                updated_at = NOW()
            """
        ),
        {
            "pid": pool_id,
            "ttype": target_type,
            "tid": target_id,
            "mode": mode,
            "priority": int(priority),
            "tenant_id": tenant_id,
            "user_id": user_id,
            "actor": actor,
        },
    )
    await session.commit()


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _new_pool_id(code: str) -> str:
    import uuid

    return f"sp_{code[:24]}_{uuid.uuid4().hex[:8]}"


def today() -> Any:
    return _now().date()
