"""FactorMiner（arXiv:2602.14670）因子表达式工具包。

- ``dsl`` 白名单制解析器（禁 eval——检索式因子库把 eval() 当求值器的 RCE
  教训见因子挖掘方法论文档）；
- ``evaluator`` 宽面 pandas 求值（本仓唯一语义口径）；
- ``library`` 论文附录 D 的 110 条因子（逐字嵌入 + 导入即校验）。
"""

from .dsl import (
    FEATURES,
    OPERATORS,
    Call,
    Complexity,
    Feature,
    Node,
    OperatorSpec,
    complexity,
    min_history,
    parse,
    walk,
    window_arg_index,
)
from .evaluator import evaluate, missing_implementations
from .library import LIBRARY, LIBRARY_TEXT, PaperFactor, by_id, load_library

__all__ = [
    "FEATURES",
    "LIBRARY",
    "LIBRARY_TEXT",
    "OPERATORS",
    "Call",
    "Complexity",
    "Feature",
    "Node",
    "OperatorSpec",
    "PaperFactor",
    "by_id",
    "complexity",
    "evaluate",
    "load_library",
    "min_history",
    "missing_implementations",
    "parse",
    "walk",
    "window_arg_index",
]
