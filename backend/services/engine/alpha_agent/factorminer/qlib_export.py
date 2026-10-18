"""FM-DSL → qlib 表达式翻译（T-FM-01 落库副产，T-FM-02 前置）。

论文 110 因子中**不含截面算子**的子集可逐条翻译成 qlib 表达式；产物只作
**可携带元数据**（审计 / 移植 / admin 面展示），运行时求值一律走
``factorminer.runtime`` 的自有求值器（``fm:`` 前缀路由）。qlib 表达式引擎
逐标的求值、没有 CsRank 类截面算子（T-FM-01 报告算子谱：74/110 因子用到
CsRank），含 CsRank 的因子只能走 FM 求值器——这是翻译覆盖的天花板。

映射口径（与 ``evaluator.py`` 对齐；容器对拍
``backend/tests/test_factorminer_qlib_parity.py`` 逐值钉住可翻译因子，
改映射必须重跑对拍。**注意运行容器的 qlib 是被本平台改过的**——实测
``Greater`` 实为 ``np.maximum``、``Less`` 实为 ``np.minimum``（真比较在
``Gt``/``Lt``/``Ge``/``Le`` 名下），翻译按**该环境的真实语义**写，
对拍是唯一判据）：

- ``Neg`` → ``Mul(x, -1)``（容器实测 qlib 解析器接受负字面量）；
- ``Greater``/``Less`` → ``Gt``/``Lt``（0/1 比较，NaN→假）；``Eq`` 同名直通；
- ``TsRank`` → ``Rank``（两者都是窗内 pct 秩，已实测一致）、
  ``TsMax``/``TsMin`` → ``Max``/``Min``、``Delay`` → ``Ref``、
  ``SMA`` → ``Mean``（求值器 SMA ≡ 滚动均值）；
- ``IfElse(c, a, b)`` → ``If(Gt(c, 0), a, b)``——求值器取 ``c>0`` 支（NaN
  走 else），qlib ``If`` 是 ``np.where`` 真值判定（负数/NaN 都取左支），
  用 ``Gt(c,0)`` 归一到 0/1 才等价；
- ``Max2``/``Min2`` → ``Greater``/``Less``——该 qlib 的这两个名字就是
  ``np.maximum``/``np.minimum``，与求值器逐位同语义（含 NaN 传播）；
- ``And``/``Or`` **不可直通**：求值器是布尔语义（``(a>0)&(b>0)``），qlib 的
  同名算子是对**原值** ``np.bitwise_and/or``（float32 上直接抛 TypeError）。
  翻译成 0/1 指示运算：``And`` → ``Mul(Gt(a,0), Gt(b,0))``、
  ``Or`` → ``Gt(Add(Gt(a,0), Gt(b,0)), 0)``；
- ``WMA(x, n)`` → ``Mul(WMA(x, n), n)``——该 qlib 的 wma 对已归一权重又做
  ``nanmean``（多除一次窗长），满窗时恰好是求值器的 1/n；
- ``EMA`` → 抛 ``QlibExportGap``：该 qlib 是 ``ewm(span=n)``（adjust=True
  默认），求值器是 ``ewm(alpha=2/(n+1), adjust=False)`` 递归——初值暂态
  不可精确互译；
- ``SignedPower(x, p)`` → ``Sign(x)·Power(Abs(x), p)``；
- ``TsArgMax(x, n)`` → ``Sub(n, IdxMax(x, n))``——**实测 qlib IdxMax 是
  「窗首起 1 基偏移」**（argmax_oldest0 + 1），求值器是「距当前根数」
  （当根 = 0），故 n − IdxMax = 距当前根数；
- ``$amt`` → ``$amount``；``$vwap``/``$returns`` 是合成行情字段（qlib 缓存
  无此列）→ 展开「amount/volume×10000×$factor」/「close/Ref(close,1)−1」；
  vwap 固定 ×$factor 约定（取数期的覆盖率兜底是数据质检行为，翻译产物
  不随数据漂移）；
- ``CsRank`` → 抛 ``QlibExportGap``（截面算子，表达不了）。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from . import dsl

#: 合成字段展开（见模块 docstring）
_VWAP_EXPR = "Mul(Mul(Div($amount, $volume), 10000), $factor)"
_RETURNS_EXPR = "Sub(Div($close, Ref($close, 1)), 1)"

_FIELD_MAP: dict[str, str] = {
    "$open": "$open",
    "$high": "$high",
    "$low": "$low",
    "$close": "$close",
    "$volume": "$volume",
    "$amt": "$amount",
    "$vwap": _VWAP_EXPR,
    "$returns": _RETURNS_EXPR,
}

#: 同名直通算子（qlib 表达式引擎同名算子，语义等价由容器对拍钉住；
#: ``Greater``/``Less``（该 qlib 里是 max/min）、``And``/``Or``（布尔语义
#: 分叉）、``WMA``（差窗长倍数）、``EMA``（递归口径不同）不走直通——
#: 见 ``_render`` 专支）
_PASSTHROUGH = frozenset(
    {
        "Abs",
        "Log",
        "Sign",
        "Add",
        "Sub",
        "Mul",
        "Div",
        "Eq",
        "Delta",
        "Kurt",
        "Max",
        "Mean",
        "Med",
        "Min",
        "Resi",
        "Rsquare",
        "Skew",
        "Slope",
        "Std",
        "Corr",
        "Cov",
    }
)


class QlibExportGap(ValueError):
    """表达式含 qlib 表达式引擎表达不了的算子（当前仅 CsRank 截面类）。"""


@dataclass(frozen=True)
class QlibExport:
    """翻译结果：``expression`` 与 ``gap`` 恰有其一非空。"""

    expression: str | None
    gap: str | None

    @property
    def ok(self) -> bool:
        return self.expression is not None


def export(expression: str) -> QlibExport:
    """FM 表达式 → qlib 表达式（失败给原因，不抛——审计与落库两条路都要原因）。"""
    try:
        node = dsl.parse(expression)
    except ValueError as exc:
        return QlibExport(None, f"FM 解析失败：{exc}")
    try:
        return QlibExport(_render(node), None)
    except QlibExportGap as exc:
        return QlibExport(None, str(exc))


def _render(node: dsl.Node) -> str:
    if isinstance(node, dsl.Num):
        return _fmt_num(node.value)
    if isinstance(node, dsl.Feature):
        target = _FIELD_MAP.get(node.name)
        if target is None:  # 防御：dsl 扩字段而这里漏映射时给 gap 而非 KeyError
            raise QlibExportGap(f"字段 {node.name} 无 qlib 映射")
        return target
    if not isinstance(node, dsl.Call):
        raise QlibExportGap(f"未知节点 {node!r}")
    op = node.op
    args = [_render(arg) for arg in node.args]
    if op == "Neg":
        return f"Mul({args[0]}, -1)"
    if op == "CsRank":
        raise QlibExportGap("CsRank 是截面算子——qlib 表达式引擎逐标的求值，无法翻译")
    if op == "TsRank":
        return f"Rank({args[0]}, {args[1]})"
    if op == "TsMax":
        return f"Max({args[0]}, {args[1]})"
    if op == "TsMin":
        return f"Min({args[0]}, {args[1]})"
    if op == "Delay":
        return f"Ref({args[0]}, {args[1]})"
    if op == "SMA":
        return f"Mean({args[0]}, {args[1]})"
    if op == "Greater":
        # 该 qlib 的 Greater 是 np.maximum，真比较在 Gt（对拍钉住）
        return f"Gt({args[0]}, {args[1]})"
    if op == "Less":
        return f"Lt({args[0]}, {args[1]})"
    if op == "IfElse":
        # 求值器取 c>0 支（NaN 走 else）；qlib If 是 np.where 真值判定
        # （负数/NaN 取左支），Gt(c,0) 归一到 0/1 才等价。
        return f"If(Gt({args[0]}, 0), {args[1]}, {args[2]})"
    if op == "Max2":
        # 该 qlib 的 Greater 就是 np.maximum：与求值器逐位同语义（含 NaN）
        return f"Greater({args[0]}, {args[1]})"
    if op == "Min2":
        return f"Less({args[0]}, {args[1]})"
    if op == "And":
        # 求值器 (a>0)&(b>0)；qlib And 是对原值 bitwise_and（float 上 TypeError）。
        # Gt 两侧都返 1.0/0.0，乘积即布尔与（NaN>0 两边都为假，语义同）。
        return f"Mul(Gt({args[0]}, 0), Gt({args[1]}, 0))"
    if op == "Or":
        # 同理：0/1 指示相加 >0 即布尔或。
        return f"Gt(Add(Gt({args[0]}, 0), Gt({args[1]}, 0)), 0)"
    if op == "WMA":
        # 该 qlib wma 对归一权重再做 nanmean（多除一次窗长）→ 满窗 ×n 复原
        return f"Mul(WMA({args[0]}, {args[1]}), {args[1]})"
    if op == "EMA":
        raise QlibExportGap(
            "EMA 在该 qlib 为 ewm(span=n, adjust=True)，与求值器的 "
            "adjust=False 递归初值暂态不可精确互译"
        )
    if op == "SignedPower":
        return f"Mul(Sign({args[0]}), Power(Abs({args[0]}), {args[1]}))"
    if op == "TsArgMax":
        return f"Sub({args[1]}, IdxMax({args[0]}, {args[1]}))"
    if op in _PASSTHROUGH:
        return f"{op}({', '.join(args)})"
    raise QlibExportGap(f"算子 {op} 未建立 qlib 映射")


def _fmt_num(value: float) -> str:
    """字面量渲染：整数值不带小数点；科学计数法展开成定点（qlib 解析器对
    ``1e-06`` 这类记法未验，定点无歧义）。"""
    if float(value).is_integer() and abs(value) < 1e15:
        return str(int(value))
    text = repr(float(value))
    if "e" in text or "E" in text:
        text = format(Decimal(text), "f")
    return text


@dataclass(frozen=True)
class LibraryCoverage:
    """全库翻译审计：可翻译 (fid, qlib 表达式) 与不可翻译 (fid, 原因) 各一列。"""

    translatable: tuple[tuple[str, str], ...]
    gaps: tuple[tuple[str, str], ...]

    @property
    def n_translatable(self) -> int:
        return len(self.translatable)

    @property
    def n_gap(self) -> int:
        return len(self.gaps)


def library_coverage() -> LibraryCoverage:
    """110 条逐一试翻（importer dry-run / 测试 / T-FM-02 共用一份实现）。"""
    from .library import LIBRARY

    ok: list[tuple[str, str]] = []
    gaps: list[tuple[str, str]] = []
    for factor in LIBRARY:
        result = export(factor.expression)
        if result.ok:
            assert result.expression is not None
            ok.append((factor.fid, result.expression))
        else:
            gaps.append((factor.fid, result.gap or "未知原因"))
    return LibraryCoverage(tuple(ok), tuple(gaps))
