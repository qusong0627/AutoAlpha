"""FM-DSL 结构原创闸（T-FM-06）：算子路谱 → 与库内既有表达式分级。

AlphaAgent 原创性约束（AST 相似度惩罚重复结构）的改造版：对表达式因子解析
AST → 算子路谱（前序 op 序列 + n-gram 多重集 + 树深/节点数）→ 与因子库既有
表达式算结构相似度，**重复结构在计算前拒（省算力）**；值级相关去重与其互补
（结构异但值同 → 值级库级闸兜底）。依赖 T-FM-01 的 FM-DSL 解析器（白名单制，
禁 eval），本模块只做纯函数统计，不执行任何代码。

分级（:func:`classify`）与硬拒政策——**只拒 ``clone``**：

- ``clone``（AST 逐位相等，数值按浮点值比较）：同式值必然重复，计算前拒零
  风险；
- ``literal_variant``（同 op 序列同特征、仅数字/窗口不同，如 fm024/fm025）：
  **恰是 T-FE-09 变体残差台账的计算对象**（「家族×窗口/变换变体」），预拒会
  掐死变体探索——交值级库级闸（0.5）兜底，本闸只标注；
- ``near``（trigram 余弦 ≥ 阈值 θ）：弱信号，只标注（未来生成循环的「饱和
  方向」输入，T-FM-04 接口）；
- ``novel``：无命中。

窗口字面量抽象为 ``#``、特征名保留：``$close`` 与 ``$volume`` 的结构差异是
**真差异**（值完全不同），不因 op 序列相同而误判同形；op 序列不足 3 个时
trigram 档为空 → 除非 clone/变体否则如实 novel（宁漏勿误）。
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .dsl import Call, Feature, Node, Num

#: 分级常量（普通字符串——与 str-Enum 的 isinstance 陷阱保持距离）
VERDICT_CLONE = "clone"
VERDICT_LITERAL_VARIANT = "literal_variant"
VERDICT_NEAR = "near"
VERDICT_NOVEL = "novel"

#: near 档默认阈值（trigram 余弦）。T-FM-06 结构审计（110 内部 + 与 T-FM-01
#: 值级去重结果交叉验证）校准后定稿；调用方可显式覆盖。
DEFAULT_NEAR_THRESHOLD = 0.8

_NGRAM_SIZES = (1, 2, 3)


@dataclass(frozen=True)
class OpProfile:
    """算子路谱：对一条 FM-DSL 表达式的确定性结构摘要。"""

    #: 前序（先根后参）算子名序列（不含字段与数字）
    op_sequence: tuple[str, ...]
    #: 前序节点序列，字面量抽象为 ``#``、特征名保留——「仅窗口/常数不同」的判据
    shape_key: str
    #: n → 算子序列上的 n-gram 多重集（序列不足 n 时该档为空）
    ngrams: Mapping[int, Counter[tuple[str, ...]]]
    #: 根→叶最长链节点数（叶子树 = 1）
    depth: int
    node_count: int
    op_count: int
    distinct_ops: int


def profile(node: Node) -> OpProfile:
    """算子路谱（纯函数，同输入恒同输出）。

    ``ngrams`` 顶层为只读映射（``MappingProxyType``，写顶层键抛 ``TypeError``）；
    内层 ``Counter`` 不冻结，且路谱**不承诺 hashable**——当缓存键请自建稳定摘要。
    """
    op_sequence: list[str] = []
    shape_tokens: list[str] = []
    depth = _collect(node, 1, op_sequence, shape_tokens)
    ops = tuple(op_sequence)
    ngrams: dict[int, Counter[tuple[str, ...]]] = {}
    for n in _NGRAM_SIZES:
        counts: Counter[tuple[str, ...]] = Counter()
        for start in range(0, len(ops) - n + 1):
            counts[ops[start : start + n]] += 1
        ngrams[n] = counts
    return OpProfile(
        op_sequence=ops,
        shape_key=",".join(shape_tokens),
        ngrams=MappingProxyType(ngrams),
        depth=depth,
        node_count=len(shape_tokens),
        op_count=len(ops),
        distinct_ops=len(set(ops)),
    )


def _collect(node: Node, depth: int, ops: list[str], shape: list[str]) -> int:
    """单次前序遍历：回填 op 序列 / 形状记号，返回该子树最长链深度。"""
    if isinstance(node, Num):
        shape.append("#")
        return depth
    if isinstance(node, Feature):
        shape.append(node.name)
        return depth
    ops.append(node.op)
    shape.append(node.op)
    # default：零参 Call（解析器产不出，手工构造可能）不得让 max() 抛 ValueError
    return max(
        (_collect(arg, depth + 1, ops, shape) for arg in node.args), default=depth
    )


def ast_equal(a: Node, b: Node) -> bool:
    """AST 逐位相等（数值按浮点值比较——``1e-8`` 与 ``0.00000001`` 视为同值）。"""
    if isinstance(a, Num) and isinstance(b, Num):
        return float(a.value) == float(b.value)
    if isinstance(a, Feature) and isinstance(b, Feature):
        return a.name == b.name
    if isinstance(a, Call) and isinstance(b, Call):
        return (
            a.op == b.op
            and len(a.args) == len(b.args)
            and all(ast_equal(x, y) for x, y in zip(a.args, b.args, strict=True))
        )
    return False


@dataclass(frozen=True)
class StructuralSimilarity:
    """一对表达式的结构相似度全量记录。"""

    clone: bool
    shape_equal: bool
    trigram_cosine: float
    bigram_cosine: float
    unigram_cosine: float
    depth_delta: int
    node_count_delta: int


def similarity(a: Node, b: Node) -> StructuralSimilarity:
    """两表达式的结构相似度（节点级入口；内部各自算路谱，均为纯函数）。"""
    pa = profile(a)
    pb = profile(b)
    return StructuralSimilarity(
        clone=ast_equal(a, b),
        shape_equal=pa.shape_key == pb.shape_key,
        trigram_cosine=_ngram_cosine(pa.ngrams[3], pb.ngrams[3]),
        bigram_cosine=_ngram_cosine(pa.ngrams[2], pb.ngrams[2]),
        unigram_cosine=_ngram_cosine(pa.ngrams[1], pb.ngrams[1]),
        depth_delta=pa.depth - pb.depth,
        node_count_delta=pa.node_count - pb.node_count,
    )


def _ngram_cosine(a: Counter[tuple[str, ...]], b: Counter[tuple[str, ...]]) -> float:
    """n-gram 多重集余弦（任一为空 → 0.0：无证据不冒充相似）。"""
    if not a or not b:
        return 0.0
    dot = sum(count * b[gram] for gram, count in a.items())
    norm_a = math.sqrt(sum(count * count for count in a.values()))
    norm_b = math.sqrt(sum(count * count for count in b.values()))
    return dot / (norm_a * norm_b)


def classify(
    sim: StructuralSimilarity, *, near_threshold: float = DEFAULT_NEAR_THRESHOLD
) -> str:
    """分级：clone > literal_variant > near（余弦 ≥ θ，含等号）> novel。"""
    if not 0.0 <= near_threshold <= 1.0:
        raise ValueError(f"near 阈值非法：{near_threshold!r}（应在 [0, 1]）")
    if sim.clone:
        return VERDICT_CLONE
    if sim.shape_equal:
        return VERDICT_LITERAL_VARIANT
    if sim.trigram_cosine >= near_threshold:
        return VERDICT_NEAR
    return VERDICT_NOVEL


#: 判决强度排序（强在前）——screen 的命中排序与总判决取最强用
_VERDICT_RANK = {
    VERDICT_CLONE: 3,
    VERDICT_LITERAL_VARIANT: 2,
    VERDICT_NEAR: 1,
    VERDICT_NOVEL: 0,
}


@dataclass(frozen=True)
class ScreenHit:
    """一次对库命中的单条记录。"""

    key: str
    verdict: str
    similarity: StructuralSimilarity


@dataclass(frozen=True)
class StructuralScreen:
    """对一条候选表达式扫全库的判决汇总。

    ``verdict`` = 全库最强判决；``matches`` = 非 novel 命中，按
    （判决强度降序, trigram 余弦降序, key 升序）稳定排序；``best`` = 首条。
    """

    verdict: str
    matches: tuple[ScreenHit, ...]
    best: ScreenHit | None


def screen(
    node: Node,
    corpus: Mapping[str, Node],
    *,
    near_threshold: float = DEFAULT_NEAR_THRESHOLD,
) -> StructuralScreen:
    """候选表达式 vs 库语料（key → AST）的结构查重。

    语义 = 计算前预筛：``clone`` 可直接拒（值必然重复）；``literal_variant``
    与 ``near`` 只标注（变体是探索对象，值级闸兜底）。空语料 → novel。
    """
    hits: list[ScreenHit] = []
    for key, other in corpus.items():
        sim = similarity(node, other)
        verdict = classify(sim, near_threshold=near_threshold)
        if verdict != VERDICT_NOVEL:
            hits.append(ScreenHit(key=str(key), verdict=verdict, similarity=sim))
    hits.sort(
        key=lambda hit: (
            -_VERDICT_RANK[hit.verdict],
            -hit.similarity.trigram_cosine,
            hit.key,
        )
    )
    matches = tuple(hits)
    verdict = matches[0].verdict if matches else VERDICT_NOVEL
    return StructuralScreen(
        verdict=verdict, matches=matches, best=matches[0] if matches else None
    )
