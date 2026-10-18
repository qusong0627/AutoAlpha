"""市场数据中枢解析 — 按市场返回对应 DataHub 实例。

智能策略 / 投研平台 / 推理等需要按 market 读取各市场本地 parquet 的模块，
统一通过本模块解析，避免各自硬编码 QuantDBDataHub（A 股）。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def get_hub_for_market(market: str | None = None) -> Any | None:
    """按市场返回对应 DataHub 实例（lazy import 避免循环依赖）。

    market: CN / HK / US / CRYPTO / FUTURES，缺省视为 CN。
    返回 None 表示市场无对应 hub 或不可用。
    """
    market_upper = str(market or "").upper() or "CN"

    _MARKET_HUB = {
        "CN": ("backend.services.engine.data_platform.quantdb_hub", "QuantDBDataHub"),
        "HK": ("backend.services.engine.data_platform.quanthk_hub", "QuantHKDataHub"),
        "US": ("backend.services.engine.data_platform.quantus_hub", "QuantUSDataHub"),
        "CRYPTO": ("backend.services.engine.data_platform.quantbc_hub", "QuantBCDataHub"),
        "FUTURES": ("backend.services.engine.data_platform.quantfutures_hub", "QuantFuturesDataHub"),
    }

    entry = _MARKET_HUB.get(market_upper)
    if entry is None:
        logger.debug("未知市场 %s，回退 CN", market_upper)
        entry = _MARKET_HUB["CN"]

    try:
        import importlib

        mod = importlib.import_module(entry[0])
        cls = getattr(mod, entry[1])
        return cls.get_instance()
    except Exception as exc:  # noqa: BLE001
        logger.debug("市场 %s hub 加载失败: %s", market_upper, exc)
        return None


def fetch_stock_list_for_market(market: str | None = None) -> dict[str, str]:
    """按市场加载 {suffix_symbol: name} 映射（best-effort）。

    非 A 股 hub 的 fetch_stock_list 若列名不同，这里做归一。
    """
    hub = get_hub_for_market(market)
    if hub is None or not getattr(hub, "available", True):
        return {}
    try:
        df = hub.fetch_stock_list()
        if df is None or df.empty:
            return {}
        symbol_col = "Symbol" if "Symbol" in df.columns else "symbol"
        name_col = "Name" if "Name" in df.columns else ("stock_name" if "stock_name" in df.columns else None)
        if symbol_col not in df.columns or name_col is None:
            return {}
        mapping: dict[str, str] = {}
        for _, row in df[[symbol_col, name_col]].dropna().iterrows():
            sym = str(row[symbol_col]).strip()
            nm = str(row[name_col]).strip()
            if sym and nm:
                mapping[sym] = nm
        return mapping
    except Exception as exc:  # noqa: BLE001
        logger.debug("市场 %s 标的名称加载失败: %s", market or "CN", exc)
        return {}
