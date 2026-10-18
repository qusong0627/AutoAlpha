"""股票标签工具：tag_code 标准化与 DSL 别名解析。

标签从 parquet/stock_daily_latest 的宽表列迁移到 PG 长表 stock_tag 后，
选股 DSL 中的因子（如 idx_hs300、concept_ai、is_csi300）需解析为 tag_code，
再翻译成 EXISTS 谓词。本模块集中维护别名 → tag_code 映射。
"""

from __future__ import annotations

TAG_FACTOR_ALIASES: dict[str, str] = {
    "idx_hs300": "hs300",
    "hs300": "hs300",
    "is_hs300": "hs300",
    "is_csi300": "hs300",
    "csi300": "hs300",
    "idx_zz500": "csi500",
    "zz500": "csi500",
    "csi500": "csi500",
    "is_csi500": "csi500",
    "idx_zz1000": "csi1000",
    "zz1000": "csi1000",
    "csi1000": "csi1000",
    "is_csi1000": "csi1000",
    "idx_chinext": "chinext",
    "chinext": "chinext",
    "idx_margin": "margin",
    "margin": "margin",
    "idx_all": "all",
    "concept_ai": "ai",
    "concept_chip": "chip",
    "concept_new_energy": "new_energy",
    "concept_pv": "pv",
    "concept_military": "military",
    "concept_medical": "medical",
    "concept_fintech": "fintech",
    "concept_consumption": "consumption",
    "concept_state_owned": "state_owned",
    "concept_lithium": "lithium",
}

SPECIAL_TAG_CODES = {"all"}


def resolve_tag_code(factor: str) -> str | None:
    """将 DSL 因子名解析为 tag_code，非标签因子返回 None。"""
    if not factor:
        return None
    return TAG_FACTOR_ALIASES.get(factor.strip().lower())


def is_tag_factor(factor: str) -> bool:
    return factor is not None and factor.strip().lower() in TAG_FACTOR_ALIASES


def is_membership_true_op(op: str, value: float) -> bool:
    """判断 DSL 条件是否表示"属于该标签"（成员判定）。

    标签为二元成员关系（成员=1，非成员=0）。根据 DSL 运算符和阈值
    判断条件筛选的是成员还是非成员：
    - = / == : 值 != 0 → 成员
    - != / <> : 值 == 0 → 成员
    - > / >= : 阈值 < 1 → 成员（成员值 1 满足 > 阈值）
    - < / <= : 阈值 > 1 → 成员（非成员值 0 不满足 < 阈值）
    """
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    op = op.strip().lower()
    if op in {"=", "=="}:
        return v != 0
    if op in {"!=", "<>"}:
        return v == 0
    if op in {">", ">="}:
        return v < 1
    if op in {"<", "<="}:
        return v > 1
    return False


def build_membership_predicate(
    tag_code: str, *, symbol_col: str = "sdl.symbol", negate: bool = False
) -> str:
    """生成判断 symbol 是否属于某标签的 SQL 谓词片段（不含参数绑定）。

    返回的 SQL 用 :tag 参数占位，调用方需在执行时绑定 tag_code。
    """
    base = (
        f"SELECT 1 FROM stock_tag st "
        f"WHERE st.symbol = {symbol_col} AND st.tag_code = :tag_{tag_code}"
    )
    return f"NOT EXISTS ({base})" if negate else f"EXISTS ({base})"


def param_name(tag_code: str) -> str:
    return f"tag_{tag_code}"
