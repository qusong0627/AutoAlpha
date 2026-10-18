"""admin 路由包（本地化：原仓库此处聚合约 30 个 admin 路由，
独立迁移只保留因子挖掘相关的 4 个模块，故初始化器置空）。

保留的模块：
- alpha_factor_pipeline.py     挖掘因子 → l1_factors 训练特征毕业
- rd_mined_materialize.py      rd_mined 物化后台任务控制台
- quantdb_factor_catalog.py    训练因子目录
- research_factor_registration.py
"""
