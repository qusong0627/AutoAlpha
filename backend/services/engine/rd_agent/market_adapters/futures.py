"""期货/贵金属市场适配器"""

from __future__ import annotations

import os
from pathlib import Path

from . import register_adapter
from .base import BacktestConfig, DataConfig, MarketAdapter


# 期货专用因子集 — 适配期货特性（无涨跌停、T+0、保证金杠杆、主力连续合约）
FUTURES_ALPHA = {
    # K线形态
    "FUMID": "($close - $open) / $open",
    "FULEN": "($high - $low) / $open",
    "FUMID2": "($close - $open) / ($high - $low + 1e-12)",
    "FUUP": "($high - Greater($open, $close)) / $open",
    "FULOW": "(Less($open, $close) - $low) / $open",
    "FUSFT": "(2 * $close - $high - $low) / $open",
    "FUPOS": "(2 * $close - $high - $low) / ($high - $low + 1e-12)",
    # 短期动量 (1-10天，期货日内趋势强)
    "FUROC1": "Ref($close, 1) / $close - 1",
    "FUROC2": "Ref($close, 2) / $close - 1",
    "FUROC5": "Ref($close, 5) / $close - 1",
    "FUROC10": "Ref($close, 10) / $close - 1",
    "FUROC20": "Ref($close, 20) / $close - 1",
    # 对数收益动量
    "FULRET1": "Log($close / Ref($close, 1))",
    "FULRET5": "Log($close / Ref($close, 5))",
    "FULRET10": "Log($close / Ref($close, 10))",
    "FULRET20": "Log($close / Ref($close, 20))",
    # 均线
    "FUMA3": "Mean($close, 3) / $close",
    "FUMA5": "Mean($close, 5) / $close",
    "FUMA10": "Mean($close, 10) / $close",
    "FUMA20": "Mean($close, 20) / $close",
    # 波动率 (期货波动大，关注近期)
    "FUSTD3": "Std($close, 3) / $close",
    "FUSTD5": "Std($close, 5) / $close",
    "FUSTD10": "Std($close, 10) / $close",
    "FUSTD20": "Std($close, 20) / $close",
    "FULSTD5": "Std(Log($close / Ref($close, 1)), 5)",
    "FULSTD10": "Std(Log($close / Ref($close, 1)), 10)",
    "FULSTD20": "Std(Log($close / Ref($close, 1)), 20)",
    # 趋势强度
    "FURSQR5": "Rsquare($close, 5)",
    "FURSQR10": "Rsquare($close, 10)",
    "FURSQR20": "Rsquare($close, 20)",
    # 价格极值
    "FUMAX5": "Max($high, 5) / $close",
    "FUMAX10": "Max($high, 10) / $close",
    "FUMIN5": "Min($low, 5) / $close",
    "FUMIN10": "Min($low, 10) / $close",
    # RSV
    "FURSV5": "($close - Min($low, 5)) / (Max($high, 5) - Min($low, 5) + 1e-12)",
    "FURSV10": "($close - Min($low, 10)) / (Max($high, 10) - Min($low, 10) + 1e-12)",
    # 量价相关
    "FUCORR5": "Corr(Log($close), Log($volume + 1), 5)",
    "FUCORR10": "Corr(Log($close), Log($volume + 1), 10)",
    "FUCORR20": "Corr(Log($close), Log($volume + 1), 20)",
    # 成交量变化
    "FUVOLCH5": "Log($volume / Ref($volume, 5))",
    "FUVOLCH10": "Log($volume / Ref($volume, 10))",
    "FUVOLCH20": "Log($volume / Ref($volume, 20))",
    # 涨跌天数
    "FUCNTP5": "Mean($close > Ref($close, 1), 5)",
    "FUCNTP10": "Mean($close > Ref($close, 1), 10)",
    "FUCNTP20": "Mean($close > Ref($close, 1), 20)",
    "FUCNTD5": "Mean($close > Ref($close, 1), 5) - Mean($close < Ref($close, 1), 5)",
    "FUCNTD10": "Mean($close > Ref($close, 1), 10) - Mean($close < Ref($close, 1), 10)",
}


@register_adapter
class FuturesAdapter(MarketAdapter):
    """期货/贵金属市场适配器"""

    market_id = "futures"
    market_name = "期货"
    description = "期货/贵金属（上金所+国内商品+国际合约），akshare 数据源，FuturesAlpha 因子集 (40+ 因子)"

    def get_data_config(self) -> DataConfig:
        return DataConfig(
            provider_uri=self.get_qlib_provider_uri(),
            data_dir="/app/db/futures_data",
            calendar="day",
            market="futures",
            extra={"symbols": "CL.FUT,RB0.CN,Au99.99,...", "freq": "day"},
        )

    def get_qlib_provider_uri(self) -> str:
        # 与 QlibDataBuilder.for_market("FUTURES") 输出一致：
        # {quantfutures_data_dir}/.qlib_cache/futures_data
        from backend.services.engine.data_platform.quantfutures_hub import (
            _resolve_quantfutures_data_dir,
        )

        # 统一固定目录优先（/data/qlib/{sub}）
        try:
            from backend.shared.qlib_paths import resolve_qlib_provider_uri
            fixed = resolve_qlib_provider_uri("FUTURES")
            if os.path.isdir(fixed):
                return fixed
        except Exception:
            pass
        return str(Path(_resolve_quantfutures_data_dir()) / ".qlib_cache" / "futures_data")

    def get_backtest_config(self) -> BacktestConfig:
        return BacktestConfig(
            annualization_days=252,
            limit_threshold=1.0,  # 期货无涨跌停
            commission_rate=0.0001,  # 期货手续费按合约价值比例（远低于股票）
            min_commission=0.0,
            region="cn",  # Qlib region 仍用 cn（数据格式相同）
            needs_adjustment_factor=False,  # 主力连续合约无复权
            extra={"futures_mode": True, "margin_mode": True},
        )

    def get_factor_set(self) -> dict[str, str]:
        return FUTURES_ALPHA.copy()

    def get_factor_set_name(self) -> str:
        return "futures_alpha"

    def get_prop_setting_class(self) -> str:
        return "rdagent.app.qlib_rd_loop.conf.FactorBasePropSetting"

    def get_env_overrides(self) -> dict[str, str]:
        api_key = (
            os.getenv("AI_IDE_LLM_API_KEY")
            or os.getenv("AI_IDE_API_KEY")
            or os.getenv("OPENAI_API_KEY", "")
        )
        return {
            "QLIB_PROVIDER_URI": self.get_qlib_provider_uri(),
            "OPENAI_BASE_URL": os.getenv("OPENAI_BASE_URL", ""),
            "OPENAI_API_KEY": api_key,
            "CHAT_MODEL": os.getenv("CHAT_MODEL", ""),
            "REASONING_MODEL": os.getenv("CHAT_MODEL", ""),
            "CHAT_STREAM": "false",
            "CHAT_MAX_TOKENS": os.getenv("CHAT_MAX_TOKENS", "8000"),
            "CHAT_TEMPERATURE": os.getenv("CHAT_TEMPERATURE", "0.3"),
            "FUTURES_MODE": "true",
        }

    def prepare_data(self) -> bool:
        """从 QuantFutures parquet 构建 Qlib 二进制缓存。"""
        from backend.services.engine.qlib_data_builder import QlibDataBuilder

        try:
            builder = QlibDataBuilder.for_market("FUTURES")
            builder.build_all(incremental=True)
            return True
        except Exception as e:
            import logging

            logging.getLogger(__name__).error("Futures data preparation failed: %s", e)
            return False

    def is_data_ready(self) -> bool:
        """检查 Qlib 目录 + calendars + instruments + features 是否齐全。"""
        from pathlib import Path

        p = Path(self.get_qlib_provider_uri())
        return (
            p.is_dir()
            and (p / "calendars" / "day.txt").is_file()
            and (p / "instruments" / "all.txt").is_file()
            and (p / "features").is_dir()
            and len(list((p / "features").iterdir())) > 0
        )
