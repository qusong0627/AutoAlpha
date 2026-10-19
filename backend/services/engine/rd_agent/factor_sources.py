"""因子数据溯源：从因子代码静态提取数据依赖（H5 退役后的 QuantDB 契约）。

AutoAlpha 解析因子代码中的数据集依赖后记入
``metadata.sourceDatasets`` / 回测台账 ``params``，多年后重跑可知当初算的是
哪几份数据。规则：

1. 文件顶部 ``SOURCE_DATASETS = [...]`` 声明优先（LLM/模板按契约照实列出
   ``qd.daily/financial/minute`` 用到的数据集）；
2. 无声明则正则兜底：``qd.daily('ds')`` / ``qd.financial('tbl')`` /
   ``qd.minute('ds')`` 的首个字符串参数；
3. 面板 ``$`` 列（``qd.panel()`` 默认 ``$`` 前缀价量列）→ 追加 ``daily_forward``
   （面板基础列的源数据集，与执行器口径一致）。

只做静态扫描，不执行代码；解析失败返回 []（调用方按无声明处理）。
"""

from __future__ import annotations

import ast
import re

#: ``qd.daily('l1_factors', ...)`` / ``qd.financial('balance', ...)`` /
#: ``qd.minute('min5_kline', ...)`` —— 首参数据集名（单双引号皆可）。
_RE_QD_DATASET = re.compile(
    r"qd\.(?:daily|financial|minute)\s*\(\s*['\"]([A-Za-z0-9_]+)['\"]"
)

#: 面板 ``$`` 列 token（``df["$close"]`` 下标键是规范写法）。
_RE_DOLLAR_TOKEN = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*")

#: 面板基础列的源数据集（panel.parquet 由它构建，见 panel_cache）。
PANEL_SOURCE_DATASET = "daily_forward"


def _declared_datasets(code: str) -> list[str] | None:
    """文件顶部 ``SOURCE_DATASETS = [...]`` 字面量；无声明返回 None。

    非法字面量（动态拼接）同样返回 None，交由正则兜底——宁可多扫，不误信。
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "SOURCE_DATASETS"
        ):
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, SyntaxError):
                return None
            if isinstance(value, (list, tuple)):
                return [str(v) for v in value]
            return None
    return None


def scan_source_datasets(code: str) -> list[str]:
    """从因子代码提取源数据集 key 列表（去重、稳定序）。"""
    if not code:
        return []
    declared = _declared_datasets(code)
    found: list[str] = []
    if declared is not None:
        found.extend(d for d in declared if d and d not in found)
    else:
        for m in _RE_QD_DATASET.finditer(code):
            ds = m.group(1)
            if ds not in found:
                found.append(ds)
    if _RE_DOLLAR_TOKEN.search(code) and PANEL_SOURCE_DATASET not in found:
        found.append(PANEL_SOURCE_DATASET)
    return found


def qd_version() -> str:
    """取数 SDK 版本（语义化；取不到时回 ``unknown``，绝不抛）。"""
    try:
        from backend.services.engine.rd_agent.quantdb_data import __version__

        return str(__version__)
    except Exception:  # noqa: BLE001
        return "unknown"


def panel_data_version(root: str | None = None) -> str | None:
    """面板数据版本（panel.meta 指纹；无缓存返回 None）。"""
    try:
        from backend.services.engine.data_platform.panel_cache import (
            _read_meta as _rm,
            panel_dir as _pd,
        )
        from backend.services.engine.rd_agent.rd_loop_wrapper import RDLoopWrapper

        base = root or RDLoopWrapper._resolve_quantdb_dir()
        if not base:
            return None
        meta = _rm(_pd(base) / "daily_pv.meta.json")
        if isinstance(meta, dict):
            fp = meta.get("source_fingerprint")
            return str(fp) if fp else None
        return None
    except Exception:  # noqa: BLE001
        return None


def source_snapshot(code: str, *, root: str | None = None) -> dict[str, object]:
    """一次取齐溯源三件套（各落点直接塞进 metadata/params）。"""
    return {
        "sourceDatasets": scan_source_datasets(code),
        "qdVersion": qd_version(),
        "dataVersion": panel_data_version(root),
    }
