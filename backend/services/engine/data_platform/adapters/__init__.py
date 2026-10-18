"""数据源适配器注册中心。

外部调用：
    from backend.services.engine.data_platform.adapters import register_all
    register_all()

A 股唯一数据源为 quantdb_local（本地 parquet）。
akshare / efinance / yahoo_finance / simonlin_global 仅服务 HK/US 市场。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

logger = logging.getLogger(__name__)

# (name, registrar_callable)
_REGISTRARS: list[tuple[str, Callable[[], bool]]] = []


def _collect() -> None:
    """惰性收集所有 register() 函数，import 失败的适配器静默跳过。"""
    global _REGISTRARS
    if _REGISTRARS:
        return
    out: list[tuple[str, Callable[[], bool]]] = []

    for mod_name in (
        # A 股唯一数据源：QuantDB 本地 parquet
        "backend.services.engine.data_platform.adapters.quantdb_local_adapter",
        # HK/US 市场数据源
        "backend.services.engine.data_platform.adapters.yahoo_finance_adapter",
        "backend.services.engine.data_platform.adapters.simonlin_global_adapter",
        "backend.services.engine.data_platform.adapters.akshare_adapter",
        "backend.services.engine.data_platform.adapters.efinance_adapter",
    ):
        try:
            import importlib
            m = importlib.import_module(mod_name)
            if hasattr(m, "register"):
                out.append((mod_name.rsplit(".", 1)[-1], m.register))
        except Exception as exc:  # noqa: BLE001
            logger.warning("import adapter module %s failed: %s", mod_name, exc)

    _REGISTRARS = out


def register_all() -> dict[str, bool]:
    """注册全部可用适配器；返回 {name: success}。"""
    _collect()
    results: dict[str, bool] = {}
    for name, fn in _REGISTRARS:
        try:
            results[name] = bool(fn())
        except Exception as exc:  # noqa: BLE001
            logger.error("adapter %s register() raised: %s", name, exc)
            results[name] = False
    logger.info("Data-source adapters registered: %s", results)
    return results


def list_known() -> list[str]:
    _collect()
    return [n for n, _ in _REGISTRARS]
