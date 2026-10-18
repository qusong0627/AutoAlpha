# TDX L2 实时推理 + 自动买卖链路 v1

> 基于 TQ 接口实测能力（2026-08-25）设计：十档/千档/逐笔明细 TQ 未开放，
> 实时数据源 = 5 档快照(3s) + L2 扩展日线聚合(30~60s) + 88 字段扩展信息。

## 一、总体链路

```
TQ 客户端(Windows) ─JSON-RPC─> 桥 :8550 ─HTTP─> Linux trade 服务
                                                    │
   tdx_l2_capture_task (每60s, 候选池+持仓 ≤50只)     ▼
   ├─ get_exday_data  ─> PG tdx_l2_snapshot(时间序列) + tdx_l2_daily(日末)
   └─ get_more_info   ─> 同上 (L2TicNum/OrderNum)
                                                    │
   实时因子 (采集时算, 存 Redis tdx:l2:realtime:{symbol})
   ├─ OB_BALANCE   订单流不平衡 = ((BOrder−BCancel)−(SOrder−SCancel))/总量
   ├─ AVP_GAP      委买卖价差方向 = (SellAvp−BuyAvp)/BuyAvp
   ├─ BIG_BUY      大单主买占比 (Vol 4×4 矩阵)
   └─ TICK_ACT     逐笔活跃度 = ΔL2TicNum/分钟
                                                    │
   tdx_l2_realtime_task (每60s)                       ▼
   ├─ 实时胜率分 = 0.6×日频分(当日推理缓存) + 0.4×实时信号分(0~100)
   ├─ 候选池实时分 > 65 且大盘>MA20 → 推送买入 (tdx/paper 三档模式)
   └─ 持仓实时分 < 45 且 T+1 可卖 → 自动市价卖出
```

## 二、数据层

### 表（trade 库，create_registered_tables 注册 `trade.tdx_l2`）

```sql
CREATE TABLE tdx_l2_snapshot (
  id BIGSERIAL PRIMARY KEY,
  trade_date VARCHAR(8) NOT NULL, ts TIMESTAMPTZ NOT NULL,
  symbol VARCHAR(16) NOT NULL,          -- SH600206 (prefix)
  stock_code VARCHAR(16) NOT NULL,      -- 600206.SH
  cjbs BIGINT,                          -- 成交单数
  b_order/b_cancel/s_order/s_cancel DOUBLE PRECISION,   -- 净挂单/净撤单
  buy_avp/sell_avp DOUBLE PRECISION,    -- 委买/委卖均价
  total_b_order/total_s_order DOUBLE PRECISION,
  vol_4x4/amo_4x4/vol_num JSONB,        -- 分档矩阵(原始)
  l2_tic_num/l2_order_num BIGINT,       -- 逐笔计数 (more_info)
  total_b_vol/total_s_vol DOUBLE PRECISION,
  ob_balance/avp_gap/big_buy_ratio/tick_activity DOUBLE PRECISION,  -- 实时因子
  UNIQUE (symbol, ts)
);
CREATE INDEX ON tdx_l2_snapshot(symbol, trade_date);
CREATE TABLE tdx_l2_daily (...);  -- 每日最后一帧, UNIQUE(trade_date, symbol)
```

### 采集任务

- 轮询集合 = 当日推理候选池 Top N（`load_latest_scores`，N 默认 20）+ 全部持仓（paper+tdx）
- 桥读限流 60 次/分钟 → 预算分配：exday 每 60s/只（20 只=20/min）+ more_info 每 120s/只（10/min）≈ 30/min ✅
- watchlist 超 30 只自动拉长间隔：`exday_interval = max(60, len*3)`；RATE_LIMITED 退避加倍
- 落库 + Redis 双写（Redis 供实时推理毫秒读）

### 实时因子（0~100 信号分合成）

```
ob_score  = clip(50 + ob_balance*100, 0, 100)      # 0.5 中性
avp_score = clip(50 − avp_gap*1000, 0, 100)        # 卖均价高 → 空头
big_score = clip(big_buy_ratio*100, 0, 100)
act_score = min(100, tick_activity/10)             # >1000 笔/min 满分
signal    = 0.40*ob + 0.25*avp + 0.20*big + 0.15*act
```

## 三、实时胜率分与触发

```
realtime_score = 0.6 * (fusion_score/3*100) + 0.4 * signal
买入: realtime_score > buy_trigger(65) 且 大盘>MA20 且 冷却已过 且 当日未买
卖出: realtime_score < sell_trigger(45) 且 available_volume>0 (T+1已解锁)
冷却: 每只 30 分钟 (Redis tdx:l2:cooldown:{symbol})
```

- 执行复用 `run_rolling_push` 的 place_rolling_orders / place_paper_orders（三档模式）
- 重复保护：当日已成交买入/卖出查询（桥 pull_orders / paper 持仓）

## 四、配置（Redis `tdx:l2:config` + API）

| 参数 | 默认 | 说明 |
|------|------|------|
| pool_size | 20 | 候选池大小 |
| buy_trigger | 65 | 买入实时分阈值 |
| sell_trigger | 45 | 卖出实时分阈值 |
| interval_sec | 60 | 实时轮询周期 |
| cooldown_min | 30 | 单只冷却 |
| factor_weights | 0.40/0.25/0.20/0.15 | 信号合成权重 |

API：`GET/PUT /tdx/l2-config`、`GET /tdx/l2/realtime`（Redis 实时分批量查询）

## 五、交付物

| 文件 | 内容 |
|------|------|
| `services/tdx_l2_capture_task.py` | 采集+因子计算+落库+Redis |
| `services/tdx_l2_realtime.py` | 实时分合成+触发执行 |
| `routers/tdx_l2.py` | config API + realtime 查询 |
| `main.py` | 注册两个后台任务 + 表注册 |
| `tests/test_tdx_l2_capture.py` | 解析/因子/落库单测 |
| `tests/test_tdx_l2_realtime.py` | 合成/触发/冷却/去重单测 |
| `docs/integrations/tdx-l2-realtime.md` | 本文 |

## 六、边界与风险

- 十档/千档/逐笔明细拿不到 → 用聚合替代，信号保守化
- Vol 4×4 矩阵索引布局待实测后精调 big_buy_ratio
- 实时分是"日频分×修正"近似，v2 可训练独立盘中模型
- 触发参数全部可改，默认值保守（65/45/60s/30min）
