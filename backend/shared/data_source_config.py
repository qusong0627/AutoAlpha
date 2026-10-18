"""数据源勾选配置。

后台管理页面可选择启用哪些数据源（akshare/ccass/南向/北向/雅虎等）。
配置持久化到 config/data_sources_config.json（Docker 中 ./config 是挂载卷）。

默认：akshare、ccass、hsgt_south（南向）、hsgt_north（北向）启用；
      yahoo 默认停用（不勾选、不同步）。

用法:
    from backend.shared.data_source_config import get_enabled_sources, save_sources
    enabled = get_enabled_sources("HK")   # {"akshare": True, "yahoo": False, ...}
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 各市场可用数据源（market -> {source: {label, default}}）
# default=True 表示默认启用（勾选），False 表示默认停用
MARKET_SOURCES = {
    "A": {
        "quantdb": {"label": "QuantDB A股", "default": True},
        "akshare": {"label": "akshare", "default": True},
        "hsgt_north": {"label": "北向资金(沪深港通)", "default": True},
        "hsgt_south": {"label": "南向资金(港股通)", "default": True},
        "yahoo": {"label": "雅虎", "default": False},
    },
    "HK": {
        "akshare": {"label": "akshare", "default": True},
        "ccass": {"label": "CCASS机构持仓", "default": True},
        "hsgt_south": {"label": "南向资金(港股通)", "default": True},
        "hsgt_north": {"label": "北向资金(沪深港通)", "default": True},
        "yahoo": {"label": "雅虎", "default": False},
        "paid": {"label": "付费数据", "default": True},
    },
    "US": {
        "akshare": {"label": "akshare", "default": True},
        "yahoo": {"label": "雅虎", "default": False},
    },
    "BC": {
        "binance": {"label": "Binance", "default": True},
    },
    "FUTURES": {
        "akshare": {"label": "akshare", "default": True},
    },
}


def _config_path() -> Path:
    override = os.getenv("QM_DATA_SOURCE_CONFIG_FILE", "").strip()
    if override:
        return Path(override)
    return _PROJECT_ROOT / "config" / "data_sources_config.json"


def _default_config() -> dict[str, dict[str, bool]]:
    """生成默认配置（按 MARKET_SOURCES 的 default）。"""
    return {
        market: {src: meta["default"] for src, meta in sources.items()}
        for market, sources in MARKET_SOURCES.items()
    }


def _load() -> dict[str, dict[str, bool]]:
    path = _config_path()
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取数据源配置失败: %s，使用默认", exc)
    return _default_config()


def get_enabled_sources(market: str) -> dict[str, bool]:
    """返回某市场各数据源是否启用。"""
    cfg = _load()
    return cfg.get(market, _default_config().get(market, {}))


def is_source_enabled(market: str, source: str) -> bool:
    """某市场某数据源是否启用。"""
    return get_enabled_sources(market).get(source, False)


def save_sources(market: str, sources: dict[str, bool]) -> dict[str, bool]:
    """保存某市场的数据源勾选。返回保存后的状态。"""
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    cfg = _load()
    current = cfg.setdefault(market, _default_config().get(market, {}))
    # 只允许配置已知数据源
    known = MARKET_SOURCES.get(market, {})
    for src, enabled in sources.items():
        if src in known:
            current[src] = bool(enabled)
    cfg[market] = current

    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return dict(current)


def list_sources(market: str) -> list[dict]:
    """返回某市场数据源清单（含 label 和当前是否启用）。"""
    cfg = get_enabled_sources(market)
    known = MARKET_SOURCES.get(market, {})
    return [
        {"source": src, "label": meta["label"], "enabled": cfg.get(src, meta["default"])}
        for src, meta in known.items()
    ]
