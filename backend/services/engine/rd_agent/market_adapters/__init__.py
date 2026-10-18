"""市场适配器注册表"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import MarketAdapter

_registry: dict[str, type[MarketAdapter]] = {}


def register_adapter(cls: type[MarketAdapter]) -> type[MarketAdapter]:
    """装饰器：注册市场适配器"""
    _registry[cls.market_id] = cls
    return cls


def get_adapter(market_id: str) -> MarketAdapter:
    """获取市场适配器实例"""
    if market_id not in _registry:
        available = list(_registry.keys())
        raise ValueError(f"Unknown market: {market_id}. Available: {available}")
    return _registry[market_id]()


def list_markets() -> list[dict[str, str]]:
    """列出所有可用市场"""
    return [
        {
            "market_id": cls.market_id,
            "market_name": cls.market_name,
            "description": cls.description,
        }
        for cls in _registry.values()
    ]


def _crypto_enabled() -> bool:
    """生产环境可通过 ENABLE_CRYPTO=false 屏蔽区块链市场（默认开启）。"""
    raw = os.getenv("ENABLE_CRYPTO", "").strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    return True


# 导入适配器以触发注册（crypto 按开关注册）
from . import a_share, futures, hong_kong, us_stock  # noqa: F401, E402

if _crypto_enabled():
    from . import crypto  # noqa: F401, E402
