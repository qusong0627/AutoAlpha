# factor_library —— 经典因子库（Alpha360 / TDXGS / JQ110）

把外部因子表达式（`alpha_library/expressions/` 04/05/06）落地为 QuantDB 数据集，
与 alpha_library 同一契约（分区 parquet、前复权价、无未来函数、float32）。

## 库与数据集

| 库 | 因子数 | 数据集 | 说明 |
|---|---|---|---|
| Alpha360 | 360 | `6_ml_datasets/alpha360/` | 6 字段 × 60 日原始量价回溯（DL 用） |
| TDXGS | 88 | `6_ml_datasets/tdxgs/` | 通达信/同花顺技术指标（MyTT 口径） |
| JQ110 | 109 | `6_ml_datasets/jq110/` | 聚宽策略因子（动量/情绪/技术/风险/风格） |

分区：`dt=YYYYMMDD/data.parquet`（symbol, time, OHLCV, 因子列 float32）；
附 OHLCV 与 daily_forward 同语义（因子报告 close_fwd 标签依赖 close 列）。

## 构建

```bash
python3 backend/scripts/build_factor_library.py --lib all          # 2016 至今，约 37 分钟
python3 backend/scripts/build_factor_library.py --lib tdxgs --max-symbols 300   # 冒烟
```

- 已有分区跳过（增量）；重跑先删目标目录；
- 内存策略：因子逐帧转 float32 即时释放（峰值 ~6GB）；alpha360 按日流式（不驻留 20GB）。

## 口径（详见 `ops.py` 头注释与各 expressions md）

- **严格窗口纪律**：rolling 类 `min_periods=窗口`（zoo 参考实现用 min_periods=1，
  会把不完整窗口算出来误导训练，不采用）；递归类（EMA/中国式 SMA/RSI/TRIX）按通达信 ewm 语义；
- **RSI 修正**：MyTT 有界式 `SMA(max(diff,0))/SMA(|diff|)×100`（zoo 的 up/dn 比值无上界，已弃用）；
- **JQ110 真实口径**：成交额/换手/流动性用名义口径真实值（非手数代理）；β 对中证500 真实回归；
  money_flow = Σ amount×sign(ret)（成交额×涨跌方向）；
- **无未来函数**：全部 rolling/shift/ewm/cumsum 仅用过去数据（已用独立重算核对移位方向）。

## 集成

- `factor_report/datasets.py` 已注册三库 → 因子报告页可直接评估（IC/分位/换手/相关性/去重簇）；
- IC 快照：`<dataset>/report/factor_report.json`（容器内跑
  `python3 backend/scripts/build_factor_report.py --dataset tdxgs`）；
- 训练特征选择：数据集可直接被训练管线读取（同 l1_factors 契约）。

> 免责声明同项目根 CLAUDE.md：仅供学习研究，不构成投资建议。
