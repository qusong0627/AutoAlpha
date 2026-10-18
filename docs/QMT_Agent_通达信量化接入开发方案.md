# QMT Agent 通达信量化（TDX）后端接入开发方案

> 状态：方案评审中（未实施）
> 日期：2026-07-28
> 涉及模块：`tools/qmt_agent`
> 关联文档：`DOCS/QMT_Agent_内置策略桥接迁移方案.md`（方案 A，QMT 内置策略路线）
> 本机验证环境：`C:\new_tdx64`（通达信金融终端 64 位 + 量化插件），tqcenter v1.0.14（2026-07-03）

## 1. 背景与定位

- MiniQMT 外接通道受《程序化交易管理实施细则》（2025-07-07 施行）影响，券商收紧开通。
- 通达信量化平台为通达信官方量化方案，**接入券商面更广**，作为新的主力柜台通道。
- 交付形态维持**桌面客户端 exe**（托盘常驻 + 自动更新 + GUI 诊断），内部代码按 SDK 分层重构，执行层抽象为多后端。

### 与 QMT 内置策略方案的关系

| | 通达信 TDX 后端（本方案） | QMT 内置策略（方案 A） |
|---|---|---|
| 接入形态 | **进程内直调**（import tqcenter） | loopback 桥接薄策略 |
| 改造复杂度 | 低（与现有 xtquant client.py 同构） | 中（新增本地协议 + 策略文件） |
| 通道性质 | 外接 Python（依赖券商验收） | 券商托管内置（合规确定性高） |
| 优先级 | **主路线（先实施）** | 备选路线（保留设计，按需实施） |

## 2. 通达信量化技术事实（已在本机确认）

来源：`C:\new_tdx64\PYPlugins\user\tqcenter.py`（4016 行，纯 Python + ctypes）。

### 2.1 运行模型

- `tqcenter.py` 通过 ctypes 加载 `PYPlugins\TPythClient.dll`（64 位），与**运行中且已登录**的通达信客户端进程通信（JSON-RPC 风格，`GetOrderStr`/`GetTdxDataStr` 等 DLL 入口）。
- 可被**任意外部 Python 进程**导入：`sys.path.insert(0, r"{tdx_path}\PYPlugins\user")` 后 `from tqcenter import tq`。DLL 路径由 tqcenter 相对自身定位（`parents[1]/TPythClient.dll`），因此必须从通达信安装目录原位导入，**不随 exe 打包 tqcenter 本体**（保证与客户端版本匹配）。
- 模块顶部 import `numpy/pandas` → agent 打包需捆绑二者。
- 语法为现代 Python（typing/f-string），与现有开发环境（≥3.10）完全兼容，无 QMT 内置 3.6 限制。
- 连接生命周期：`tq.initialize()`（惰性自动）/ `tq.close()`；RPC 返回 `ErrorId ∈ {"6","7"}` 表示连接失效，tqcenter 内部自动 `_reInitialize()`。

### 2.2 交易 API（`tqcenter.py:3573-3813`）

| 函数 | 说明 | 备注 |
|---|---|---|
| `tq.stock_account(account, account_type='stock')` | 换取账户句柄（int） | 失败返回 -1 |
| `tq.query_stock_asset(account_id)` | 资金查询 | 返回 dict |
| `tq.query_stock_orders(account_id, stock_code, cancelable_only)` | 委托查询 | 支持只查可撤 |
| `tq.query_stock_positions(account_id)` | 持仓查询 | |
| `tq.order_stock(account_id, stock_code, order_type, order_volume, price_type, price, notify)` | 下单 | 同步返回含委托号的 dict |
| `tq.cancel_order_stock(account_id, stock_code, order_id)` | 撤单 | |

**关键差异（相对 xtquant）**：
1. **无委托/成交推送回调** —— 订单状态必须轮询 `query_stock_orders`（现有 `OrderLifecycleMonitor` 正好复用）；
2. **无 `userOrderId` 透传** —— `client_order_id ↔ tdx order_id` 映射完全依赖下单同步返回值，需强化"下单成功但返回解析失败"场景的补偿对账；
3. **代码格式为后缀型** `600000.SH`（含 BJ/HK/US/期货期权等后缀），与全系统 Prefix 标准（`SH600000`）相反，必须在后端适配层双向转换。

### 2.3 行情 API（保护限价所需）

- `tq.get_market_snapshot(...)`：Level1 快照（盘口买卖档）→ 用于 `protect_limit` 保护限价计算；
- `tq.subscribe_quote / subscribe_hq`：行情推送回调（`Register_DataTransferFunc`），可选用于降低快照轮询频率。

## 3. 目标架构

```
┌────────── 云端 QuantMind（不变） ──────────┐
│  /internal/strategy/bridge/*  +  /ws/bridge │
└──────────────┬─────────────────────────────┘
               │ HTTPS + WSS（auth/reporter/WS 全部复用）
┌──────────────▼─────────────────────────────┐
│  qmt_agent.exe                              │
│  ┌───────────────────────────────────────┐  │
│  │ bridge_core（SDK 化核心包，无 GUI）    │  │
│  │  auth / reporter / runtime_workers     │  │
│  │  supervisor / 派单队列（50ms 节奏）    │  │
│  │  backends/                             │  │
│  │   ├─ base.py       ExecutionBackend    │  │
│  │   ├─ xtquant_backend.py（现状迁入）    │  │
│  │   ├─ tdx_backend.py（★ 本方案新增）    │  │
│  │   └─ mock_backend.py                   │  │
│  └───────────────────────────────────────┘  │
│  desktop_app.py（PyQt 壳） / qmt_agent.py    │
└──────────────┬─────────────────────────────┘
               │ ctypes → TPythClient.dll（进程内）
        通达信金融终端 64 位（已登录） → 券商柜台
```

### 3.1 ExecutionBackend 接口（P1 抽象）

从现有 `client.py` 提炼，全部后端实现同一契约：

```python
class ExecutionBackend:
    def connect(self) -> BackendStatus: ...
    def disconnect(self) -> None: ...
    def is_alive(self) -> bool: ...                 # 看门狗探活
    def place_order(self, req: OrderRequest) -> OrderAck: ...
    def cancel_order(self, req: CancelRequest) -> CancelAck: ...
    def query_orders(self, since: ...) -> list[OrderState]: ...
    def query_trades(self, since: ...) -> list[TradeRecord]: ...
    def query_asset(self) -> AccountAsset: ...
    def query_positions(self) -> list[Position]: ...
    def get_level1(self, symbol: str) -> Level1Quote | None: ...  # 保护限价用
    capabilities: BackendCapabilities  # has_push_callback / has_user_order_id / ...
```

- 统一使用 **Prefix 格式**（`SH600000`）作为接口层标准；后缀转换封装在 `tdx_backend.py` 内部（`SH600000 ↔ 600000.SH`），转换函数集中一处，禁止散落切片。
- `capabilities` 声明差异：TDX 后端 `has_push_callback=False` → `OrderLifecycleMonitor` 自动切换为轮询驱动模式。

### 3.2 TdxBackend 设计要点

| 主题 | 设计 |
|---|---|
| 动态导入 | 配置 `tdx_path`（如 `C:\new_tdx64`），启动时 `sys.path` 注入 `PYPlugins\user` 并 import；失败退化 mock（沿用 xtquant 模式） |
| 连接管理 | `connect()` = `tq.stock_account(account)` 换句柄；句柄失效（返回 -1 / ErrorId 6/7）触发重连流程，复用 ReconnectWorker |
| 探活 | 周期 `query_stock_asset` 轻量调用；连续失败 N 次判定柜台离线，心跳携带 degraded |
| 下单 | 派单队列出队 → `order_stock` 同步调用 → 解析返回委托号 → 写映射表 → 上报 SUBMITTED |
| 线程安全 | `GetOrderStr` 的 DLL 并发安全性未知 → **所有 RPC 调用收敛到派单线程串行执行**（查询走同一线程的时间片），P0 验证后再决定是否放开并发 |
| 订单状态轮询 | `OrderLifecycleMonitor` 以 500ms~1s 轮询 `query_stock_orders`（交易时段），diff 出状态变化 → 映射为统一状态 → `/bridge/execution` 上报；成交明细从委托的成交量字段推导，P0 确认是否有独立成交查询 |
| 状态映射 | 新建 TDX 状态码映射表（P0 实测后填充），并入 `config.py` 映射体系 |
| 保护限价 | `get_market_snapshot` 取盘口 → 复用现有保护价纯函数（保护比例/最小价位对齐逻辑不变） |
| 补偿对账 | 启动及重连后全量 `query_stock_orders` 回补当日委托；下单返回解析失败时，用 (代码+方向+数量+时间窗) 模糊匹配孤儿委托并接管 |
| 代码转换 | 边界双向转换，仅支持 A 股后缀（SH/SZ/BJ），其余后缀直接拒单 |

### 3.3 配置变更（`AgentConfig`）

```jsonc
{
  "execution_backend": "tdx",            // 新增: xtquant | tdx | mock（默认按现状 xtquant）
  "tdx_path": "C:\\new_tdx64",           // tdx 后端必填
  "tdx_account": "8888888888",           // 资金账号（stock_account 入参）
  "tdx_order_poll_interval_ms": 500,     // 委托轮询间隔（下限保护 200）
  "tdx_rpc_timeout_ms": 10000
}
```

桌面壳配置页增加"柜台类型"下拉（QMT/通达信），按类型显隐路径与账号字段；诊断页增加"通达信客户端在线/账户句柄有效"检测项。

## 4. 实施计划

| 阶段 | 内容 | 产出 / 通过标准 |
|---|---|---|
| **P0 技术验证**（先行） | 在 `C:\new_tdx64` 模拟环境验证：① `stock_account` 换句柄；② `order_stock` 下单并确认返回体中委托号字段；③ `query_stock_orders` 字段与状态码枚举实测；④ 撤单链路；⑤ `get_market_snapshot` 盘口字段与延迟；⑥ RPC 串行 QPS 与并发安全性；⑦ 客户端断开/重登后 ErrorId 6/7 恢复行为 | 验证报告：字段字典 + 状态码映射表 + go/no-go |
| **P1 后端抽象重构** | `client.py` 拆出 `bridge_core/backends/`：`base.py` 接口 + `xtquant_backend.py`（行为不变）+ `mock_backend.py`；`OrderLifecycleMonitor` 支持轮询驱动模式 | 现有单测全绿（回归无变化） |
| **P2 TdxBackend 开发** | 按 3.2 实现 `tdx_backend.py` + 代码格式转换 + TDX 状态映射 + 配置项 | 单测（mock DLL 层）+ 模拟盘冒烟 |
| **P3 联调与容错** | 模拟盘全链路：下单/撤单/部成/废单/拒单、通达信客户端重启、agent 重启、断网恢复、孤儿委托对账、轮询状态延迟测量 | 联调用例清单 + 修复记录 |
| **P4 桌面壳与打包** | 配置页柜台类型切换、诊断项、PyInstaller 捆绑 numpy/pandas、安装包与 COS 发布、README 更新 | 新版本安装包（version bump） |

依赖关系：P0 → P1 → P2 → P3 → P4；P1 可与 P0 并行启动（不依赖 TDX 实测结论）。

## 5. 风险清单

| 风险 | 等级 | 缓解 |
|---|---|---|
| 券商未开通/未验收通达信量化外接 | **高（前置）** | 开发前与目标券商确认开通政策；本方案保留 QMT 内置策略备选路线 |
| 无委托/成交推送 → 状态延迟 500ms~1s | 中 | 交易时段缩短轮询间隔；云端状态机容忍轮询延迟；上报带 `poll_ts` 便于审计 |
| 下单返回解析失败导致映射丢失 | 中 | 孤儿委托模糊匹配 + 全量对账兜底（3.2） |
| `GetOrderStr` DLL 并发安全性未知 | 中 | 默认全串行；P0 压测后再评估 |
| tqcenter 版本随客户端升级变化（v1.0.14 现状） | 中 | 原位导入不打包；启动时读取 tqcenter 版本号上报 + 不兼容告警 |
| 轮询 `query_stock_orders` 计入申报频率的合规口径不确定 | 低 | 查询类不属申报；仍保留派单限速熔断（≤50 笔/秒可配置） |
| exe 体积膨胀（numpy/pandas） | 低 | 可接受；后续可评估 lazy import 裁剪 |

## 6. 与现有代码的复用对照

| 现有模块 | 复用度 | 说明 |
|---|---|---|
| `auth.py` / `reporter.py` / `agent.py`(WS) | 100% | 云端链路不动 |
| `runtime_supervisor.py` / `schedule_policy.py` | 100% | 不动 |
| `runtime_workers.py` | ~90% | OrderLifecycleMonitor 增加轮询驱动模式 |
| `client.py` | 重构 | 拆为 backends/，xtquant 逻辑平移 |
| `config.py` | 扩展 | 新增 tdx 配置项 + TDX 状态映射表 |
| `desktop_app.py` | 小改 | 柜台类型切换 + 诊断项 |
| 打包/发布链路 | 100% | 仅增 numpy/pandas 依赖 |

## 7. 参考

- 通达信量化平台帮助文档：https://help.tdx.com.cn/quant/docs/
- 本机 `C:\new_tdx64\PYPlugins\user\tqcenter.py`（v1.0.14）
- `DOCS/QMT_Agent_内置策略桥接迁移方案.md`（备选路线）
- `tools/qmt_agent/README.md`（现有架构）
