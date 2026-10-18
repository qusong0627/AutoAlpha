"""统计派挖掘内核（移植 P2，机构级 Tier 1+2）。

与 LLM 派（``rdagent``）并列的第二个内置算法：不调用任何 LLM，直接在
QuantDB 行情面板上计算经典价量候选 → A 股可交易性过滤 → 可选行业/市值
中性化 → 逐日截面 IC/RankIC 检验（多周期 + 样本外一致性 + 多重检验修正 +
分组单调性）→ 达标候选按 |RankIC| 取 Top-K，携带真实统计量与可执行表达式
入库。模块划分：

- :mod:`panel`      —— 取数（quantdb_hub）、面板规范化、可交易性过滤、
                      行业/股本元数据；
- :mod:`candidates` —— 候选因子族（formulation + 文献出处 + pandas 实现 +
                      qlib 表达式双表示）；
- :mod:`neutralize` —— 行业 / 行业+市值中性化（逐日截面回归残差）；
- :mod:`ic_stats`   —— 截面 IC 统计核心（IC/RankIC/ICIR/t 值/分组/BH-FDR）；
- :mod:`engine`     —— 编排：取数 → 过滤 → 中性化 → 评估 → 选择 → 结果。
"""

from __future__ import annotations
