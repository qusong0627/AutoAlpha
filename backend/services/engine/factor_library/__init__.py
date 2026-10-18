"""因子库（factor_library）—— 经典技术/量价因子库的实现与落盘。

组成：
  ops.py        通达信/聚宽技术指标算子（MyTT 口径，宽表向量化）
  alpha360.py   Alpha360 原始量价 60 日回溯（360）
  tdxgs.py      TDXGS 通达信/同花顺技术指标（88）
  jq110.py      JQ110 聚宽策略因子（109）

构建入口：backend/scripts/build_factor_library.py
表达式对照：data/quantdb/6_ml_datasets/alpha_library/expressions/（04/05/06）
落盘契约：<quantdb>/6_ml_datasets/<lib>/dt=YYYYMMDD/data.parquet（symbol, time, 因子列 float32）
"""
