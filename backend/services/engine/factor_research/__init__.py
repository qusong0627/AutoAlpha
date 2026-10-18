"""因子研究模块（factor-lib-demo 改造版）。

组成：
  catalog.py     —— 82 个因子元数据（来源/大类/小类/方向/公式/Wind 数据源）
  data.py        —— QuantDB 读取（daily_forward/valuation/index/instrument）
  engine.py      —— 行情类 + 估值类因子 + 截面打分
  financials.py  —— PIT 财报面板（income 单季 / cashflow YTD）+ 财务/行为因子
  analysis.py    —— IC / Top-N 回测 / KPI / 相关矩阵
  store.py       —— 快照 artifact 读取
  service.py     —— API 服务层（含实时合成）
  router.py      —— FastAPI 路由（/api/v1/factor-research）

注意：本包不在 __init__ 里挂 router，保证构建脚本（纯 pandas 环境）可直接 import。
"""
