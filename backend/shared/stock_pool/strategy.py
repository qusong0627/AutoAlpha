"""策略运行环境股票池支持（Strategy Lab SDK + 模拟盘 code 模式共用）。

用户在策略里写一行即可::

    def setup(ctx):
        ctx.universe = "csi300"
        ctx.stock_pool = "pool:csi1000"   # ← 多这一行

语义：``universe ∩ 股票池``（保序取 universe）；``stock_pool`` 为空/``all``
时不过滤。空池或零交集一律抛错，绝不静默退化为全市场（与训练/推理一致）。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class PoolUniverseResult:
    symbols: list[str] = field(default_factory=list)
    pool_id: str | None = None
    pool_checksum: str | None = None
    pool_symbol_count: int = 0
    dropped: int = 0
    warnings: list[str] = field(default_factory=list)


def apply_pool_to_universe(
    symbols: Sequence[str] | Iterable[str] | None,
    pool_ref: str | None,
    tenant_id: str | None = None,
    user_id: str | None = None,
    *,
    strict: bool = True,
) -> PoolUniverseResult:
    """把 ``universe`` 符号表按股票池引用裁剪，返回交集（前缀式，保序）。

    Args:
        symbols: universe 解析出的符号表（任意口径，内部归一为前缀式）。
        pool_ref: 池引用（``pool:csi300`` / 裸 code / ``list:...`` /
            ``file:...`` / ``all``），空则不过滤。
        tenant_id/user_id: 池可见域（用户私有池需要；缺省只能解析全局/内置池）。
        strict: True 时空池/零交集抛 ``ValueError``；False 时返回原表并记警告。
    """
    from .filters import _to_prefix, intersect_symbols
    from .resolver import ResolveContext, resolver as pool_resolver

    base = [_to_prefix(s) for s in (symbols or [])]
    ref = (pool_ref or "").strip()
    if not ref:
        return PoolUniverseResult(symbols=base)

    snapshot = pool_resolver.resolve_sync(
        ref,
        ResolveContext(tenant_id=tenant_id, user_id=user_id),
        strict=strict,
    )
    if snapshot.unfiltered:
        return PoolUniverseResult(
            symbols=base,
            pool_id=snapshot.pool_id,
            pool_checksum=snapshot.checksum,
            warnings=list(snapshot.warnings or []),
        )

    allowed = {_to_prefix(s) for s in (snapshot.api_symbols or [])}
    allowed.discard("")
    if not allowed:
        msg = f"股票池 {snapshot.pool_id} 解析为空池，拒绝退化为全市场"
        if strict:
            raise ValueError(msg)
        logger.warning("%s", msg)
        return PoolUniverseResult(
            symbols=base,
            pool_id=snapshot.pool_id,
            pool_checksum=snapshot.checksum,
            warnings=[msg, *list(snapshot.warnings or [])],
        )

    kept = intersect_symbols(base, allowed)
    dropped = len(base) - len(kept)
    logger.info(
        "策略股票池: pool_id=%s checksum=%s kept=%d dropped=%d pool_size=%d",
        snapshot.pool_id,
        snapshot.checksum,
        len(kept),
        dropped,
        len(allowed),
    )
    if not kept:
        msg = (
            f"股票池 {snapshot.pool_id} 与 universe 零交集 "
            f"（池 {len(allowed)} 只 / universe {len(base)} 只），拒绝退化为全市场"
        )
        if strict:
            raise ValueError(msg)
        logger.warning("%s", msg)
        return PoolUniverseResult(
            symbols=[],
            pool_id=snapshot.pool_id,
            pool_checksum=snapshot.checksum,
            pool_symbol_count=len(allowed),
            dropped=dropped,
            warnings=[msg, *list(snapshot.warnings or [])],
        )
    return PoolUniverseResult(
        symbols=kept,
        pool_id=snapshot.pool_id,
        pool_checksum=snapshot.checksum,
        pool_symbol_count=len(allowed),
        dropped=dropped,
        warnings=list(snapshot.warnings or []),
    )


def validate_pool_ref_format(ref: Any) -> str | None:
    """校验池引用格式（SDK 用户代码环境用，不查 DB）。

    返回 None 表示合法，否则返回错误信息。
    """
    import re

    if ref is None:
        return None
    if not isinstance(ref, str) or not ref.strip():
        return "stock_pool must be a non-empty string"
    raw = ref.strip()
    lowered = raw.lower()
    for prefix in ("pool:", "pool_id:", "list:", "file:", "cos://", "user_pool:"):
        if lowered.startswith(prefix):
            return None
    if lowered in ("all", "all_a_share", "*"):
        return None
    if "/" in raw or raw.endswith(".txt") or raw.endswith(".csv"):
        return None
    if re.fullmatch(r"[A-Za-z0-9_\-]+", raw):
        return None
    return (
        f"stock_pool={raw!r} 格式不支持，应为 pool:<code> / list:... / "
        "file:... / 裸 code / all"
    )
