# QMT Agent 内置策略桥接迁移方案（方案 A）

> 状态：方案评审中（未实施）
> 日期：2026-07-28
> 涉及模块：`tools/qmt_agent`
> 目标环境：国金 QMT 交易端（内置 Python 3.6），本机验证路径 `D:\国金QMT交易端模拟`

## 1. 背景与动因

### 1.1 政策背景

- 2024-05 证监会发布《证券市场程序化交易管理规定（试行）》（2024-10 施行），确立程序化交易"报告制 + 券商管理职责"框架。
- 2025-04-07 沪深北三大交易所同步发布《程序化交易管理实施细则》，**2025-07-07 起施行**：
  - 高频交易认定标准：单账户每秒申报/撤单 ≥300 笔，或单日 ≥2 万笔；
  - 券商对客户"外部接入"（外接系统/API 接口）承担核查、验收、留痕与监控责任。
- 传导结果：多家券商对 miniQMT 外接 API 收紧开通政策、暂停新开甚至通知停用。现有 qmt_agent 基于 xtquant/MiniQMT 外接通道，面临不可用风险。

### 1.2 结论

将"最后一公里"的柜台执行层从 **xtquant 外接** 迁移到 **QMT 主版本内置策略（passorder 官方合规通道）**，其余云端链路（鉴权、WebSocket、上报、派单队列、自动更新）全部保留。

## 2. 方案比选

| 方案 | 做法 | 结论 |
|---|---|---|
| **A. 本地桥接（选定）** | qmt_agent 进程保留全部云端逻辑，内置策略作为"薄执行端"，通过 loopback HTTP 通信 | ✅ 复用率 ~85%，风险最小 |
| B. SDK 包全部塞进 QMT 内置 Python | 全部逻辑降级到 Python 3.6 在 QMT 进程内运行 | ❌ 否决 |
| C. 多文件纯策略 | 编辑器只认单文件，实质等于 B | ❌ 不成立 |

### 否决方案 B 的理由

1. QMT 内置 Python 为 **3.6**（`python36.dll`），现有代码使用 `from __future__ import annotations`、`dataclasses`、新式 typing（均需 ≥3.7），且依赖 `websocket-client`（QMT site-packages 未内置），全量降级/vendor 成本高。
2. QMT UI 进程内跑云端 WS + 10 个 worker 线程：QMT 卡顿/崩溃 = 整条链路掉线，且失去 RuntimeSupervisor 监督重启能力。
3. 升级需替换磁盘包并重启 QMT 策略，无法复用现有 COS 自动更新链路。
4. 桌面壳（PyQt）无法进入 QMT 进程，运维诊断能力丢失。

### 环境事实（已在 D:\国金QMT交易端模拟 确认）

- 内置 Python 3.6（`bin.x64\python36.dll`、`Python36x64_2025-08-04.zip`）；
- site-packages 含 `requests`/`urllib3`，**无** `websocket-client`；
- 策略编辑器只管理单个 `.py` 文件，但可通过 `sys.path` 导入磁盘包；
- 策略示例位于安装目录 `python\` 下。

## 3. 目标架构

```
┌────────────── 云端 QuantMind ──────────────┐
│  quantmind-api / quantmind-trade            │
│  /internal/strategy/bridge/*  +  /ws/bridge │
└──────────────┬─────────────────────────────┘
               │ HTTPS + WSS（不变）
┌──────────────▼─────────────────────────────┐
│  qmt_agent.exe（桌面壳 + 云端网关 + 调度器）│
│  auth / reporter / runtime_workers          │
│  派单队列（撤单优先，50ms 节奏）            │
│  本地 loopback API :18965（扩展指令端点）   │
└──────────────┬─────────────────────────────┘
               │ 127.0.0.1 HTTP（新增一跳）
┌──────────────▼─────────────────────────────┐
│  QMT 内置策略 quantmind_bridge_strategy.py  │
│  轮询取指令 → passorder / cancel            │
│  order_callback / deal_callback → 回报 POST │
└──────────────┬─────────────────────────────┘
               │ 官方合规通道
        国金 QMT 柜台（券商托管）
```

### 3.1 职责划分

| 组件 | 职责 | 变化 |
|---|---|---|
| qmt_agent.exe | AK/SK 鉴权、云端 WS 收指令、账户/心跳/执行上报、派单队列、桌面壳诊断、自动更新 | 移除 xtquant 直连；`client.py` 柜台层替换为"本地执行通道" |
| 内置策略（单文件） | 轮询取指令、passorder/cancel、订单/成交回调回传、账户快照采集回传 | 全新，约 200~300 行，仅依赖 stdlib + requests（3.6 兼容） |

### 3.2 本地通信协议（loopback :18965 扩展）

写操作沿用现有 `X-Local-Token` 鉴权（环境变量 `QMT_AGENT_LOCAL_API_TOKEN`）。

| 端点 | 方向 | 说明 |
|---|---|---|
| `GET /bridge/commands?wait=1&timeout=5` | 策略 → agent | 长轮询取指令批次（下单/撤单），空闲时 5s 超时返回空 |
| `POST /bridge/ack` | 策略 → agent | 指令受理确认（返回 QMT 柜台 order_id 或错误） |
| `POST /bridge/events` | 策略 → agent | 委托状态/成交回报（来自 order_callback/deal_callback） |
| `POST /bridge/snapshot` | 策略 → agent | 账户资金/持仓快照（策略侧周期采集） |
| `GET /bridge/strategy_health` | 桌面壳/运维 | 策略在线状态（最近一次轮询时间戳） |

### 3.3 指令与回报映射

- `client_order_id ↔ QMT order_id` 双向映射逻辑保留在 agent 侧（迁移自 `client.py`）；
- passorder 使用 `userOrderId` 参数携带 `client_order_id`，回调中透传，避免映射丢失；
- QMT 状态码映射表复用 `config.py` 现有表（50→SUBMITTED、56→FILLED、57→REJECTED 等）；
- 保护限价（`protect_limit`）：Level1 盘口在策略侧读取（内置 API），保护价计算逻辑下放到策略文件（纯函数，从现有代码移植，3.6 兼容）。

### 3.4 容错与监督

| 故障 | 感知方 | 处理 |
|---|---|---|
| QMT 关闭/策略停止 | agent（轮询超时 > 阈值） | 标记执行端离线，云端心跳携带 degraded 状态，拒绝新指令并回写 REJECTED |
| agent 崩溃 | RuntimeSupervisor | 指数退避重启（不变）；策略轮询失败静默重试 |
| 指令超时未 ack | agent | 超时补偿：透过 `/bridge/events` 的启动补偿查询（策略侧 `get_trade_detail_data`）对账 |
| 断电/重启 | 双方 | 策略启动时全量回补当日委托/成交，agent 与云端对账 |

### 3.5 合规注意事项

- 账户需完成程序化交易报告备案（券商侧办理）；
- 派单节奏保持 ≥50ms（现状），远低于高频认定线（300 笔/秒）；撤单/改单风暴场景需在 agent 派单队列增加每秒申报上限熔断（建议默认 ≤50 笔/秒，可配置）。

## 4. 交付物与部署

### 4.1 交付物变化

| 项 | 现状 | 迁移后 |
|---|---|---|
| qmt_agent.exe | 含 xtquant 直连 | 移除 xtquant，体积减小；打包/InnoSetup/COS 自动更新链路不变 |
| 策略文件 | 无 | `quantmind_bridge_strategy.py` 随安装包分发至安装目录，用户导入 QMT 策略编辑器 |
| 桌面壳 | 现有诊断页 | 新增"内置策略在线"检测项 + 一键复制策略文件路径引导 |

### 4.2 用户部署流程

1. 安装/升级 qmt_agent（自动更新或安装器）；
2. 桌面壳配置 AK/SK、账户；
3. 打开国金 QMT → 策略编辑器导入 `quantmind_bridge_strategy.py` → 运行；
4. 桌面壳诊断页确认：云端 WS ✅ / 本地策略 ✅ / 账户快照 ✅。

## 5. 实施计划

| 阶段 | 内容 | 产出 |
|---|---|---|
| P0 技术验证 | 在 D:\国金QMT交易端模拟 编写最小策略，验证：passorder + userOrderId 透传、order/deal 回调、后台线程存活、requests 访问 loopback、长轮询稳定性、策略停止时线程清理 | 验证报告 + go/no-go |
| P1 协议与 agent 改造 | 18965 API 扩展 5 个端点；`client.py` 抽象出 `ExecutionBackend` 接口，新增 `LocalBridgeBackend`；保留 mock 模式 | agent 侧代码 + 单测 |
| P2 策略文件开发 | 单文件策略（3.6 兼容）：长轮询、passorder/cancel、回调回传、快照采集、保护限价移植、启动回补 | `quantmind_bridge_strategy.py` |
| P3 联调与容错 | 模拟盘全链路：下单/撤单/部成/废单、QMT 重启、agent 重启、断网恢复、对账 | 联调用例清单 + 修复 |
| P4 打包发布 | 移除 xtquant 依赖、安装包携带策略文件、桌面壳诊断项、README 更新 | 新版本安装包（version bump） |

## 6. 风险清单

| 风险 | 等级 | 缓解 |
|---|---|---|
| 内置策略回调时序/字段与 xtquant 差异 | 中 | P0 验证期实测字段，映射表按实测修订 |
| 长轮询在 QMT 内置 3.6 requests 下的连接稳定性 | 中 | P0 压测；退化方案：短轮询 200ms |
| 用户忘记运行策略 | 中 | 桌面壳醒目告警 + 云端心跳 degraded 状态 |
| 券商后续对内置策略网络访问加限制 | 低 | 关注券商通知；备选 PTrade 通道预研 |
| 策略文件被用户误改 | 低 | 文件头校验版本号，agent 检测策略版本不匹配时告警 |

## 7. 参考

- 证监会《证券市场程序化交易管理规定（试行）》
- 沪深北交易所《程序化交易管理实施细则》（2025-07-07 施行）
- `tools/qmt_agent/README.md`（现有架构与鉴权链路）
