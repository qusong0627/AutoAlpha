"""Services 包初始化（本地化：改为惰性再导出）。

原文件在包初始化时**立即** import `backtest_service`，会连带拉起 qlib /
lightgbm / 回测栈。独立迁移只用到 `rd_agent_persistence`，而导入
`backend.services.engine.qlib_app.services.rd_agent_persistence` 必然先执行本
`__init__`，于是整条挖掘链被无关重依赖拖死。

改为 PEP 562 `__getattr__`：包本身可空导入，`from ...services import
QlibBacktestService` 仍按需生效，对外 API 面不变。
"""

__all__ = ["QlibBacktestService"]


def __getattr__(name: str):
    if name == "QlibBacktestService":
        from backend.services.engine.qlib_app.services.backtest_service import (
            QlibBacktestService,
        )

        return QlibBacktestService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
