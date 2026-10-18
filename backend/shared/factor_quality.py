"""按路径加载训练侧因子质量度量（PFS / 多样性熵）——单一实现，避免口径漂移。

实现唯一来源：``docker/training/data/factor_quality.py``（纯 numpy/pandas）。
训练容器经 ``data/`` 目录挂载直接 import；engine/后端侧经本模块按路径加载
（OSS 容器 docker-compose 已把 ``./docker/training`` 挂到 ``/app/docker/training``，
与训练容器共用同一份文件——不要在此复制公式）。

加载失败（精简部署未挂载该目录）时返回 None，调用方应优雅降级
（跳过多维度量，不影响主流程）。
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from types import ModuleType

logger = logging.getLogger(__name__)

_MODULE_NAME = "qm_training_factor_quality"

_CANDIDATES = (
    # OSS 容器内（compose 挂载 ./docker/training → /app/docker/training）
    Path("/app/docker/training/data/factor_quality.py"),
    # 仓库内开发/测试（backend/shared/ → 仓库根）
    Path(__file__).resolve().parents[2] / "docker" / "training" / "data" / "factor_quality.py",
)

_cached: ModuleType | None = None
_loaded = False


def load_factor_quality() -> ModuleType | None:
    """返回 factor_quality 模块；不可用返回 None（结果缓存，只尝试一次）。"""
    global _cached, _loaded
    if _loaded:
        return _cached
    _loaded = True
    for path in _CANDIDATES:
        try:
            if not path.is_file():
                continue
            spec = importlib.util.spec_from_file_location(_MODULE_NAME, path)
            if spec is None or spec.loader is None:
                continue
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            _cached = mod
            logger.debug("factor_quality loaded from %s", path)
            return _cached
        except Exception as exc:  # noqa: BLE001 — 加载失败不阻断主流程
            logger.warning("factor_quality load failed at %s: %s", path, exc)
    logger.info("factor_quality unavailable (docker/training not mounted); quality metrics disabled")
    return None
