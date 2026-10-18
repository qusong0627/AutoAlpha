"""图表策略表达式引擎（P5）

安全求值白名单 DSL，用于：
- 自定义指标（图表叠加）
- 简单策略买卖条件（chart-backtest）
- 后续 AI 生成策略

支持函数：MA/EMA/SMA/CROSS/CROSSUP/CROSSDOWN/REF/HHV/LLV/ABS/MAX/MIN/AND/OR/NOT
操作数：CLOSE/OPEN/HIGH/LOW/VOLUME/AMOUNT
运算：+ - * / > < >= <= == != ( )

实现：词法分析 -> 递归下降解析 -> 对 pandas Series 逐元素求值。
"""

from __future__ import annotations

import re
from typing import Any

import pandas as pd

# ---------------------------------------------------------------------------
# 词法分析
# ---------------------------------------------------------------------------

_TOKEN_RE = re.compile(
    r"\s*(?P<NUM>\d+\.?\d*|\.\d+)"
    r"|\s*(?P<ID>[A-Za-z_][A-Za-z0-9_]*)"
    r"|\s*(?P<OP>==|!=|>=|<=|>|<|&&|\|\||[+\-*/(),])"
)


class Token:
    __slots__ = ("kind", "value")

    def __init__(self, kind: str, value: str):
        self.kind = kind
        self.value = value

    def __repr__(self) -> str:
        return f"Token({self.kind},{self.value})"


def tokenize(expr: str) -> list[Token]:
    tokens: list[Token] = []
    pos = 0
    while pos < len(expr):
        m = _TOKEN_RE.match(expr, pos)
        if not m:
            raise ValueError(f"无法解析字符 {expr[pos]!r} @ {pos}")
        kind = m.lastgroup
        if kind == "NUM":
            tokens.append(Token("num", m.group("NUM")))
        elif kind == "ID":
            tokens.append(Token("id", m.group("ID")))
        else:
            tokens.append(Token("op", m.group("OP")))
        pos = m.end()
    tokens.append(Token("eof", ""))
    return tokens


# ---------------------------------------------------------------------------
# AST 节点
# ---------------------------------------------------------------------------


class Node:
    def eval(self, ctx: dict[str, pd.Series]) -> pd.Series:
        raise NotImplementedError


class NumNode(Node):
    def __init__(self, v: float):
        self.v = v

    def eval(self, ctx):
        return float(self.v)


class IdNode(Node):
    def __init__(self, name: str):
        self.name = name

    def eval(self, ctx):
        key = self.name.upper()
        if key in ctx:
            return ctx[key]
        raise ValueError(f"未知变量/函数 {self.name}")


class BinNode(Node):
    def __init__(self, op: str, left: Node, right: Node):
        self.op = op
        self.left = left
        self.right = right

    def eval(self, ctx):
        a = self.left.eval(ctx)
        b = self.right.eval(ctx)
        if not isinstance(a, pd.Series) and not isinstance(b, pd.Series):
            # 纯标量
            if self.op == "+":
                return a + b
            if self.op == "-":
                return a - b
            if self.op == "*":
                return a * b
            if self.op == "/":
                return a / b if b else float("nan")
            if self.op == ">":
                return float(a > b)
            if self.op == "<":
                return float(a < b)
            if self.op == ">=":
                return float(a >= b)
            if self.op == "<=":
                return float(a <= b)
            if self.op == "==":
                return float(a == b)
            if self.op == "!=":
                return float(a != b)
            if self.op == "&&":
                return float(a > 0 and b > 0)
            if self.op == "||":
                return float(a > 0 or b > 0)
            raise ValueError(f"未知运算符 {self.op}")
        if self.op == "+":
            return a + b
        if self.op == "-":
            return a - b
        if self.op == "*":
            return a * b
        if self.op == "/":
            return a / b.replace(0, float("nan"))
        if self.op == ">":
            return (a > b).astype(float)
        if self.op == "<":
            return (a < b).astype(float)
        if self.op == ">=":
            return (a >= b).astype(float)
        if self.op == "<=":
            return (a <= b).astype(float)
        if self.op == "==":
            return (a == b).astype(float)
        if self.op == "!=":
            return (a != b).astype(float)
        if self.op == "&&":
            return ((a > 0) & (b > 0)).astype(float)
        if self.op == "||":
            return ((a > 0) | (b > 0)).astype(float)
        raise ValueError(f"未知运算符 {self.op}")


class NotNode(Node):
    def __init__(self, child: Node):
        self.child = child

    def eval(self, ctx):
        v = self.child.eval(ctx)
        return float(v <= 0) if not isinstance(v, pd.Series) else (v <= 0).astype(float)


# 白名单函数：返回 Series
_FUNCS: dict[str, Any] = {}


def _register(fn):
    _FUNCS[fn.__name__.upper()] = fn
    return fn


@_register
def MA(series, n):
    return series.rolling(int(float(n))).mean()


@_register
def EMA(series, n):
    return series.ewm(span=int(float(n)), adjust=False).mean()


@_register
def SMA(series, n, m=1):
    return series.ewm(alpha=float(m) / int(float(n)), adjust=False).mean()


@_register
def REF(series, n):
    return series.shift(int(float(n)))


@_register
def HHV(series, n):
    return series.rolling(int(float(n))).max()


@_register
def LLV(series, n):
    return series.rolling(int(float(n))).min()


@_register
def ABS(series):
    return series.abs()


@_register
def MAX(series, n):
    return series.rolling(int(float(n))).max()


@_register
def MIN(series, n):
    return series.rolling(int(float(n))).min()


@_register
def CROSS(a, b):
    """a 上穿或下穿 b（含任一方向变化）。"""
    prev = (a.shift(1) <= b.shift(1)) & (a > b)
    nxt = (a.shift(1) >= b.shift(1)) & (a < b)
    return (prev | nxt).astype(float)


@_register
def CROSSUP(a, b):
    return ((a.shift(1) <= b.shift(1)) & (a > b)).astype(float)


@_register
def CROSSDOWN(a, b):
    return ((a.shift(1) >= b.shift(1)) & (a < b)).astype(float)


@_register
def AND(a, b):
    return ((a > 0) & (b > 0)).astype(float)


@_register
def OR(a, b):
    return ((a > 0) | (b > 0)).astype(float)


@_register
def NOT(a):
    return (a <= 0).astype(float)


# 操作数（原始序列）
_OPERANDS = ("CLOSE", "OPEN", "HIGH", "LOW", "VOLUME", "AMOUNT", "VOL")

_PRIORITY = {"||": 1, "&&": 2, ">": 3, "<": 3, ">=": 3, "<=": 3, "==": 3, "!=": 3, "+": 4, "-": 4, "*": 5, "/": 5}


class Parser:
    def __init__(self, tokens: list[Token]):
        self.ts = tokens
        self.pos = 0

    def peek(self) -> Token:
        return self.ts[self.pos]

    def next(self) -> Token:
        t = self.ts[self.pos]
        self.pos += 1
        return t

    def expect_op(self, op: str):
        t = self.next()
        if t.kind != "op" or t.value != op:
            raise ValueError(f"期望 {op!r}，实际 {t.value!r}")

    def parse(self) -> Node:
        node = self.parse_or()
        if self.peek().kind != "eof":
            raise ValueError(f"多余内容 {self.peek().value!r}")
        return node

    def parse_or(self) -> Node:
        left = self.parse_and()
        while self.peek().kind == "op" and self.peek().value == "||":
            self.next()
            left = BinNode("||", left, self.parse_and())
        return left

    def parse_and(self) -> Node:
        left = self.parse_cmp()
        while self.peek().kind == "op" and self.peek().value == "&&":
            self.next()
            left = BinNode("&&", left, self.parse_cmp())
        return left

    def parse_cmp(self) -> Node:
        left = self.parse_add()
        while self.peek().kind == "op" and self.peek().value in (">", "<", ">=", "<=", "==", "!="):
            op = self.next().value
            left = BinNode(op, left, self.parse_add())
        return left

    def parse_add(self) -> Node:
        left = self.parse_mul()
        while self.peek().kind == "op" and self.peek().value in ("+", "-"):
            op = self.next().value
            left = BinNode(op, left, self.parse_mul())
        return left

    def parse_mul(self) -> Node:
        left = self.parse_unary()
        while self.peek().kind == "op" and self.peek().value in ("*", "/"):
            op = self.next().value
            left = BinNode(op, left, self.parse_unary())
        return left

    def parse_unary(self) -> Node:
        if self.peek().kind == "op" and self.peek().value == "-":
            self.next()
            return BinNode("-", NumNode(0), self.parse_unary())
        if self.peek().kind == "op" and self.peek().value == "(":
            self.next()
            node = self.parse_or()
            self.expect_op(")")
            return node
        t = self.next()
        if t.kind == "num":
            return NumNode(float(t.value))
        if t.kind == "id":
            name = t.value
            if name.upper() == "NOT":
                self.expect_op("(")
                inner = self.parse_or()
                self.expect_op(")")
                return NotNode(inner)
            if self.peek().kind == "op" and self.peek().value == "(":
                self.next()
                args: list[Node] = []
                if not (self.peek().kind == "op" and self.peek().value == ")"):
                    args.append(self.parse_or())
                    while self.peek().kind == "op" and self.peek().value == ",":
                        self.next()
                        args.append(self.parse_or())
                self.expect_op(")")
                return FuncNode(name.upper(), args)
            return IdNode(name)
        raise ValueError(f"意外 token {t.value!r}")


class FuncNode(Node):
    def __init__(self, name: str, args: list[Node]):
        self.name = name
        self.args = args

    def eval(self, ctx):
        fn = _FUNCS.get(self.name)
        if fn is None:
            raise ValueError(f"未注册函数 {self.name}")
        vals = [a.eval(ctx) for a in self.args]
        # 标量参数转 float，函数内部转 int；序列原样
        vals = [float(v) if not isinstance(v, pd.Series) else v for v in vals]
        try:
            return fn(*vals)
        except (TypeError, ValueError):
            raise ValueError(f"函数 {self.name} 参数错误")


def compile_expr(expr: str) -> Node:
    return Parser(tokenize(expr)).parse()


def build_context(ohlcv: pd.DataFrame) -> dict[str, pd.Series]:
    """构造求值上下文：操作数序列 + 索引。"""
    ctx: dict[str, pd.Series] = {
        "CLOSE": ohlcv["close"].astype(float),
        "OPEN": ohlcv["open"].astype(float),
        "HIGH": ohlcv["high"].astype(float),
        "LOW": ohlcv["low"].astype(float),
        "VOLUME": ohlcv["volume"].astype(float),
        "VOL": ohlcv["volume"].astype(float),
    }
    if "amount" in ohlcv.columns:
        ctx["AMOUNT"] = ohlcv["amount"].astype(float)
    ctx["__idx"] = ohlcv.index
    return ctx


def eval_bool_expr(node: Node, ctx: dict[str, pd.Series]) -> pd.Series:
    """布尔表达式 -> 0/1 Series（NaN 视为 0）。"""
    return (node.eval(ctx).fillna(0) > 0)
