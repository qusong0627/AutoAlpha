"""全局股票池模块（Global Stock Pool, v2 简化版）。

设计要点：
- **TXT 即事实源**：每个池的成员是一个前缀式一行一个的 TXT。
  全局池扁平存放（`/data/stock_pool/<code>.txt`），用户 / 租户池按
  隔离子目录存放（`/data/stock_pool/u<user_id>/<code>.txt`），人可读可手改，
  回测引擎和其他模块可直接读取；编辑保存 → 重写 TXT → 立即生效，
  没有草稿/发布版本模型。
- **PG 单表存元信息**：`qm_stock_pool`（列表/归属/市场/文件路径/计数），
  `qm_stock_pool_binding` 记录长生命周期引用（被引用的池不可删）。
- **唯一读取入口**：所有功能统一经 `PoolResolver`；内置池目录（`builtins.py`）
  收敛原先 `UNIVERSE_MAP` / `_ALLOWED_UNIVERSES` / alpha_agent 白名单三份。
- **内置池自动刷新**：启动 seed + 每日 worker 从 QuantDB 指数权重刷新成分
  TXT（指数成分调整自动跟进）；resolver 读不到文件时还会自愈重拉。
- **口径统一**：TXT/前端/API 一律前缀式 `SH600036`；进程内解析统一转
  后缀式 `600036.SH`（DB/Qlib/parquet 层），转换只经 `normalize.py`。

典型用法：

    from backend.shared.stock_pool import resolve_pool

    snap = await resolve_pool("pool:csi300", tenant_id=tid, user_id=uid)
    snap.api_symbols  # ['SH600036', ...] 前缀式
    snap.symbols      # ['600036.SH', ...] 后缀式
    snap.checksum     # 成员集合校验和（回测/训练结果落库用于复现）
"""

from __future__ import annotations

from .constants import (
    BINDING_MODES,
    INSTRUMENT_FILE_PREFIX,
    MARKETS,
    MEMBER_MAX,
    POOL_TYPES,
    SCOPES,
    SCOPE_GLOBAL,
    STATUS_ACTIVE,
    STATUS_ARCHIVED,
    STATUSES,
    TARGET_BACKTEST,
    TARGET_FACTOR,
    TARGET_INFERENCE,
    TARGET_LIVE,
    TARGET_SIMULATION,
    TARGET_STRATEGY,
    TARGET_TRAINING,
    TARGET_TYPES,
)
from .builtins import (
    BUILTIN_BY_CODE,
    BUILTIN_CODES,
    BUILTIN_POOLS,
    INDEX_NAMES,
    INDEX_SYMBOLS,
    BuiltinPool,
    cn_index_names,
    cn_index_symbols,
    get_builtin,
    is_builtin,
)
from .filters import (
    PoolFilterOutcome,
    filter_signals_by_pool,
    intersect_symbols,
)
from .materializer import (
    legacy_pool_txt_name,
    legacy_pool_txt_path,
    materialize_snapshot,
    pool_dir,
    pool_subdir,
    pool_txt_name,
    pool_txt_path,
    qlib_data_dir,
    read_instruments,
    read_instruments_file,
    read_pool_txt,
    resolve_pool_txt,
    write_pool_txt,
)
from .normalize import (
    checksum_symbols,
    is_valid_symbol,
    normalize_market,
    normalize_symbols,
    normalize_to_qlib,
    to_api_symbol,
    to_storage_symbol,
)
from .parser import (
    IndexEntry,
    ParseReport,
    ParseRow,
    StockIndex,
    decode_bytes,
    load_stock_index,
    match_token,
    normalize_name,
    parse_stock_list,
    parse_upload,
    resolve_index_path,
    symbol_to_name,
    validate_symbols,
)
from .resolver import (
    PoolResolver,
    ResolveContext,
    register_index_provider,
    resolve_pool,
    resolve_pool_sync,
    resolver,
)
from .schemas import (
    PoolBinding,
    PoolBindingRequest,
    PoolCreateFromMembersRequest,
    PoolImportResult,
    PoolMembersSave,
    PoolParseRequest,
    PoolSnapshot,
    StockPool,
    StockPoolCreate,
    StockPoolUpdate,
)
from .seed import (
    pool_storage_ready,
    refresh_builtin_txts,
    refresh_builtin_txts_sync,
    run_builtin_pool_refresh_worker,
    seed_builtin_pools,
    seed_builtin_pools_sync,
)

__all__ = [
    # constants
    "BINDING_MODES",
    "INSTRUMENT_FILE_PREFIX",
    "MARKETS",
    "MEMBER_MAX",
    "POOL_TYPES",
    "SCOPES",
    "SCOPE_GLOBAL",
    "STATUS_ACTIVE",
    "STATUS_ARCHIVED",
    "STATUSES",
    "TARGET_BACKTEST",
    "TARGET_FACTOR",
    "TARGET_INFERENCE",
    "TARGET_LIVE",
    "TARGET_SIMULATION",
    "TARGET_STRATEGY",
    "TARGET_TRAINING",
    "TARGET_TYPES",
    # builtins
    "BUILTIN_BY_CODE",
    "BUILTIN_CODES",
    "BUILTIN_POOLS",
    "INDEX_NAMES",
    "INDEX_SYMBOLS",
    "BuiltinPool",
    "cn_index_names",
    "cn_index_symbols",
    "get_builtin",
    "is_builtin",
    # filters
    "PoolFilterOutcome",
    "filter_signals_by_pool",
    "intersect_symbols",
    # materializer
    "legacy_pool_txt_name",
    "legacy_pool_txt_path",
    "materialize_snapshot",
    "pool_dir",
    "pool_subdir",
    "pool_txt_name",
    "pool_txt_path",
    "qlib_data_dir",
    "read_instruments",
    "read_instruments_file",
    "read_pool_txt",
    "resolve_pool_txt",
    "write_pool_txt",
    # normalize
    "checksum_symbols",
    "is_valid_symbol",
    "normalize_market",
    "normalize_symbols",
    "normalize_to_qlib",
    "to_api_symbol",
    "to_storage_symbol",
    # parser
    "IndexEntry",
    "ParseReport",
    "ParseRow",
    "StockIndex",
    "decode_bytes",
    "load_stock_index",
    "match_token",
    "normalize_name",
    "parse_stock_list",
    "parse_upload",
    "resolve_index_path",
    "symbol_to_name",
    "validate_symbols",
    # resolver
    "PoolResolver",
    "ResolveContext",
    "register_index_provider",
    "resolve_pool",
    "resolve_pool_sync",
    "resolver",
    # schemas
    "PoolBinding",
    "PoolBindingRequest",
    "PoolCreateFromMembersRequest",
    "PoolImportResult",
    "PoolMembersSave",
    "PoolParseRequest",
    "PoolSnapshot",
    "StockPool",
    "StockPoolCreate",
    "StockPoolUpdate",
    # seed
    "pool_storage_ready",
    "refresh_builtin_txts",
    "refresh_builtin_txts_sync",
    "run_builtin_pool_refresh_worker",
    "seed_builtin_pools",
    "seed_builtin_pools_sync",
]
