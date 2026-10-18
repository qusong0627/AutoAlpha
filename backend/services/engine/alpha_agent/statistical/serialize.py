"""统计派 · 结果序列化（审计产物的唯一形状）。

``MiningResult`` 里有 numpy 标量、tuple、嵌套 dataclass、Timestamp——直接
``json.dumps`` 会在运行时炸（``np.float64`` 不是 JSON 类型）。这里一处转换
（:func:`jsonable`），runner 落盘、API 出参、任务详情页共用，避免三份
「差不多」的 to_dict 各自漏字段。

审计契约：**入选与未入选都给全**——未入选者带拒因与已算出的统计量，
复核 IC 数字异动时先看这份报告（谁被哪条闸拦下、拦在哪个数上）。
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

import numpy as np
import pandas as pd

from . import engine as _engine


def _plain(value: Any) -> Any:
    return value.item() if isinstance(value, np.generic) else value


def jsonable(obj: Any) -> Any:
    """递归转成 JSON 可序列化结构（dataclass / numpy / Timestamp / tuple）。"""
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, (pd.Timestamp,)):
        return obj.isoformat()
    if isinstance(obj, np.ndarray):
        return [jsonable(v) for v in obj.tolist()]
    return _plain(obj)


def json_safe(obj: Any) -> Any:
    """``jsonable`` + 非有限浮点（NaN/±Inf）→ ``None``。

    ``json.dumps`` 默认把 NaN 写成裸字面量 ``NaN``——那是 Python 方言，不是
    JSON：Postgres ``json`` 列直接拒收（``Token "NaN" is invalid``），
    ``JSON.parse`` 也解不出。被闸门拒掉的周期本来就没有 IC（NaN 是「没数」，
    不是 0），落成 ``None`` 与展示面「缺失一律 —」同一口径。
    """
    def walk(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v) for v in value]
        if isinstance(value, float) and not np.isfinite(value):
            return None
        return value

    return walk(jsonable(obj))


def result_to_dict(result: _engine.MiningResult) -> dict:
    """``MiningResult`` → 审计字典（任务详情 / 落盘共用）。"""
    return {
        "algorithm": "statistical",
        "selected": list(result.selected),
        "window": jsonable(result.window),
        "horizons": list(result.horizons),
        "panel_rows": result.panel_rows,
        "n_symbols": result.n_symbols,
        "universe": result.universe,
        "neutralize_mode": result.neutralize_mode,
        "neutralize": jsonable(result.neutralize_report),
        # n_kept 是 property，asdict 拿不到——显式补上（过滤报告的主读数）
        "filter": {**jsonable(result.filter_report), "n_kept": result.filter_report.n_kept},
        "candidates": [jsonable(c) for c in result.candidates],
        # Tier 3：去重记录与合成因子证据（未启用/退化 → None，缺失不是空报告）
        "dedup": jsonable(result.dedup),
        "composite": jsonable(result.composite),
    }
