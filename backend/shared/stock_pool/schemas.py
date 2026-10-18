"""全局股票池 - Pydantic DTO（v2 简化版：单表元信息 + TXT 成员）。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

PoolType = Literal["system_index", "static", "imported"]
PoolScope = Literal["global", "tenant", "user"]
PoolStatus = Literal["active", "archived"]
BindingMode = Literal["filter", "whitelist", "blacklist"]


class StockPool(BaseModel):
    """池元信息（成员不在库里，在 file_path 指向的 TXT）。"""

    pool_id: str
    code: str
    name: str
    description: str | None = None
    market: str = "CN"
    pool_type: PoolType = "static"
    scope: PoolScope = "global"
    tenant_id: str | None = None
    owner_user_id: str | None = None
    status: PoolStatus = "active"
    file_path: str | None = None
    symbol_count: int = 0
    checksum: str | None = None
    source_kind: str | None = None
    source_ref: str | None = None
    is_system: bool = False
    created_by: str | None = None
    updated_by: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class PoolBinding(BaseModel):
    pool_id: str
    target_type: str
    target_id: str
    mode: BindingMode = "filter"
    priority: int = 100
    tenant_id: str | None = None
    user_id: str | None = None


class PoolBindingRequest(BaseModel):
    """登记一条池引用。

    只登记**长生命周期**引用（策略 / 模型 / 模拟盘账户 / 实盘配置 / 因子）。
    回测与推理是一次性运行，不登记 binding —— 可复现信息（池 checksum）
    随结果记录落库，否则绑定表会随运行次数无界增长。
    """

    target_type: Literal[
        "backtest",
        "training",
        "inference",
        "simulation",
        "live",
        "strategy",
        "factor",
    ]
    target_id: str = Field(..., min_length=1, max_length=200)
    mode: BindingMode = "filter"
    priority: int = Field(default=100, ge=0, le=10000)
    tenant_id: str | None = None
    user_id: str | None = None


class PoolSnapshot(BaseModel):
    """解析结果：所有功能消费的唯一形态。"""

    pool_id: str
    code: str
    market: str = "CN"
    # 成员集合稳定校验和（排序后 sha256 前 16 位）：回测/训练结果落库用于复现。
    checksum: str | None = None
    # 库内口径（后缀式 600036.SH）
    symbols: list[str] = Field(default_factory=list)
    # API 口径（前缀式 SH600036）
    api_symbols: list[str] = Field(default_factory=list)
    source: str = "pool"  # pool | builtin | inline | file | all
    unfiltered: bool = False  # True 表示「不过滤」（对应旧的 universe='all'）
    warnings: list[str] = Field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not self.symbols

    def to_qlib_instruments(self) -> list[str]:
        from .normalize import normalize_to_qlib

        return [normalize_to_qlib(s, self.market) for s in self.symbols]


# ---------------------------------------------------------------------------
# 写入
# ---------------------------------------------------------------------------
class StockPoolCreate(BaseModel):
    code: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")
    name: str = Field(..., min_length=1, max_length=200)
    description: str | None = None
    market: str = "CN"
    pool_type: PoolType = "static"
    scope: PoolScope = "global"
    tenant_id: str | None = None
    owner_user_id: str | None = None
    source_kind: str | None = None
    source_ref: str | None = None


class StockPoolUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    status: PoolStatus | None = None


class PoolMembersSave(BaseModel):
    """整体覆盖成员。保存即写 TXT 即生效，没有发布步骤。

    `symbols` 与 `text` 二选一（同时给时以 symbols 为准）；
    `text` 为原始粘贴内容（一行一个代码，兼容逗号/分号分隔与 # 注释）。
    """

    symbols: list[str] = Field(default_factory=list)
    text: str | None = None


class PoolImportResult(BaseModel):
    total: int = 0
    accepted: int = 0
    rejected: int = 0
    duplicates: int = 0
    rejected_samples: list[str] = Field(default_factory=list)
    symbol_count: int = 0


# ---------------------------------------------------------------------------
# 上传解析（CSV/TXT → 与 stocks_index.json 对比 → 生成股票池）
# ---------------------------------------------------------------------------
class PoolParseRequest(BaseModel):
    """上传解析请求。

    优先使用 `content_base64`（保留原始字节，可正确解码 GBK/GB18030 的
    Excel 导出文件）；纯手工粘贴内容时用 `content_text`。
    """

    content_base64: str | None = Field(
        default=None, description="文件原始字节的 base64（推荐，能正确处理 GBK）"
    )
    content_text: str | None = Field(default=None, description="直接粘贴的文本内容")
    filename: str | None = Field(default=None, description="原始文件名，用于推断格式")
    fmt: Literal["csv", "txt"] | None = Field(
        default=None, description="缺省按内容/后缀推断"
    )
    has_header: bool = Field(default=True, description="CSV 首行是否为表头")
    column: str | None = Field(
        default=None, description="强制指定代码列（列名或从 0 开始的列序号）"
    )
    row_limit: int = Field(default=500, ge=0, le=20000, description="返回明细行数上限")


class PoolCreateFromMembersRequest(BaseModel):
    """用解析确认后的成员列表建池（建完即可用，无发布步骤）。"""

    code: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_\-]+$")
    name: str = Field(..., min_length=1, max_length=200)
    description: str | None = None
    market: str = "CN"
    pool_type: PoolType = "imported"
    symbols: list[str] = Field(
        default_factory=list,
        description="确认保留的代码列表（前端回传，服务端重新校验）",
    )
