"""内置系统池目录（seed 数据）。

收敛原先散落的三份白名单，成为唯一来源：
- `data_platform/quantdb_hub.py` 的 `UNIVERSE_MAP` / `UNIVERSE_NAMES`
- `strategy_lab/sdk/context.py` 的 `_ALLOWED_UNIVERSES`
- `routers/alpha_agent.py` 的 `valid_universes`

P2 阶段 `quantdb_hub.UNIVERSE_MAP` 改为从本目录派生，消灭「SDK 能写、
回测解析静默查空」的不一致（如 `hs300_ext` / `hk_main` / `us_sp500`）。
"""

from __future__ import annotations

from dataclasses import dataclass

from .constants import (
    MARKET_CN,
    MARKET_HK,
    MARKET_US,
    POOL_TYPE_SYSTEM_INDEX,
    SCOPE_GLOBAL,
)


@dataclass(frozen=True)
class BuiltinPool:
    code: str
    name: str
    market: str = MARKET_CN
    index_symbol: str | None = None
    description: str = ""
    aliases: tuple[str, ...] = ()
    # 该池在 QuantDB 缺少成分数据时不报错，仅记录警告（用于 HK/US 等未接入场景）
    optional_source: bool = False

    @property
    def pool_id(self) -> str:
        return f"sys_{self.code}"


BUILTIN_POOLS: tuple[BuiltinPool, ...] = (
    BuiltinPool(
        code="csi300",
        name="沪深300",
        index_symbol="000300.SH",
        description="中证指数公司沪深300成分股（按 QuantDB 指数权重）",
    ),
    BuiltinPool(
        code="csi500",
        name="中证500",
        index_symbol="000905.SH",
        description="中证500成分股",
    ),
    BuiltinPool(
        code="csi1000",
        name="中证1000",
        index_symbol="000852.SH",
        description="中证1000成分股",
    ),
    BuiltinPool(
        code="sse50",
        name="上证50",
        index_symbol="000016.SH",
        description="上证50成分股",
    ),
    BuiltinPool(
        code="gem",
        name="创业板",
        index_symbol="399006.SZ",
        description="创业板指成分股",
    ),
    BuiltinPool(
        code="star",
        name="科创板",
        index_symbol="000688.SH",
        description="科创50成分股",
    ),
    BuiltinPool(
        code="csi800",
        name="中证800",
        index_symbol="000906.SH",
        description="中证800成分股",
    ),
    BuiltinPool(
        code="all_a",
        name="全部A股",
        index_symbol=None,
        description="全市场A股（成员数超阈值，走 parquet 快照存储）",
    ),
    BuiltinPool(
        code="hs300_ext",
        name="沪深300扩展池",
        index_symbol=None,
        description=(
            "沪深300 + 扩展标的。QuantDB 无对应指数权重，"
            "需以静态成员维护或由上游文件物化；缺成分时解析会给出明确警告。"
        ),
        optional_source=True,
    ),
    BuiltinPool(
        code="hk_main",
        name="港股主板",
        market=MARKET_HK,
        index_symbol="HSI.HK",
        description="港股主板（依赖 QuantHK 指数权重接入，未就绪时返回空并警告）",
        optional_source=True,
    ),
    BuiltinPool(
        code="us_sp500",
        name="美股标普500",
        market=MARKET_US,
        index_symbol="SPX.US",
        description="美股标普500（依赖 QuantUS 指数权重接入，未就绪时返回空并警告）",
        optional_source=True,
    ),
)

# code → BuiltinPool
BUILTIN_BY_CODE: dict[str, BuiltinPool] = {}
for _builtin in BUILTIN_POOLS:
    BUILTIN_BY_CODE[_builtin.code] = _builtin
    for _alias in _builtin.aliases:
        BUILTIN_BY_CODE[_alias] = _builtin

BUILTIN_CODES: frozenset[str] = frozenset(p.code for p in BUILTIN_POOLS)

# 向后兼容别名：P2 前 quantdb_hub.UNIVERSE_MAP 的等价视图
INDEX_SYMBOLS: dict[str, str | None] = {p.code: p.index_symbol for p in BUILTIN_POOLS}

# 向后兼容别名：UNIVERSE_NAMES 等价视图
INDEX_NAMES: dict[str, str] = {p.code: p.name for p in BUILTIN_POOLS}


def cn_index_symbols() -> dict[str, str | None]:
    """CN 有指数来源的池 → 指数代码（**等价于历史 `UNIVERSE_MAP`**）。

    排除 `optional_source=True` 的池（如 `hs300_ext` 没有指数权重来源），
    保证与 P2 之前的行为逐条一致。这是 `quantdb_hub.UNIVERSE_MAP` 的唯一来源。
    """
    return {
        p.code: p.index_symbol
        for p in BUILTIN_POOLS
        if p.market == MARKET_CN and not p.optional_source
    }


def cn_index_names() -> dict[str, str]:
    """`cn_index_symbols()` 对应的中文名（等价于历史 `UNIVERSE_NAMES`）。"""
    return {
        p.code: p.name
        for p in BUILTIN_POOLS
        if p.market == MARKET_CN and not p.optional_source
    }


DEFAULT_POOL_CODE = "csi300"


def is_builtin(code: str) -> bool:
    return (code or "").strip().lower() in BUILTIN_BY_CODE


def get_builtin(code: str) -> BuiltinPool | None:
    return BUILTIN_BY_CODE.get((code or "").strip().lower())


def seed_rows() -> list[dict]:
    """生成 qm_stock_pool 的 seed 行（成员不在库里，TXT 由 seed 刷新生成）。"""
    rows: list[dict] = []
    for pool in BUILTIN_POOLS:
        rows.append(
            {
                "pool_id": pool.pool_id,
                "code": pool.code,
                "name": pool.name,
                "description": pool.description,
                "market": pool.market,
                "pool_type": POOL_TYPE_SYSTEM_INDEX,
                "scope": SCOPE_GLOBAL,
                "tenant_id": None,
                "owner_user_id": None,
                "source_kind": "quantdb_index_weights",
                "source_ref": pool.index_symbol,
                "is_system": True,
                "created_by": "system",
                "updated_by": "system",
            }
        )
    return rows
