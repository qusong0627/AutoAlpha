"""旧用户池（`stock_pool_files`）→ 全局池登记桥（P4 轻量治理，v2 简化）。

设计取舍
--------
`stock_pool_files` 是「COS 文件 + `is_active` 单选」的旧模型，用户自己的池都在那里。
完整收编（数据迁移 + 前端改造 + 权限配额）已被明确搁置，因此这里只做**写侧登记**：

- 旧链路**完全不动**（文件仍上传、`is_active` 语义不变、AI 向导照旧读它）；
- 同时把这份池登记成一条 `scope=user` 的一等池（成员写 TXT，保存即生效），
  于是它能被 `PoolResolver` 以 `pool:<code>` 解析，也能出现在统一的池列表里；
- 失败只告警，**绝不影响旧链路**。

代码口径：成员统一经 `normalize` 归一后写 TXT（前缀式）。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any

from . import repository as repo
from .constants import POOL_TYPE_IMPORTED, SCOPE_USER
from .normalize import is_valid_symbol, normalize_symbols

logger = logging.getLogger(__name__)

SOURCE_KIND = "legacy_cos_file"


def _legacy_pool_code(pool_name: str, user_id: str) -> str:
    """由「用户 + 池名」生成稳定 code：同名池反复保存（哪怕内容变了）都命中
    同一条记录，内容变化直接覆盖成员 TXT（保存即生效）。

    用户维度必须参与 code，避免不同用户同名池互撞。
    """
    import re
    import unicodedata

    base = unicodedata.normalize("NFKC", str(pool_name or "")).strip()
    slug = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+", "_", base).strip("_")
    if not slug:
        slug = "legacy_pool"
    if len(slug) > 40:
        slug = slug[:40]
    uid = re.sub(r"[^0-9A-Za-z_]+", "", str(user_id or "anon")) or "anon"
    return f"legacy_u{uid}_{slug}"


async def register_legacy_pool_file(
    *,
    user_id: str,
    tenant_id: str | None,
    pool_name: str,
    file_key: str,
    file_url: str | None,
    code_hash: str | None,
    members: Sequence[dict[str, Any]],
) -> dict[str, Any] | None:
    """把一次旧式池文件保存登记为 `scope=user` 的一等池（成员落 TXT）。

    Returns: 登记结果 dict；任何失败都返回 None 并告警（不影响旧链路）。
    """
    try:
        return await _register(
            user_id=user_id,
            tenant_id=tenant_id,
            pool_name=pool_name,
            file_key=file_key,
            members=members,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "旧池文件登记为全局池失败（旧链路不受影响） user=%s pool=%s: %s",
            user_id,
            pool_name,
            exc,
        )
        return None


async def _register(
    *,
    user_id: str,
    tenant_id: str | None,
    pool_name: str,
    file_key: str,
    members: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    from backend.shared.database_manager_v2 import get_session

    symbols = normalize_symbols(
        [str(m.get("symbol") or "") for m in (members or [])], "CN"
    )
    valid = [s for s in symbols if is_valid_symbol(s, "CN")]
    if not valid:
        logger.info("旧池文件无可识别成分，跳过登记 pool=%s", pool_name)
        return {"skipped": True, "reason": "no valid symbol"}

    code = _legacy_pool_code(pool_name, str(user_id))

    async with get_session() as session:
        await repo.ensure_tables(session)

        pool = await repo.get_pool_by_code(
            session,
            code,
            scope=SCOPE_USER,
            tenant_id=tenant_id,
            owner_user_id=str(user_id),
        )
        if pool is None:
            from .schemas import StockPoolCreate

            pool = await repo.create_pool(
                session,
                StockPoolCreate(
                    code=code,
                    name=pool_name or code,
                    description="由旧式股票池文件（stock_pool_files）自动登记",
                    market="CN",
                    pool_type=POOL_TYPE_IMPORTED,
                    scope=SCOPE_USER,
                    tenant_id=tenant_id,
                    owner_user_id=str(user_id),
                    source_kind=SOURCE_KIND,
                    source_ref=file_key,
                ),
                actor=f"legacy:{user_id}",
            )

        result = await repo.save_members(
            session, pool, valid, actor=f"legacy:{user_id}"
        )

    logger.info(
        "旧池文件已登记为一等池: code=%s pool_id=%s members=%d",
        code,
        pool.pool_id,
        result.get("symbol_count", 0),
    )
    return {
        "pool_id": pool.pool_id,
        "code": code,
        "members": result.get("symbol_count", 0),
        "checksum": result.get("checksum"),
    }
