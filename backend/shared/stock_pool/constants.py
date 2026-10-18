"""全局股票池模块 - 常量与枚举。

简化版设计（v2，文件优先）：
- **成员唯一事实源 = TXT 文件**（前缀式一行一个，如 `SH600036`），
  落在 `POOL_TXT_DIR`（默认 /data/stock_pool/<code>.txt）；
- PG 只存**元信息**单表 `qm_stock_pool`（列表 / 归属 / 文件路径）与
  引用表 `qm_stock_pool_binding`（被引用的池不可删）；
- 无草稿/发布版本模型：编辑保存 → 重写 TXT → 立即生效；
- 内置池（csi300 等）启动与每日从 QuantDB 指数权重刷新成同名 TXT；
- 所有功能统一经 `PoolResolver` 读取，不再各自维护白名单。
"""

from __future__ import annotations

# 成员 TXT 根目录（容器内）。与 QuantDB 数据卷同级，训练/回测可直接读。
POOL_TXT_DIR_ENV = "QM_STOCK_POOL_TXT_DIR"
POOL_TXT_DIR_DEFAULT = "/data/stock_pool"

# 物化出的 Qlib instruments 文件名前缀，避免与 Qlib 原生池（csi300 等）冲突
INSTRUMENT_FILE_PREFIX = "pool_"

# 成员数量软上限（防止把行情接口拖死；TXT 本身没有大小问题）
MEMBER_MAX = 20000

# ---------------------------------------------------------------------------
# 池类型
# ---------------------------------------------------------------------------
POOL_TYPE_SYSTEM_INDEX = "system_index"  # 指数成分池（成分来自 QuantDB，刷新成 TXT）
POOL_TYPE_STATIC = "static"  # 手工维护的固定成分
POOL_TYPE_IMPORTED = "imported"  # 文件导入

POOL_TYPES = frozenset(
    {POOL_TYPE_SYSTEM_INDEX, POOL_TYPE_STATIC, POOL_TYPE_IMPORTED}
)

# ---------------------------------------------------------------------------
# 作用域
# ---------------------------------------------------------------------------
SCOPE_GLOBAL = "global"  # 全平台，仅管理员可改
SCOPE_TENANT = "tenant"  # 租户级（预留）
SCOPE_USER = "user"  # 用户私有池

SCOPES = frozenset({SCOPE_GLOBAL, SCOPE_TENANT, SCOPE_USER})

# ---------------------------------------------------------------------------
# 状态（简化：只有可用 / 归档两态，编辑即生效，无 draft/published）
# ---------------------------------------------------------------------------
STATUS_ACTIVE = "active"
STATUS_ARCHIVED = "archived"

STATUSES = frozenset({STATUS_ACTIVE, STATUS_ARCHIVED})

# ---------------------------------------------------------------------------
# 绑定目标（哪个功能在用这个池）
# ---------------------------------------------------------------------------
TARGET_BACKTEST = "backtest"
TARGET_TRAINING = "training"
TARGET_INFERENCE = "inference"
TARGET_SIMULATION = "simulation"
TARGET_LIVE = "live"
TARGET_STRATEGY = "strategy"
TARGET_FACTOR = "factor"

TARGET_TYPES = frozenset(
    {
        TARGET_BACKTEST,
        TARGET_TRAINING,
        TARGET_INFERENCE,
        TARGET_SIMULATION,
        TARGET_LIVE,
        TARGET_STRATEGY,
        TARGET_FACTOR,
    }
)

# 绑定模式
BINDING_MODE_FILTER = "filter"  # 作为候选全集（交集过滤）
BINDING_MODE_WHITELIST = "whitelist"  # 白名单，与 filter 同义，语义区分
BINDING_MODE_BLACKLIST = "blacklist"  # 排除

BINDING_MODES = frozenset(
    {BINDING_MODE_FILTER, BINDING_MODE_WHITELIST, BINDING_MODE_BLACKLIST}
)

# ---------------------------------------------------------------------------
# 支持的市场（与 data_platform.normalize_market 口径对齐）
# ---------------------------------------------------------------------------
MARKET_CN = "CN"
MARKET_HK = "HK"
MARKET_US = "US"
MARKET_BC = "BC"
MARKET_FUTURES = "FUTURES"

MARKETS = frozenset({MARKET_CN, MARKET_HK, MARKET_US, MARKET_BC, MARKET_FUTURES})
