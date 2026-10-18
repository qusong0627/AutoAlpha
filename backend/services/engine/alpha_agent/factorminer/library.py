"""FactorMiner 论文内置因子库（论文附录 D，110 条，逐字嵌入）。

出处: https://arxiv.org/html/2602.14670v2 "Appendix D Full Factor Library"
抓取: 2026-10-11。原始抓取文本存 data/refs/factorminer/paper_appendixD_110_factors.txt
（data/ 是仓外符号链接不进 git）；本模块的 LIBRARY_TEXT 即其逐字副本，
由 /tmp 的一次性生成脚本从原文提取嵌入（禁手工转录——110 条手抄必出错）。

模块导入即校验：110 条全部通过 FM-DSL 白名单解析、编号 001-110 连续、
顶层为算子调用；任何一条非法则导入即抛 ValueError（拒绝带病出厂）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .dsl import Call, Complexity, complexity, min_history, parse

#: 110 行 `NNN <表达式>`，与论文附录 D 逐字一致（顺序即论文顺序）。
LIBRARY_TEXT = """\
001 Neg(CsRank(Div(Sub($close, Min($close, 48)), Add(Sub(Max($close, 48), Min($close, 48)), 1e-8))))
002 Neg(Div(Sub($close, EMA($close, 10)), EMA($close, 10)))
003 Sub(CsRank(Delta($volume, 1)), CsRank(Div(Sub($close, $vwap), $vwap)))
004 Mul(CsRank(Div($volume, Mean($volume, 24))), CsRank(Neg($returns)))
005 Neg(Mul(TsRank(Div(Sub($close, TsMin($close, 12)), Add(Sub(TsMax($close, 12), TsMin($close, 12)), 1e-6)), 12), CsRank(Abs(Cov($returns, $volume, 12)))))
006 Neg(Div(Sub($close, $vwap), $vwap))
007 Neg(Sub(CsRank(Div(Sub($close, $low), Add(Sub($high, $low), 0.0001))), CsRank(Delta(EMA($volume, 5), 5))))
008 Neg(Mul(CsRank(Div($returns, Add(Std($returns, 12), 0.0001))), CsRank(Div($volume, Mean($volume, 12)))))
009 Neg(Mul(CsRank($returns), CsRank(Std($returns, 12))))
010 Neg(CsRank(Sub(CsRank(Div($close, Delay($close, 5))), CsRank(Div($volume, Delay($volume, 5))))))
011 Neg(Sub(CsRank(Delta($close, 6)), CsRank(Delta($vwap, 6))))
012 Mul(CsRank(Neg(Div(Sub($close, $vwap), $vwap))), CsRank(Div($volume, EMA($volume, 12))))
013 Mul(CsRank(Neg(Div(Sub($close, $vwap), $vwap))), Sub(1, CsRank($volume)))
014 Mul(Add(Mul(CsRank(Neg(Div(Sub($close, $vwap), $vwap))), 0.6), Mul(CsRank(Delta(Neg(Div(Sub($close, $vwap), $vwap)), 3)), 0.4)), CsRank(Delta(Div(Mean(Div(Abs($returns), Add($volume, 1)), 24), Add(Div(Abs($returns), Add($volume, 1)), 1e-6)), 3)))
015 Mul(CsRank(Neg(Delta($returns, 3))), Mul(Sub(1, CsRank(Std($returns, 12))), CsRank(Std($returns, 12))))
016 Neg(Mul(CsRank(Delta(Div(Sub($close, $vwap), $vwap), 3)), CsRank(Div($volume, EMA($volume, 12)))))
017 Neg(Mul(TsRank(Delta(Div(Sub($close, $vwap), $vwap), 3), 12), TsRank(Std($returns, 12), 12)))
018 Neg(Mul(TsRank(Div(Sub($close, TsMin($close, 12)), Add(Sub(TsMax($close, 12), TsMin($close, 12)), 1e-6)), 12), TsRank(Div($volume, EMA($volume, 12)), 12)))
019 IfElse(Greater(Abs(Div(Sub($close, $vwap), $vwap)), 0.01), Neg(CsRank(Sub($close, TsMax($close, 12)))), Neg(CsRank(Sub($close, TsMin($close, 12)))))
020 TsRank(Sub(CsRank(Delta($volume, 3)), CsRank(Delta($close, 3))), 18)
021 Neg(Mul(TsRank(Delta(Div($volume, EMA($volume, 12)), 3), 12), TsRank(Delta(Div(Sub($close, $vwap), $vwap), 3), 12)))
022 Neg(CsRank(Div(Sub(Min2($open, $close), $low), Add(Sub($high, $low), 0.0001))))
023 Neg(TsRank(Div(Delta($close, 3), Mean(Abs(Delta($close, 3)), 12)), 12))
024 Sub(TsRank(Delta($open, 6), 12), TsRank(Delta($close, 6), 12))
025 Sub(TsRank(Delta($open, 9), 18), TsRank(Delta($close, 9), 18))
026 Neg(Mul(TsRank(Cov(TsRank($close, 18), TsRank($volume, 18), 10), 18), TsRank(Div(Sub($close, $low), Sub($high, $low)), 18)))
027 Neg(Mul(TsRank(Cov(TsRank($open, 12), TsRank($volume, 12), 5), 12), TsRank(Div(Sub($close, $low), Sub($high, $low)), 12)))
028 Neg(Mul(Sub($close, $low), Div($volume, Mean($volume, 12))))
029 Mul(Sub($high, $close), Div($volume, Mean($volume, 12)))
030 Mul(Delta(Sub($high, $close), 3), Div($volume, Mean($volume, 12)))
031 IfElse(Greater(Delta($close, 1), 0), Neg(TsRank(Delta($close, 3), 12)), TsRank(Delta($close, 3), 12))
032 Neg(TsRank(Sub(CsRank(Delta($returns, 2)), CsRank(Delta($volume, 2))), 12))
033 Neg(Mul(TsRank(Corr(TsRank(Div($volume, Mean($volume, 24)), 12), TsRank(Sub($high, $low), 12), 12), 24), CsRank(Delta($close, 2))))
034 IfElse(Greater(Delta($close, 2), 0), Neg(TsRank(Delta($close, 5), 15)), TsRank(Delta($close, 5), 15))
035 Neg(Mul(TsRank(Delta(Sub($high, $low), 2), 12), CsRank(Delta($close, 2))))
036 IfElse(Greater(Std($returns, 12), Mean(Std($returns, 12), 48)), Neg(CsRank(Div(Sub(Min2($open, $close), $low), Add(Sub($high, $low), 0.0001)))), Neg(CsRank($returns)))
037 Neg(TsRank(Mul(CsRank(Delta($close, 3)), CsRank(Delta($volume, 3))), 18))
038 IfElse(Greater(Std($returns, 12), Mean(Std($returns, 12), 48)), Neg(TsRank(Sub(CsRank(Delta($volume, 3)), CsRank(Delta($close, 3))), 18)), Neg(TsRank(Div(Sub($close, $vwap), Add(Std($returns, 24), 0.0001)), 12)))
039 Neg(Mul(CsRank(Delta($close, 5)), CsRank(Kurt($returns, 24))))
040 Neg(Mul(CsRank(Div(Sub($close, $low), Add(Sub($high, $low), 0.0001))), CsRank(Skew($returns, 24))))
041 Mul(CsRank(Div(Sub($high, $close), Add(Sub($high, $low), 1e-8))), CsRank(Neg(Skew($returns, 24))))
042 IfElse(Greater(Abs(Skew($returns, 24)), 1.0), Neg(CsRank($returns)), Neg(CsRank(Skew($returns, 24))))
043 Neg(TsRank(Mul(CsRank(Skew($volume, 24)), CsRank(Delta($close, 3))), 12))
044 Neg(Mul(CsRank(Kurt($volume, 24)), CsRank(Div(Sub($close, $low), Add(Sub($high, $low), 0.0001)))))
045 IfElse(Greater(Kurt($returns, 24), 3.0), Neg(CsRank(Div(Sub($high, $close), Add(Sub($high, $low), 0.0001)))), Neg(CsRank(Div(Sub($close, $low), Add(Sub($high, $low), 0.0001)))))
046 IfElse(Greater(Std($returns, 12), Mean(Std($returns, 12), 48)), Neg(CsRank(Delta($close, 3))), Neg(CsRank(Div(Sub($close, $low), Add(Sub($high, $low), 0.0001)))))
047 Neg(Mul(CsRank(TsRank(Delta(Log(Add($volume, 1)), 3), 6)), CsRank(Div(Delta($close, 6), $close))))
048 TsRank(Sub(CsRank(Std($returns, 12)), CsRank(Delta($close, 6))), 18)
049 IfElse(Greater(Skew($returns, 24), 0.5), CsRank(Delta($volume, 3)), Neg(CsRank(Std($returns, 12))))
050 IfElse(Greater(Skew($returns, 24), 0.5), Neg(CsRank(Div(Sub($close, $low), Add(Sub($high, $low), 1e-6)))), Neg(CsRank(Delta($close, 6))))
051 IfElse(Less(Skew($returns, 24), -0.5), CsRank(Delta($close, 5)), Neg(CsRank(Std($returns, 12))))
052 IfElse(Less(Skew($returns, 24), -0.5), Neg(TsRank($returns, 12)), Neg(CsRank(Delta($close, 6))))
053 Neg(CsRank(TsArgMax(SignedPower(IfElse(Less($returns, 0), Std($returns, 20), $close), 2), 5)))
054 IfElse(Greater($amt, Mean($amt, 24)), CsRank(Delta($close, 3)), Neg(CsRank(Div(Sub($close, $vwap), $vwap))))
055 IfElse(Greater(Corr($close, $volume, 12), 0.5), Neg(CsRank($returns)), CsRank(Delta($volume, 3)))
056 IfElse(Greater(Kurt($returns, 24), 3.0), Neg(CsRank(Div($amt, Add($volume, 1e-6)))), Neg(CsRank(Delta($close, 3))))
057 Neg(Sub(TsRank(Delta($close, 6), 24), TsRank(Delta($volume, 6), 24)))
058 IfElse(Greater($amt, Mean($amt, 48)), Neg(CsRank(Div($amt, Add($volume, 1e-6)))), Neg(TsRank($returns, 12)))
059 IfElse(Greater(Std($returns, 12), Mean(Std($returns, 12), 24)), Neg(CsRank(Skew($amt, 12))), Neg(CsRank(Delta($close, 3))))
060 IfElse(Greater(Div($amt, Mean($amt, 24)), 1.2), Sub(CsRank($close), CsRank($volume)), Neg(CsRank(Delta($close, 3))))
061 Neg(Mul(TsRank(Delta($volume, 1), 12), TsRank(Delta($close, 1), 12)))
062 Neg(Div(CsRank($returns), Add(Std($amt, 12), 1e-6)))
063 Neg(TsRank(Add(Add(CsRank(Delta($returns, 3)), CsRank(Delta($amt, 3))), CsRank(Kurt($volume, 12))), 12))
064 IfElse(Less(Corr($close, $volume, 12), -0.5), CsRank($returns), Neg(CsRank(Delta($close, 3))))
065 IfElse(Greater(Std($returns, 12), Mean(Std($returns, 12), 48)), Neg(CsRank(Delta($amt, 6))), Neg(CsRank(Delta($close, 1))))
066 IfElse(Greater(Kurt($volume, 24), 3), Neg(CsRank(Delta($amt, 3))), Neg(CsRank($returns)))
067 Neg(Mul(CsRank(Div(Sub($low, $close), Add(Sub($low, $high), 1e-6))), CsRank(Delta($amt, 6))))
068 Neg(IfElse(Less(Skew($returns, 24), -0.5), Neg(Sub(TsRank($close, 12), TsRank($volume, 12))), CsRank(Delta($returns, 3))))
069 Neg(IfElse(Greater(Kurt($returns, 12), 3.0), Neg(CsRank(Std($returns, 6))), CsRank(Delta($returns, 3))))
070 Neg(Mul(CsRank(Div(Sub($close, $low), Add(Sub($high, $low), 0.0001))), TsRank(Std($returns, 24), 24)))
071 Mul(CsRank(Div(Sub($high, $close), Add(Sub($high, $low), 1e-6))), TsRank(Std($volume, 12), 12))
072 IfElse(Greater(Kurt($volume, 24), 3.0), Neg(Corr($close, $volume, 12)), Neg(TsRank($returns, 24)))
073 IfElse(Greater($amt, EMA($amt, 12)), Neg(TsRank(Delta($volume, 3), 12)), Neg(CsRank($returns)))
074 Neg(Mul(CsRank(Div(Sub($close, $low), Add(Sub($high, $low), 1e-6))), TsRank(Skew($volume, 24), 12)))
075 Neg(Mul(CsRank(Div($returns, $amt)), TsRank(Std($volume, 20), 20)))
076 Neg(IfElse(Greater(Div($amt, Mean($amt, 24)), 2.0), Neg(Delta($close, 1)), SMA($returns, 6)))
077 Neg(Mul(TsRank(Std(Div($volume, Mean($volume, 24)), 24), 24), TsRank($returns, 12)))
078 Neg(Mul(CsRank(Div($returns, $amt)), TsRank(Kurt($volume, 20), 20)))
079 IfElse(Greater(Std($returns, 12), EMA(Std($returns, 12), 48)), Neg(Div(Sub($close, $low), Add(Sub($high, $low), 1e-6))), Div(Sub($high, $close), Add(Sub($high, $low), 1e-6)))
080 IfElse(Greater(Rsquare($close, 24), 0.7), Neg(CsRank(Slope($close, 24))), Neg(CsRank(Resi($close, 12))))
081 IfElse(And(Greater(Rsquare($close, 24), 0.6), Greater(Std($returns, 12), Mean(Std($returns, 12), 48))), Slope($close, 12), Neg(SMA($returns, 6)))
082 IfElse(Or(Greater(Div(Sub($high, Max2($open, $close)), Add(Sub($high, $low), 1e-6)), 0.6), Greater(Div(Sub(Min2($open, $close), $low), Add(Sub($high, $low), 1e-6)), 0.6)), Slope($close, 12), Neg(SMA($returns, 6)))
083 IfElse(And(Greater(Rsquare($close, 24), 0.5), Less(Std($returns, 12), Mean(Std($returns, 12), 48))), Slope($close, 12), Neg($returns))
084 IfElse(Eq(Sign(Delta(Std($returns, 12), 1)), Sign(Delta($returns, 1))), Neg(Resi($close, 12)), Neg(Slope($close, 24)))
085 Neg(Mul(CsRank(Rsquare($close, 24)), CsRank(Resi($close, 12))))
086 IfElse(Eq(Sign(Delta(Resi($close, 12), 1)), Sign(Resi($close, 12))), Neg(Resi($close, 6)), Neg(Slope($close, 12)))
087 IfElse(Greater(Kurt($returns, 24), 3.0), Neg(Resi($close, 6)), Neg(Resi($close, 24)))
088 Neg(Mul(CsRank(Div($returns, Add($amt, 1e-6))), TsRank(Skew($returns, 24), 24)))
089 IfElse(Or(Greater($volume, Mean($volume, 24)), Greater(Abs($returns), Std($returns, 24))), Neg(Resi($close, 6)), Neg(Slope($close, 12)))
090 IfElse(Greater(Rsquare($close, 24), 0.75), Neg(Slope($close, 12)), Neg(TsRank($returns, 12)))
091 Neg(Mul(TsRank(Div($returns, Add($amt, 1e-6)), 24), TsRank(Kurt($returns, 24), 24)))
092 Neg(CsRank(EMA(Div($returns, Add($amt, 1e-6)), 6)))
093 Neg(TsRank(Delta(Div($returns, Add($amt, 1e-6)), 3), 24))
094 IfElse(And(Greater(Std($returns, 12), Mean(Std($returns, 12), 60)), Less(Kurt($returns, 24), 2)), Neg(Delta($close, 3)), Neg(TsRank($returns, 24)))
095 IfElse(Or(Greater(Abs(Skew($returns, 24)), 1.5), Greater(Kurt($returns, 24), 4.0)), Neg(Resi($close, 6)), Neg(TsRank($returns, 24)))
096 Neg(Mul(TsRank(WMA(Div($returns, Add($amt, 1e-6)), 6), 24), TsRank(Corr($close, $volume, 24), 24)))
097 Neg(CsRank(Med(Div($returns, Add($amt, 1e-6)), 6)))
098 Neg(CsRank(Delta(WMA(Div($returns, Add($amt, 1e-6)), 6), 3)))
099 Neg(Sub(EMA(Div($returns, Add($amt, 1e-6)), 6), EMA(Div($returns, Add($amt, 1e-6)), 24)))
100 Neg(Resi(Delta(Resi($close, 24), 3), 12))
101 Neg(IfElse(Greater(Abs(Skew($returns, 24)), 1.0), TsRank(Div(Sub($returns, Med($returns, 24)), Add(Std($returns, 24), 1e-6)), 24), CsRank(Resi($close, 12))))
102 IfElse(Greater(Skew($high, 24), 0), TsRank(Log(Div($high, Add($close, 1e-6))), 24), CsRank(Resi($low, 24)))
103 Neg(IfElse(Greater(Skew($open, 24), 0), TsRank(Resi($open, 24), 24), CsRank(SignedPower($returns, 0.5))))
104 Neg(IfElse(Greater(Med($returns, 24), 0), TsRank(Log(Div($close, Add($open, 1e-6))), 24), CsRank(Sign(Resi($close, 24)))))
105 Neg(IfElse(Greater(Skew($returns,24),0.4), CsRank(Sign(Resi($open,24))), TsRank(SignedPower($returns,0.6),24)))
106 Neg(IfElse(Greater(Std($volume,24),Mean(Std($volume,24),48)), CsRank(Div($amt, Add($volume,1e-6))), TsRank(Div($returns, Add(Std($returns,24),1e-6)),24)))
107 IfElse(Greater(Std($returns, 12), Med(Std($returns, 12), 48)), Mul(CsRank(Neg(Div(Sub($close, $vwap), $vwap))), CsRank(Div($volume, EMA($volume, 12)))), Neg(TsRank(Div(Sub($close, $vwap), $vwap), 24)))
108 Neg(IfElse(Less(Skew($returns, 24), -0.3), Mul(CsRank(Neg(Div(Sub($close, $vwap), $vwap))), CsRank(Div($volume, EMA($volume, 12)))), TsRank(Div(Sub($close, $vwap), $vwap), 12)))
109 IfElse(Greater(Rsquare($close, 24), 0.6), Neg(CsRank(Slope($close, 24))), Mul(CsRank(Neg(Div(Sub($close, $vwap), $vwap))), CsRank(Div($volume, EMA($volume, 12)))))
110 Neg(Mul(CsRank(Div(Sub($close, $low), Add(Sub($high, $low), 1e-6))), CsRank(Mul(Skew($returns, 24), Slope($close, 12)))))
"""

_LINE_RE = re.compile(r"^(\d{3})\s+(\S.*?)\s*$")


@dataclass(frozen=True)
class PaperFactor:
    fid: str
    expression: str
    ast: Call
    min_history: int
    complexity: Complexity


def load_library(text: str = LIBRARY_TEXT) -> tuple[PaperFactor, ...]:
    """解析并校验因子库文本；编号必须从 001 连续，表达式必须过白名单解析。"""
    factors: list[PaperFactor] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line:
            continue
        match = _LINE_RE.match(line)
        if not match:
            raise ValueError(f"第 {lineno} 行不符合 `NNN 表达式` 格式：{line!r}")
        fid, expression = match.group(1), match.group(2)
        expected = f"{len(factors) + 1:03d}"
        if fid != expected:
            raise ValueError(f"编号不连续：第 {lineno} 行是 {fid}，应为 {expected}")
        try:
            ast = parse(expression)
        except ValueError as exc:
            raise ValueError(f"因子 {fid} 表达式非法：{exc}") from exc
        if not isinstance(ast, Call):
            raise ValueError(f"因子 {fid} 顶层必须是算子调用")
        factors.append(
            PaperFactor(fid, expression, ast, min_history(ast), complexity(ast))
        )
    return tuple(factors)


#: 导入即校验（非法库会让 import 直接失败，不留半成品）。
LIBRARY: tuple[PaperFactor, ...] = load_library()

_BY_ID: dict[str, PaperFactor] = {factor.fid: factor for factor in LIBRARY}


def by_id(fid: str) -> PaperFactor:
    """按论文编号取因子（如 ``by_id("094")``）。"""
    return _BY_ID[fid]
