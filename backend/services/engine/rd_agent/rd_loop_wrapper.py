"""RDLoop 包装器 — 将 RD-Agent 的 FactorRDLoop 适配到 QuantMind

提供统一接口:
- 接收 MarketAdapter 配置市场参数
- 启动/监控/取消 RDLoop
- 从日志中提取因子结果
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
from bisect import bisect_right
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

# 运行时导入（不止注解用）：``quantdb_coverage_from_dir`` 要构造 Window。
# mining_config 只依赖 mining_datasets（纯表），不反向依赖本模块，无环。
from backend.services.engine.alpha_agent.mining_config import Window
from backend.shared.safe_pickle import load_pickle

from .market_adapters import get_adapter, list_markets
from .market_adapters.base import MarketAdapter
from .mining_datasets import datasets_from_env
from .qlib_conf_template import install_qlib_execute_patch
from .stage_prompt import STAGE_BLOCK_MARKER, render_eval_criteria_block

logger = logging.getLogger(__name__)

# H5 已退役（A 股）：挖掘数据面 = 基础面板 ``panel.parquet``（7 列）+
# 取数 SDK ``quantdb_data.py``，经 ``_ensure_panel_bundle`` 下发到任务数据目录；
# 富化列（技术指标/估值/资金流/L2）由因子代码经 ``qd.daily(数据集, [列])`` 按需
# 读取，映射单源见 ``mining_datasets``。本文件不再生成/消费任何 .h5（期货与
# 其他市场沿用各自旧链路，见 ``_generate_futures_h5`` / ``_copy_h5_source``）。


def _extract_module_docstring(source: str) -> str:
    """取模块级 docstring（首个三引号块），供 prompt 注入使用说明。"""
    import re

    m = re.search(r'^"""(.*?)"""', source, flags=re.S | re.M)
    if m:
        return m.group(1).strip()
    m = re.search(r"^'''(.*?)'''", source, flags=re.S | re.M)
    return m.group(1).strip() if m else ""


class MinuteDataError(RuntimeError):
    """分钟数据面（provider / 面板）不可用——显式失败，绝不静默退回日线。

    分钟任务最坏的失败形态是「悄悄按日线跑完、产出一批自称分钟因子的东西」，
    所以从 provider 构建到面板生成、再到 conf 渲染，每一环拿不到分钟数据都
    直接抛错让任务失败。
    """


def _minute_panel_name(debug: bool, start: date, end: date) -> str:
    """分钟面板缓存文件名（窗口即身份，防串窗）。

    「窗口 A 生成的缓存冒充窗口 B 的数据面」是静默错数据，所以缓存身份 =
    窗口。 provider 重建 = 整目录原子换入 → 缓存随之失效，不存在口径漂移中间态。
    """
    base = "minute_pv_debug" if debug else "minute_pv_all"
    return f"{base}__{start.isoformat()}_{end.isoformat()}.parquet"


#: 各市场 → ``shared.qlib_paths`` 的 provider key（回测日历所在目录）
_QLIB_PROVIDER_KEY_BY_MARKET: dict[str, str] = {
    "a_share": "CN",
    "hong_kong": "HK",
    "us_stock": "US",
    "crypto": "CRYPTO",
    "futures": "FUTURES",
}


def quantdb_coverage_from_dir(
    quantdb_dir: str | os.PathLike[str] | None,
) -> Window | None:
    """QuantDB 日线覆盖窗口（挖掘数据面同源）。

    覆盖 = ``1_kline_data/daily_forward`` 分区目录 ``dt=YYYYMMDD`` 的 min/max
    ——挖掘面板实际读取的库，不用交易日历冒充数据覆盖。

    ``quantdb_dir`` 为空 / 该目录下没有任何分区 → ``None``（语义 = 不做覆盖
    校验、不做末端保护：宁可放行由引擎侧失败，也不做假校验）；目录不存在或
    不可读时**抛 OSError**（不吞——由调用方决定是记日志放行还是拦），2000+ 个
    分区目录的现读由调用方缓存（路由侧 TTL 300s）。
    """
    if not quantdb_dir:
        return None
    daily_dir = Path(quantdb_dir) / "1_kline_data" / "daily_forward"
    days = sorted(
        d.name[3:]
        for d in daily_dir.iterdir()
        if d.is_dir() and d.name.startswith("dt=")
    )
    if not days:
        return None
    return Window(
        date.fromisoformat(f"{days[0][:4]}-{days[0][4:6]}-{days[0][6:8]}"),
        date.fromisoformat(f"{days[-1][:4]}-{days[-1][4:6]}-{days[-1][6:8]}"),
    )


def _read_calendar_days(provider_dir: Path) -> list[date]:
    """读 qlib provider 的 ``calendars/day.txt`` → 升序交易日列表（空 = 不可用）。"""
    text = (provider_dir / "calendars" / "day.txt").read_text(encoding="utf-8")
    return [date.fromisoformat(ln.strip()) for ln in text.splitlines() if ln.strip()]


def _last_trading_day_on_or_before(day: date, days: Sequence[date]) -> date | None:
    """``days`` 中 ≤ ``day`` 的最后一个交易日；没有回 ``None``。"""
    idx = bisect_right(list(days), day) - 1
    return days[idx] if idx >= 0 else None


def qlib_backtest_data_end(
    coverage: Window | None = None, *, market: str = "a_share"
) -> date | None:
    """回测可用末端（交易日）= qlib 日历倒数第二个交易日 ∩ 数据覆盖末端。

    **为什么是倒数第二个**：qlib 回测按 t+1 成交，``TradeCalendarManager
    .get_step_time`` 取「下一根 bar」的时间步（qlib/backtest/utils.py:131），
    回测终点压在日历最后一根 bar 上必然 ``IndexError: index N is out of
    bounds for axis 0 with size N``（2026-10-10 实证：test_end=2026-10-09 崩、
    前移一个交易日通过）。这个末端恰恰是默认窗口会锚到的位置，所以不是边角
    情形，是"默认配置必然跑挂"。

    再取 min 覆盖末端：qlib 缓存（provider_uri）可能落后于 QuantDB 覆盖，
    只锚覆盖会照旧把终点推到缓存日历的最后一根 bar 上（同一个 IndexError）；
    只锚日历则会在覆盖回退时读过界。覆盖末端落在非交易日（周末/假日）时
    向前贴到最近交易日——否则 min 出来的仍是「日历最后一根 bar」之外的日子。

    读不到日历（provider 未就绪）/ 市场无对应 provider / 覆盖整体早于日历
    → ``None`` = **不做末端保护**（与 ``validate_coverage`` 同一哲学：宁可放行
    由引擎侧失败，也不做假保护）。
    """
    provider_key = _QLIB_PROVIDER_KEY_BY_MARKET.get(market)
    if not provider_key:
        return None
    try:
        from backend.shared.qlib_paths import resolve_qlib_provider_uri

        days = _read_calendar_days(Path(resolve_qlib_provider_uri(provider_key)))
    except Exception as exc:  # noqa: BLE001 - 日历读不到不拦挖掘（只记一笔）
        logger.warning("[%s] qlib 回测日历读取失败，跳过末端保护: %s", market, exc)
        return None
    if len(days) < 2:
        return None
    guard = days[-2]
    if coverage is None or coverage.end >= guard:
        return guard
    return _last_trading_day_on_or_before(coverage.end, days)


class RDLoopWrapper:
    """封装 RD-Agent FactorRDLoop，提供 QuantMind 兼容接口"""

    def __init__(self, market: str = "a_share") -> None:
        self.adapter: MarketAdapter = get_adapter(market)
        if self.adapter is None:
            raise ValueError(
                f"Unknown market: {market}. Available: {[m['market_id'] for m in list_markets()]}"
            )
        self.market = market
        self._loop = None
        self._running = False
        self._cancelled = False

    @property
    def market_name(self) -> str:
        return self.adapter.market_name

    def _freq(self) -> str:
        """本任务挖掘频率（env ``QM_MINING_FREQ``；缺省 day；非法值/市场不支持显式抛）。"""
        from backend.services.engine.alpha_agent.mining_config import (
            freq_from_env,
            normalize_freq,
        )

        return normalize_freq(freq_from_env(), market=self.market)

    def _ensure_minute_provider(self) -> Path:
        """确保 1min provider 就绪且新鲜（本次重建则记录报告），返回目录。"""
        from backend.services.engine import minute_qlib_provider as MP

        provider_dir, report = MP.ensure_minute_provider(
            universe=MP.DEFAULT_UNIVERSE,  # conf 模板钉 csi300，两者必须同池
            log=lambda msg: logger.info("[%s] %s", self.market, msg),
        )
        if report is not None:
            logger.info(
                "[%s] minute provider 构建完成：%d 只 / %d bar / %d 日（%s~%s），警告 %d 条",
                self.market,
                report.symbols,
                report.bars,
                report.days,
                report.first,
                report.last,
                len(report.warnings),
            )
            for msg in report.warnings[:10]:
                logger.warning("[%s] minute provider 警告: %s", self.market, msg)
        return provider_dir

    def _generate_minute_panel(
        self,
        provider_dir: str | os.PathLike[str],
        output_path: str,
        *,
        debug: bool = False,
    ) -> bool:
        """从分钟 provider bins 生成任务数据面 ``panel.parquet``（1min MultiIndex）。

        - 列 = provider 的 8 个 1min 字段（open/high/low/close/volume/amount/
          vwap/factor，面板文件存原始列名，``qd.panel()`` 回绕加 ``$``），与因子
          表达式读到的是**同一份 bin**——写代码时看到的数据与评估时算的数据逐位一致；
        - 窗口 = [挖掘起点, 检验终点]（自定义窗口）。分钟挖掘**必须**显式给窗口：
          默认窗口是 2016/2020 年起的日线尺度，落在 121 天分钟覆盖之外——这里
          显式拒，不静默截断成"看上去成功、实际换了个区间"；
        - debug 变体取码表前 50 只（与日线 debug 同一惯例），LLM 先在 debug 上
          试代码、全量变体跑全池；
        - 共享缓存放 provider 目录内 ``panel_cache/``：provider 重建 = 整目录原子
          换入 → 缓存随之失效，不存在口径漂移的中间态。
        """
        from backend.services.engine import minute_qlib_provider as MP

        custom = self._custom_windows()
        if custom is None:
            raise MinuteDataError(
                "分钟挖掘必须显式指定挖掘数据区间（QM_MINING_WINDOW_* 未设置或非法）："
                "分钟数据覆盖只有约 121 个交易日，日线尺度的默认窗口不适用"
            )
        start = custom[0].start
        end = (custom[1] or custom[0]).end
        facts = MP.minute_data_facts(provider_dir)
        if facts is None:
            raise MinuteDataError(
                f"分钟 provider meta 不可用：{Path(provider_dir) / 'meta.json'}；"
                "先构建分钟数据面（minute_qlib_provider）再来挖"
            )
        cover_start, cover_end = facts.coverage.start, facts.coverage.end
        if start < cover_start or end > cover_end:
            raise MinuteDataError(
                f"数据窗口 {start.isoformat()} ~ {end.isoformat()} 超出分钟数据覆盖 "
                f"{cover_start.isoformat()} ~ {cover_end.isoformat()}；"
                "请把区间收进覆盖范围（前端会给出可用端点）"
            )
        if not facts.contains(start, end):
            # 起点落在覆盖内但跨过空洞（或整段落在空洞里）：按可用连续块拦
            raise MinuteDataError(
                f"数据窗口 {start.isoformat()} ~ {end.isoformat()} 跨过分钟数据空洞 "
                f"（{facts.gap_text()}）；可用连续块 "
                f"{facts.usable_start.isoformat()} ~ {cover_end.isoformat()}，"
                f"共 {facts.usable_days} 个交易日"
            )
        self._minute_window = (start, end)

        meta = MP.read_minute_meta(provider_dir) or {}
        symbols = sorted(str(s) for s in meta.get("built_symbols", []))
        if not symbols:
            raise MinuteDataError(f"分钟 provider 没有已构建标的：{provider_dir}")
        if debug:
            symbols = symbols[:50]

        cache_dir = Path(provider_dir) / "panel_cache"
        cache_path = cache_dir / _minute_panel_name(debug, start, end)
        if cache_path.is_file():
            logger.info("[%s] minute panel cache hit: %s", self.market, cache_path)
            self._link_panel(cache_path, Path(output_path))
            return True

        try:
            frame = MP.read_minute_frame(provider_dir, symbols, start=start, end=end)
            if frame.empty:
                logger.error("[%s] minute frame 为空：%s", self.market, provider_dir)
                return False
            # 面板文件存原始列名（去一层 $ 前缀），与日线面板同一约定
            frame = frame.rename(
                columns={
                    c: (c[1:] if isinstance(c, str) and c.startswith("$") else c)
                    for c in frame.columns
                }
            )
            cache_dir.mkdir(parents=True, exist_ok=True)
            tmp_path = f"{cache_path}.tmp-{os.getpid()}"
            frame.to_parquet(tmp_path, engine="pyarrow", compression="zstd")
            os.replace(tmp_path, cache_path)
            self._link_panel(cache_path, Path(output_path))
            logger.info(
                "[%s] Generated minute panel: %s (%d rows, %d cols, %s~%s)",
                self.market,
                cache_path,
                len(frame),
                frame.shape[1],
                start,
                end,
            )
            return True
        except Exception as exc:  # noqa: BLE001 —— 生成失败如实返回 False 由调用方响亮失败
            logger.error("[%s] Failed to generate minute panel: %s", self.market, exc)
            return False

    def _configure_env(self, task_log_dir: str) -> dict[str, str]:
        """从 MarketAdapter 构建环境变量"""
        env_overrides = self.adapter.get_env_overrides()
        env = {
            **os.environ,
            **env_overrides,
            "LOG_TRACE_PATH": task_log_dir,
            "PYTHONPATH": os.getenv("PYTHONPATH") or "/app",
        }
        # 设置数据文件路径环境变量
        data_file = "/app/alphaagent/scenarios/qlib/experiment/factor_data_template/daily_pv_all.h5"
        if os.path.exists(data_file):
            env["FACTOR_DATA_PATH"] = data_file
        # QuantDB 数据根（取数 SDK 的定位锚；子进程继承后 qd.root()/panel() 免猜路径）
        try:
            _qd = self._resolve_quantdb_dir()
            if _qd:
                env.setdefault("QM_QUANTDB_DATA_DIR", _qd)
        except Exception:  # noqa: BLE001 - 定位失败不拦环境配置
            pass
        # Ensure critical LLM settings are present
        if not env.get("OPENAI_BASE_URL"):
            env["OPENAI_BASE_URL"] = os.getenv("OPENAI_BASE_URL", "")
        if not env.get("OPENAI_API_KEY"):
            env["OPENAI_API_KEY"] = os.getenv("AI_IDE_LLM_API_KEY", "")
        if not env.get("CHAT_MODEL"):
            env["CHAT_MODEL"] = os.getenv("CHAT_MODEL", "")
        # litellm needs provider prefix for non-OpenAI models
        model = env.get("CHAT_MODEL", "")
        if model and not model.startswith(
            ("openai/", "azure/", "anthropic/", "huggingface/")
        ):
            env["CHAT_MODEL"] = f"openai/{model}"
        env["REASONING_MODEL"] = env.get("CHAT_MODEL", "")
        env["CHAT_STREAM"] = "false"
        # 自定义窗口任务（launcher 注入 QM_MINING_WINDOW_*）：旧 QLIB_FACTOR_*
        # 变量与 QM_* 同源派生，任何下游读取方拿到的都是同一套窗口。
        custom = self._custom_windows()
        if custom is not None:
            from backend.services.engine.alpha_agent.mining_config import task_env

            # data_end：子进程再算一次末端保护（渲染进 conf 的 test 段/回测窗
            # 必须留出次日行情）。请求入口已退让过，此处幂等；兜的是手工改写
            # QM_MINING_WINDOW_* 的进程——env 里写出的端点 = 实际消费的端点。
            env.update(
                task_env(
                    custom[0],
                    custom[1],
                    datasets_from_env(),
                    data_end=qlib_backtest_data_end(market=self.market),
                )
            )
        else:
            # 无自定义窗口：保持旧静态兜底（手动运行/直跑脚本场景）
            env.setdefault(
                "QLIB_FACTOR_TRAIN_START",
                os.getenv("QLIB_FACTOR_TRAIN_START", "2016-01-01"),
            )
            # TRAIN_END 曾缺失 → FactorBasePropSetting 落到类默认 2014-12-31，
            # 与这里的 train_start 2016-01-01 组成倒置区间（train 段恒为空）。
            # 2020-12-31 与既有 valid（2021~2022）/test（2023~2025）首尾相接。
            env.setdefault(
                "QLIB_FACTOR_TRAIN_END",
                os.getenv("QLIB_FACTOR_TRAIN_END", "2020-12-31"),
            )
            env.setdefault(
                "QLIB_FACTOR_VALID_START",
                os.getenv("QLIB_FACTOR_VALID_START", "2021-01-01"),
            )
            env.setdefault(
                "QLIB_FACTOR_VALID_END",
                os.getenv("QLIB_FACTOR_VALID_END", "2022-12-31"),
            )
            env.setdefault(
                "QLIB_FACTOR_TEST_START",
                os.getenv("QLIB_FACTOR_TEST_START", "2023-01-01"),
            )
            env.setdefault(
                "QLIB_FACTOR_TEST_END", os.getenv("QLIB_FACTOR_TEST_END", "2025-12-31")
            )
        # mlflow 3.x 默认拒绝 filesystem tracking backend（"maintenance mode"），而
        # qlib 的 MLflowExpManager（qrun）与 read_exp_res.py 用的就是 ./mlruns 文件
        # 后端 → 不开此开关，qrun 在建实验时即抛 MlflowException 退出（2026-10-10
        # 实证：日志里 qrun 全部 exit 1，全盘 0 个 qlib_res.csv）。文件后端本身仍可用。
        env.setdefault("MLFLOW_ALLOW_FILE_STORE", "true")
        # 因子处理并行数
        env.setdefault("MULTI_PROC_N", os.getenv("MULTI_PROC_N", "4"))
        # 补齐 RD-Agent litellm 后端需要的 LITELLM_ 前缀变量（deepseek 优先）
        from backend.services.engine.rd_agent.llm_env import build_llm_env

        build_llm_env(env)
        return env

    # 需要注入中文/研究方向指令的 prompt key（RD-Agent prompts.yaml 中的顶层键）。
    # 这些键在 Scenario 构造时被读进 ``_background`` 并冻结——只放**静态**内容
    # （语言/方向/评估口径/池记忆）；动态内容（阶段块）挂阶段键，见下。
    _INJECT_TARGET_KEYS = ("qlib_factor_background", "qlib_quant_background")

    #: 阶段块注入目标：假设生成规范。该键在 ``QlibFactorHypothesisGen
    #: .prepare_context`` 里**每轮实时加载**——阶段块必须走这里；挂到背景键上
    #: 会永远停在「第 1 轮」（背景在任务开始时冻结）。
    _STAGE_TARGET_KEYS = ("factor_hypothesis_specification",)

    #: 因子池注入文件路径（launcher 在 spawn 前写好、经 env 传入）
    _ENV_POOL_CONTEXT = "QMF_POOL_CONTEXT_PATH"

    #: 经验记忆/禁区注入文件路径（T-FM-04；launcher 在 spawn 前原子写好、经 env 传入）
    _ENV_EXPERIENCE_CONTEXT = "QMF_EXPERIENCE_CONTEXT_PATH"

    def _build_prompt_suffix(self) -> str:
        """构造追加到因子背景 prompt 末尾的静态指令（读时一次，随背景冻结）。"""
        suffix = (
            "\n\n====== 语言要求 / Language Requirement ======\n"
            "所有因子的 description 字段必须使用中文撰写。"
            "hypothesis 和 reason 也请用中文。\n"
            "All factor descriptions MUST be written in Chinese (中文). "
            "Hypothesis and reason should also be in Chinese.\n"
        )
        direction = getattr(self, "_direction", "")
        if direction:
            suffix += (
                "\n\n====== 研究方向 / Research Direction ======\n"
                f"用户的研究方向/假设: {direction}\n"
                f"User's research direction/hypothesis: {direction}\n"
                "请围绕此方向进行因子探索。Focus factor exploration on this theme.\n"
            )
        suffix += render_eval_criteria_block()
        suffix += self._quantdb_prompt_block()
        suffix += self._minute_prompt_block()
        suffix += self._pool_context_block()
        suffix += self._experience_context_block()
        return suffix

    def _quantdb_prompt_block(self) -> str:
        """Give daily A-share mining precise on-demand QuantDB access rules."""
        if self.market != "a_share" or self._freq() != "day":
            return ""
        from backend.services.engine.rd_agent.mining_datasets import (
            DATASET_LABELS,
            datasets_from_env,
        )

        selected = datasets_from_env()
        selected_names = "、".join(DATASET_LABELS.get(key, key) for key in selected)
        financial_tables = {"balance", "income", "cashflow", "capital", "per_share", "pershare_index", "dividend_factors", "holder_num"}
        minute_sets = {"min1_kline", "min5_kline"}
        special_sets = financial_tables | minute_sets
        lines = [
            "\n\n====== QuantDB 数据源 / On-demand Data Access ======\n",
            f"本任务仅可使用已勾选的数据源：{selected_names}。",
            "数据经 quantdb_data SDK 按需读取；扩展数据不是 qd.panel() 的列。\n",
        ]
        if set(selected) - special_sets:
            lines.append(
                "日频分区数据使用 `qd.daily('<已勾选的数据集>', ['真实字段'], start=..., end=..., symbols=[...])`；"
                "先用 `qd.columns(dataset)` 核对字段。每次读取必须限定日期、列和股票。\n"
            )
        if set(selected) & {"index_daily", "etf_kline"}:
            lines.append(
                "指数与 ETF 数据的 instrument 代码不属于股票池；必须按日期对齐并广播到股票行后再参与因子计算，"
                "不能直接把这些行当成股票因子输出。\n"
            )
        if set(selected) & financial_tables:
            lines.append(
                "财务表使用 `qd.financial('<已勾选的财务表>', ['真实字段'], start=..., end=..., symbols=[...])`；"
                "默认按公告日向前对齐，禁止用报告期直接 join，避免前视偏差。\n"
            )
        if set(selected) & minute_sets:
            lines.append(
                "分钟线使用 `qd.minute('<已勾选的分钟数据集>', start=..., end=..., symbols=[...], freq='1D')`，"
                "必须限制股票与时间窗口。不要读取全量 tick。\n"
            )
        lines.append(
            "每个因子代码顶部必须声明真实来源，例如 `SOURCE_DATASETS = ['technical_indicators']`；"
            "只使用 qd.panel() 基础价量时声明空列表。"
        )
        return "".join(lines)

    def _minute_prompt_block(self) -> str:
        """分钟任务的静态数据面说明（不含它，LLM 会按日线语义写表达式）。

        日线任务返回空串——这段文字一旦误挂到日线任务上，LLM 会按分钟语义
        构造表达式（日线数据上算出来的是另一回事），是纯误导。
        """
        from backend.services.engine.alpha_agent.mining_config import FREQ_MINUTE

        if self._freq() != FREQ_MINUTE:
            return ""
        window = getattr(self, "_minute_window", None)
        cover = (
            f"{window[0].isoformat()} ~ {window[1].isoformat()}"
            if window
            else "见数据文件说明"
        )
        return (
            "\n\n====== 分钟级挖掘 / 1-Minute Frequency ======\n"
            "本任务在 **1 分钟 bar** 上做原生因子挖掘（不是日线！）：数据文件"
            "（任务目录 `panel.parquet`）里的索引是分钟时间戳（含 09:25 集合竞价"
            " bar），因子表达式与生成的因子代码都在分钟截面上计算。\n"
            f"本任务数据窗口：{cover}。\n"
            "取数：`import quantdb_data as qd; df = qd.panel()`（列带 `$` 前缀）。"
            "可用字段：$open / $high / $low / $close / $volume / $amount / $vwap / $factor"
            "（价格已按日复权因子调整，同一交易日内 $factor 恒定；$vwap = 成交均价）。"
            "结果写 `result.to_frame(名).to_parquet('result.parquet')`（H5 已退役，"
            "不要用 read_hdf/to_hdf）。\n"
            "时间序列算子在分钟 bar 上滚动（如 Ref($close, 1) = 上一根 1min bar，"
            "Mean($close, 20) = 最近 20 根 1min bar）；IC 口径为 1 分钟前向收益。\n"
            "注意：日线富化列（技术指标 / 估值 / 资金流 / L2 微观结构等）在分钟数据面**不存在**，"
            "引用会得到空列——请只用上面列出的分钟字段与它们的派生量构造因子。\n"
        )

    def _build_stage_block(self) -> str:
        """读时计算的阶段块（当前轮次：RD-Agent 日志 tag 优先，目录数兜底）。

        RD-Agent 每轮都会重新经 ``load_content`` 读 ``factor_hypothesis_
        specification``，此时的 ContextVar tag 形如 ``Loop_{li}.{step}``——
        据此精确判定「当前第几轮」；tag 不在（例如手工调用）才退化为数
        ``Loop_*`` 目录。任何异常由 patched_load 兜住，不拦提示词。
        """
        from .stage_prompt import current_round, rdagent_tag, render_stage_block

        cur = current_round(
            tag=rdagent_tag(), log_dir=getattr(self, "_task_log_dir", "")
        )
        return render_stage_block(
            total_loops=getattr(self, "_loop_n", None), current_loop=cur
        )

    def _pool_context_block(self) -> str:
        """历史挖掘记忆段（因子池 top-k 摘要，launcher 交付的文件）。

        只读文件、不做 DB：本函数运行在 RD-Agent 子进程内，子进程里复用主
        进程 DB engine 会跨事件循环炸（旧坑）；池查询全部在 launcher
        （engine 进程）完成，这里只消费交付物。文件缺失/为空 → 空串，
        注入是增益层，任何情况下不拦挖掘。
        """
        path = os.getenv(self._ENV_POOL_CONTEXT, "").strip()
        if not path:
            return ""
        try:
            content = Path(path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            logger.warning("[%s] 池注入文件读取失败(%s): %s", self.market, path, exc)
            return ""
        if not content:
            return ""
        return f"\n\n====== 历史挖掘记忆 / Past Mining Memory ======\n{content}\n"

    def _experience_context_block(self) -> str:
        """历史经验/禁区段（T-FM-04 拒因聚类 + 饱和方向，launcher 交付的文件）。

        与 ``_pool_context_block`` 同纪律：只读文件、不做 DB（本函数运行在
        RD-Agent 子进程内），文件缺失/为空 → 空串，注入失败绝不拦挖掘。
        """
        path = os.getenv(self._ENV_EXPERIENCE_CONTEXT, "").strip()
        if not path:
            return ""
        try:
            content = Path(path).read_text(encoding="utf-8").strip()
        except OSError as exc:
            logger.warning(
                "[%s] 经验记忆注入文件读取失败(%s): %s", self.market, path, exc
            )
            return ""
        if not content:
            return ""
        return f"\n\n====== 经验记忆 / Experience Memory ======\n{content}\n"

    def _patch_prompts_for_chinese(self):
        """注入中文/方向/评估口径（静态）与阶段化课程（动态）到 RD-Agent 提示词。

        RD-Agent 的 prompt 存于各包内的 prompts.yaml，经 `utils.agent.tpl.load_content`
        每次按需读取（无缓存），因此在该函数返回值上追加指令是唯一可靠的注入点 ——
        早期实现 import `...experiment.prompts` 模块，但该模块并不存在，注入从未生效。

        两类注入点（详见 ``stage_prompt`` 模块 docstring）：
        - 静态后缀（语言/方向/评估口径/池记忆）→ 背景键，随 ``_background``
          在 Scenario 构造时冻结；
        - 阶段块 → ``factor_hypothesis_specification``（每轮实时加载），
          读时按当前 loop 判定阶段（``_build_stage_block``）。
        """
        try:
            from rdagent.utils.agent import tpl as _tpl

            _tpl._qm_suffix = self._build_prompt_suffix()
            _tpl._qm_stage_fn = self._build_stage_block
            if getattr(_tpl, "_qm_patched", False):
                return

            original_load = _tpl.load_content
            suffix_keys = self._INJECT_TARGET_KEYS
            stage_keys = self._STAGE_TARGET_KEYS

            def patched_load(uri: str, *args, **kwargs):
                content = original_load(uri, *args, **kwargs)
                if not isinstance(content, str):
                    return content
                if any(uri.endswith(f":{k}") for k in suffix_keys):
                    if "语言要求" in content:
                        return content
                    return content + getattr(_tpl, "_qm_suffix", "")
                if any(uri.endswith(f":{k}") for k in stage_keys):
                    if STAGE_BLOCK_MARKER in content:
                        return content
                    stage_fn = getattr(_tpl, "_qm_stage_fn", None)
                    if stage_fn is None:
                        return content
                    try:
                        return content + stage_fn()
                    except Exception as exc:  # noqa: BLE001 — 阶段块失败不拦提示词
                        logger.warning(
                            "[%s] 阶段块渲染失败（本轮跳过阶段注入）: %s",
                            self.market,
                            exc,
                        )
                        return content
                return content

            _tpl.load_content = patched_load
            _tpl._qm_patched = True
            logger.info(
                "[%s] Patched RD-Agent prompt loader (Chinese + direction=%s + stages)",
                self.market,
                bool(getattr(self, "_direction", "")),
            )
        except Exception as e:
            logger.error("Failed to patch prompts for Chinese: %s", e)

    def _patch_file_desc(self):
        """让框架的数据目录描述支持 ``panel.parquet`` / ``.py``（取数 SDK）等文件类型。

        ``rdagent.scenarios.qlib.experiment.utils.get_data_folder_intro()`` 会遍历任务
        数据目录、对每个文件调用 ``get_file_desc()`` 生成注入 prompt 的说明；该函数只认
        ``.h5``/``.md``，遇到未知后缀直接 ``NotImplementedError`` 让 RDLoop 起不来。
        而本仓库 A 股数据面正是 ``panel.parquet`` + 取数 SDK ``quantdb_data.py``（见
        ``_ensure_panel_bundle``）——没有这一步，``FactorRDLoop`` 构造期即失败
        （``file type panel.parquet is not supported``）。

        与 superalpha 部署一致的 monkeypatch 扩展（不改 vendored 源码）：``panel.parquet``
        给出固定说明、``.py`` 注入其模块 docstring（SDK 的 docstring 本身就是给模型的用法
        与口径约定）、其余数据文件给通用说明；未知类型仍走原始实现。
        """
        try:
            from rdagent.scenarios.qlib.experiment import utils as _u

            if getattr(_u, "_qm_file_desc_patched", False):
                return
            orig = _u.get_file_desc

            def patched(p, variable_list=()):  # type: ignore[no-untyped-def]
                name = Path(p).name
                if name == "panel.parquet":
                    return (
                        "# panel.parquet\n\n## File Type\nParquet data file with an embedded "
                        "pandas MultiIndex `(datetime, instrument)` — daily A-share "
                        "price/volume panel (forward-adjusted OHLCV plus `$factor`).\n\n"
                        "## Usage\nDo NOT read it directly; use the "
                        "companion `quantdb_data` module in this folder:\n"
                        "```python\nimport quantdb_data as qd\ndf = qd.panel()  "
                        "# columns: $open $high $low $close $volume $amount $factor\n```\n"
                    )
                if name.endswith(".py"):
                    text = ""
                    try:
                        text = Path(p).read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        pass
                    doc = _extract_module_docstring(text) or "\n".join(
                        text.splitlines()[:60]
                    )
                    return (
                        f"# {name}\n\n## File Type\nPython helper module "
                        f"(in the same folder as `panel.parquet`; import it directly).\n\n"
                        f"## Usage & Conventions\n{doc}\n"
                    )
                if name.endswith((".parquet", ".json", ".csv")):
                    return (
                        f"# {name}\n\n## File Type\nUpper-case data file "
                        f"({name.rsplit('.', 1)[-1]}); read with pandas/duckdb.\n"
                    )
                return orig(p, variable_list)

            _u.get_file_desc = patched
            _u._qm_file_desc_patched = True
            logger.info(
                "[%s] Patched data-folder file description (panel.parquet / .py SDK)",
                self.market,
            )
        except Exception as exc:  # noqa: BLE001 - 补丁失败只告警，不影响主链路
            logger.warning("[%s] patch get_file_desc failed: %s", self.market, exc)

    def _create_loop(self):
        """创建 FactorRDLoop 实例"""
        from rdagent.app.qlib_rd_loop.conf import FactorBasePropSetting
        from rdagent.app.qlib_rd_loop.factor import FactorRDLoop
        from rdagent.core.utils import import_class

        # 注入中文指令
        self._patch_prompts_for_chinese()

        # qrun 前渲染 conf_*.yaml 的 Jinja 占位符：vendored RD-Agent 缺这一步，
        # 未渲染的配置是非法 YAML（详见 qlib_conf_template 模块 docstring）。
        install_qlib_execute_patch()

        # 数据目录描述支持 panel.parquet / .py（取数 SDK）：否则 Scenario 构造期
        # 即在 get_file_desc 抛 NotImplementedError，RDLoop 起不来。
        self._patch_file_desc()

        prop_setting_path = self.adapter.get_prop_setting_class()
        prop_cls = import_class(prop_setting_path)
        prop_setting = prop_cls()

        loop = FactorRDLoop(prop_setting)
        return loop

    async def run(
        self,
        loop_n: int = 3,
        task_log_dir: str = "",
        direction: str = "",
    ) -> dict[str, Any]:
        """执行因子挖掘循环

        Args:
            loop_n: 循环轮数
            task_log_dir: 日志输出目录
            direction: 挖掘方向/假设

        Returns:
            包含 factors 和 metadata 的结果字典
        """
        self._running = True
        self._cancelled = False
        self._direction = direction
        # 阶段块读时取用：总轮数进课程表进度，日志目录做 tag 缺失时的兜底
        self._loop_n = loop_n
        self._task_log_dir = task_log_dir

        try:
            # 配置环境变量
            env = self._configure_env(task_log_dir)
            for k, v in env.items():
                os.environ[k] = v

            # 确保 daily_pv.h5 数据文件可用
            self._ensure_data_file(task_log_dir)

            # RD-Agent workspace / 数据目录对齐：设绝对路径 env 变量 + 切 cwd。
            # RD-Agent 的 FACTOR_COSTEER_SETTINGS.data_folder 与 workspace_path 默认用
            # Path.cwd() 相对解析（import 时冻结），必须显式指到 task_log_dir，
            # 否则因子执行时找不到 daily_pv.h5（默认 workspace 在 /tmp/git_ignore_folder/...）。
            if task_log_dir:
                os.environ["WORKSPACE_PATH"] = task_log_dir
                os.environ["FACTOR_CoSTEER_data_folder"] = os.path.join(
                    task_log_dir,
                    "git_ignore_folder",
                    "factor_implementation_source_data",
                )
                os.environ["FACTOR_CoSTEER_data_folder_debug"] = os.path.join(
                    task_log_dir,
                    "git_ignore_folder",
                    "factor_implementation_source_data_debug",
                )
                os.chdir(task_log_dir)

            logger.info(
                "[%s] RDLoop starting: market=%s, loops=%d, log_dir=%s",
                self.market,
                self.adapter.market_name,
                loop_n,
                task_log_dir,
            )

            # 创建 RDLoop
            self._loop = self._create_loop()

            # 注入 base_features_path（L1/L2 因子集供 LLM 参考）
            base_features_path = getattr(self, "_base_features_path", None)
            if base_features_path:
                self._loop._init_base_features(base_features_path)
                logger.info(
                    "[%s] Loaded base features from %s", self.market, base_features_path
                )
            step_count = len(self._loop.steps)
            total_steps = loop_n * step_count

            logger.info(
                "[%s] Steps per loop: %d, total steps: %d",
                self.market,
                step_count,
                total_steps,
            )
            logger.info(
                "[%s] Step flow: %s",
                self.market,
                " → ".join(
                    getattr(s, "__name__", s.__class__.__name__)
                    for s in self._loop.steps
                ),
            )

            # 运行循环
            t0 = time.time()
            await self._loop.run(step_n=total_steps)
            elapsed = time.time() - t0

            logger.info("[%s] RDLoop completed in %.1fs", self.market, elapsed)

            # 提取结果
            factors = self._extract_factors(task_log_dir)

            return {
                "market": self.market,
                "market_name": self.adapter.market_name,
                "loop_n": loop_n,
                "elapsed_seconds": elapsed,
                "factors": factors,
                "total_factors": len(factors),
                "log_dir": task_log_dir,
                "success": True,
            }

        except asyncio.CancelledError:
            self._cancelled = True
            logger.info("[%s] RDLoop cancelled", self.market)
            return {
                "market": self.market,
                "cancelled": True,
                "factors": [],
                "success": False,
            }

        except Exception as e:
            logger.exception("[%s] RDLoop failed: %s", self.market, e)
            return {
                "market": self.market,
                "error": str(e),
                "factors": [],
                "success": False,
            }

        finally:
            self._running = False

    def cancel(self):
        """请求取消运行"""
        self._cancelled = True
        logger.info("[%s] Cancellation requested", self.market)

    def _ensure_panel_bundle(self, base_dir: str, quantdb_dir: str) -> None:
        """QuantDB 面板包下发（A 股数据面唯一形态）。

        在任务数据目录（``.../factor_implementation_source_data`` 及 ``..._debug``）
        内布设：

        - ``panel.parquet``：基础面板（7 列，经 ``panel_cache.ensure_panel_cache``
          构建/复用，硬链→软链→复制三级落盘）；
        - ``quantdb_data.py``：取数 SDK（因子代码 ``import quantdb_data as qd``）。

        面板构建失败记 warning 由调用方裁决（日线主链路在 ``_ensure_data_file``
        表现为任务失败——无数据面的挖掘没有意义）。
        """
        try:
            from backend.services.engine.data_platform.panel_cache import (
                ensure_panel_cache,
            )
        except Exception as exc:  # noqa: BLE001 - 面板模块缺失不拦挖掘
            logger.warning("[%s] 面板模块不可用，跳过面板下发: %s", self.market, exc)
            return
        try:
            panel, _ = ensure_panel_cache(quantdb_dir)
        except Exception as exc:  # noqa: BLE001 - 构建失败不拦挖掘
            logger.warning("[%s] 基础面板构建失败，跳过面板下发: %s", self.market, exc)
            return
        if panel is None or not panel.is_file():
            logger.warning("[%s] 基础面板不可用，跳过面板下发", self.market)
            return
        for leaf in (
            "git_ignore_folder/factor_implementation_source_data",
            "git_ignore_folder/factor_implementation_source_data_debug",
        ):
            data_dir = Path(base_dir) / leaf
            try:
                data_dir.mkdir(parents=True, exist_ok=True)
                self._link_panel(panel, data_dir / "panel.parquet")
                self._deploy_sdk(data_dir)
            except Exception as exc:  # noqa: BLE001 - 单目录失败不拦另一目录
                logger.warning("[%s] 面板下发失败(%s): %s", self.market, leaf, exc)
                continue
        logger.info("[%s] QuantDB 面板包就绪：%s", self.market, panel)

    @staticmethod
    def _deploy_sdk(data_dir: Path) -> None:
        """取数 SDK 下发到任务数据目录（软链→复制兜底）。"""
        sdk_src = Path(__file__).with_name("quantdb_data.py")
        dst_sdk = Path(data_dir) / "quantdb_data.py"
        try:
            if dst_sdk.exists() or dst_sdk.is_symlink():
                dst_sdk.unlink()
            os.symlink(sdk_src, dst_sdk)
        except OSError:
            shutil.copy2(sdk_src, dst_sdk)

    @staticmethod
    def _link_panel(panel: Path, target: Path) -> None:
        """硬链→软链→复制三级落面板。"""
        try:
            if target.exists() or target.is_symlink():
                target.unlink()
        except OSError:
            pass
        try:
            os.link(panel, target)
            return
        except OSError:
            pass
        try:
            os.symlink(panel, target)
            return
        except OSError:
            pass
        shutil.copy2(panel, target)

    def _ensure_data_file(self, task_log_dir: str = ""):
        """确保 daily_pv.h5 数据文件和 Qlib 数据目录在 RD-Agent 期望的位置可用

        RD-Agent 的 subprocess 以 task_log_dir 为 cwd 运行，
        FACTOR_COSTEER_SETTINGS.data_folder (git_ignore_folder/factor_implementation_source_data)
        相对于 cwd 解析。需要在 task_log_dir 下创建该目录并复制数据文件。

        同时确保对应的 Qlib provider_uri 目录可用。
        """
        import shutil

        # 根据市场选择数据源
        market_data_map = {
            "crypto": {
                "source_all": "/app/db/crypto_data/5min_pv.h5",
                "source_debug": "/app/db/crypto_data/5min_pv.h5",
                "qlib_source": "/app/db/qlib_data/crypto_data",
                "qlib_target_name": "crypto_data",
            },
            "hong_kong": {
                "source_all": "/app/db/hk_data/daily_pv.h5",
                "source_debug": "/app/db/hk_data/daily_pv.h5",
                "qlib_source": "/app/db/qlib_data/hk_data",
                "qlib_target_name": "hk_data",
            },
            "us_stock": {
                "source_all": "/app/db/us_data/daily_pv.h5",
                "source_debug": "/app/db/us_data/daily_pv.h5",
                "qlib_source": "/app/db/qlib_data/us_data",
                "qlib_target_name": "us_data",
            },
            "futures": {
                "source_all": "/app/db/futures_data/daily_pv.h5",
                "source_debug": "/app/db/futures_data/daily_pv.h5",
                "qlib_source": "/data/quantfutures/.qlib_cache/futures_data",
                "qlib_target_name": "futures_data",
            },
        }

        # 默认 A 股
        market_cfg = market_data_map.get(
            self.market,
            {
                "source_all": "/app/alphaagent/scenarios/qlib/experiment/factor_data_template/daily_pv_all.h5",
                "source_debug": "/app/alphaagent/scenarios/qlib/experiment/factor_data_template/daily_pv_debug.h5",
                "qlib_source": "/data/qlib/cn_data",
                "qlib_target_name": "cn_data",
            },
        )

        source_all = market_cfg["source_all"]
        source_debug = market_cfg["source_debug"]

        # base_dir: RD-Agent subprocess cwd (task log dir)
        base_dir = task_log_dir if task_log_dir else os.getcwd()

        # RD-Agent data folders (relative to subprocess cwd).
        # H5 已退役（A 股）：任务数据面文件为 panel.parquet；期货/其他市场
        # 沿用 daily_pv.h5（各自旧链路）。
        panel_all = os.path.join(
            base_dir, "git_ignore_folder/factor_implementation_source_data/panel.parquet"
        )
        panel_debug = os.path.join(
            base_dir,
            "git_ignore_folder/factor_implementation_source_data_debug/panel.parquet",
        )
        target_all = os.path.join(
            base_dir, "git_ignore_folder/factor_implementation_source_data/daily_pv.h5"
        )
        target_debug = os.path.join(
            base_dir,
            "git_ignore_folder/factor_implementation_source_data_debug/daily_pv.h5",
        )

        # 分钟原生挖掘（P1.5）：数据面换成 1min 面板（与 provider bins 同源）；
        # **不动** ~/.qlib 全局 symlink——分钟任务的 provider_uri 由 conf 渲染层
        # 直接改写指向分钟 provider，避免把并发日线任务共用的全局指向改掉。
        # 任何一环失败都显式抛，绝不静默退回日线数据。
        freq = self._freq()
        if self.market == "a_share" and freq != "day":
            # 日线富化列（技术指标/资金流/L2）在分钟数据面不存在：选择被路由层
            # 收敛为空；这里兜手工/env 直跑的进程，如实记一笔而不是让 LLM 以为
            # 这些列还在。**读原始 env 值**（不解析成 key）：本块只做"已忽略"
            # 告知，未登记 key 原样透出即可，不为一句日志炸掉整轮挖掘。
            raw_datasets = str(os.environ.get("QM_MINING_DATASETS") or "").strip()
            if raw_datasets:
                logger.warning(
                    "[%s] 分钟模式忽略数据集选择 %s（日线富化列不出现在 1min 数据面）",
                    self.market,
                    raw_datasets,
                )
            provider_dir = self._ensure_minute_provider()
            ok_all = self._generate_minute_panel(provider_dir, panel_all, debug=False)
            ok_debug = self._generate_minute_panel(
                provider_dir, panel_debug, debug=True
            )
            if not (ok_all and ok_debug):
                raise MinuteDataError(
                    f"分钟数据面生成失败（provider={provider_dir}，"
                    f"all={ok_all}, debug={ok_debug}）"
                )
            for _d in (
                Path(panel_all).parent,
                Path(panel_debug).parent,
            ):
                self._deploy_sdk(_d)
            # 日线富化列不在分钟数据面：不给 LLM 喂日线种子因子（引用即空值）
            self._base_features_path = None
            logger.info(
                "[%s] 分钟数据面就绪：%s（窗口 %s ~ %s）",
                self.market,
                provider_dir,
                *self._minute_window,
            )
            return

        # A 股（H5 已退役）：QuantDB 基础面板 + 取数 SDK 下发到任务数据目录。
        # 无 QuantDB 目录则明确失败——静默 fallback 到不存在的预生成文件只会
        # 让任务在更深处以更难懂的方式炸。
        if self.market == "a_share":
            quantdb_dir = self._resolve_quantdb_dir()
            if not quantdb_dir:
                raise RuntimeError(
                    "A 股数据面不可用：QuantDB 目录解析失败 "
                    "（QM_QUANTDB_DATA_DIR 未配置且 /data/quantdb 不存在）"
                )
            self._ensure_panel_bundle(base_dir, quantdb_dir)
        elif self.market == "futures":
            # 期货：从 QuantFutures parquet 生成（无预生成文件，fut_ 前缀 instrument）
            futures_dir = self._resolve_market_data_dir("/data/quantfutures")
            h5_generated = False
            if futures_dir.is_dir():
                ok_all = self._generate_futures_h5(futures_dir, target_all, debug=False)
                ok_debug = self._generate_futures_h5(
                    futures_dir, target_debug, debug=True
                )
                h5_generated = ok_all and ok_debug
            if not h5_generated:
                logger.warning(
                    "[%s] Futures h5 generation failed or QuantFutures dir missing, falling back to copy",
                    self.market,
                )
                self._copy_h5_source(source_all, target_all, source_debug, target_debug)
        else:
            self._copy_h5_source(source_all, target_all, source_debug, target_debug)

        # Ensure Qlib provider_uri data is available at ~/.qlib/qlib_data/<market>
        qlib_source = market_cfg["qlib_source"]
        qlib_target_name = market_cfg["qlib_target_name"]

        # parquet 单源市场：优先固定目录/各市场本地派生缓存（统一走 qlib_paths）
        market_cache = {
            "a_share": ("CN", ("/data/quantdb", ".qlib_cache", "cn_data")),
            "hong_kong": ("HK", ("/data/quanthk", ".qlib_cache", "hk_data")),
            "us_stock": ("US", ("/data/quantus", ".qlib_cache", "us_data")),
        }
        if self.market in market_cache:
            mkt_key, (base_dir_cfg, cache_sub, leaf) = market_cache[self.market]
            candidates = [
                Path(base_dir_cfg) / cache_sub / leaf,
                self._resolve_market_data_dir(base_dir_cfg) / cache_sub / leaf,
            ]
            try:
                from backend.shared.qlib_paths import resolve_qlib_provider_uri

                candidates.insert(0, Path(resolve_qlib_provider_uri(mkt_key)))
            except Exception:
                pass
            for candidate in candidates:
                if candidate.is_dir():
                    qlib_source = str(candidate)
                    break

        qlib_target = os.path.expanduser(f"~/.qlib/qlib_data/{qlib_target_name}")
        if os.path.isdir(qlib_source):
            # 修复过期 symlink：若指向错误路径则重建
            need_create = False
            if os.path.islink(qlib_target):
                current_target = os.readlink(qlib_target)
                if os.path.abspath(current_target) != os.path.abspath(qlib_source):
                    logger.info(
                        "[%s] Replacing stale symlink: %s -> %s (was %s)",
                        self.market,
                        qlib_target,
                        qlib_source,
                        current_target,
                    )
                    os.unlink(qlib_target)
                    need_create = True
            elif os.path.isdir(qlib_target):
                # 真实目录而非 symlink，rename 后建 symlink
                backup = qlib_target + "_backup"
                logger.info(
                    "[%s] Replacing real directory with symlink: %s -> %s (backup: %s)",
                    self.market,
                    qlib_target,
                    qlib_source,
                    backup,
                )
                try:
                    if os.path.exists(backup):
                        shutil.rmtree(backup)
                    os.rename(qlib_target, backup)
                    need_create = True
                except Exception as e:
                    logger.warning(
                        "[%s] Failed to replace directory with symlink: %s",
                        self.market,
                        e,
                    )
            else:
                need_create = True

            if need_create:
                try:
                    os.makedirs(os.path.dirname(qlib_target), exist_ok=True)
                    os.symlink(qlib_source, qlib_target)
                    logger.info(
                        "[%s] Created symlink: %s -> %s",
                        self.market,
                        qlib_target,
                        qlib_source,
                    )
                except Exception as e:
                    logger.warning(
                        "[%s] Failed to create Qlib symlink: %s", self.market, e
                    )

        # A 股：生成 base_factors.json 供 RD-Agent LLM 参考
        if self.market == "a_share" and hasattr(
            self.adapter, "generate_base_factors_json"
        ):
            data_dir = os.path.join(
                base_dir, "git_ignore_folder", "factor_implementation_source_data"
            )
            os.makedirs(data_dir, exist_ok=True)
            bf_path = self.adapter.generate_base_factors_json(data_dir)
            if bf_path:
                self._base_features_path = data_dir
                logger.info("[%s] base_factors.json ready at %s", self.market, data_dir)
            else:
                self._base_features_path = None
        else:
            self._base_features_path = None

    def _copy_h5_source(self, source_all, target_all, source_debug, target_debug):
        """从预生成的 h5 文件复制。"""
        for target, source in [(target_all, source_all), (target_debug, source_debug)]:
            if not os.path.exists(source):
                logger.warning(
                    "[%s] Source data file not found: %s", self.market, source
                )
                continue
            target_dir = os.path.dirname(target)
            os.makedirs(target_dir, exist_ok=True)
            if not os.path.exists(target) or os.path.getmtime(
                source
            ) > os.path.getmtime(target):
                try:
                    shutil.copy2(source, target)
                    logger.info(
                        "[%s] Copied data file: %s -> %s", self.market, source, target
                    )
                except Exception as e:
                    logger.warning(
                        "[%s] Failed to copy data file to %s: %s",
                        self.market,
                        target,
                        e,
                    )

    @staticmethod
    def _resolve_quantdb_dir() -> str | None:
        """解析 QuantDB 数据目录，返回存在的路径或 None。"""
        env_dir = os.getenv("QM_QUANTDB_DATA_DIR", "").strip()
        candidates = [env_dir] if env_dir else []
        candidates += ["/data/quantdb", "/app/data/quantdb"]
        for d in candidates:
            if d and os.path.isdir(d):
                return d
        return None

    def _custom_windows(self) -> tuple[Window, Window | None] | None:
        """任务自定义窗口（env ``QM_MINING_WINDOW_*``）→ (mining, validation)。

        未设 = ``None``（调用方走旧默认路径，行为零变化）。env 非法时记 error
        并退回 ``None``：请求入口（/evolve）已做同样校验，这里只是防手动运行
        的进程拿到半截/倒置配置后静默按错窗口挖。
        """
        try:
            from backend.services.engine.alpha_agent.mining_config import (
                window_from_env,
            )

            return window_from_env()
        except ValueError as exc:
            logger.error(
                "[%s] QM_MINING_WINDOW_* 配置非法，退回默认窗口: %s", self.market, exc
            )
            return None

    @staticmethod
    def _resolve_market_data_dir(container_path: str) -> Path:
        """解析市场本地 parquet 数据目录（容器挂载 /data/quantX）。

        支持 env 覆盖（QM_QUANTHK_DATA_DIR 等）；容器内为 /data/quantX，
        宿主机构回退到项目 data/quantX。返回不存在的候选也不报错，
        由调用方用 .is_dir() 判断。
        """
        env_map = {
            "/data/quanthk": "QM_QUANTHK_DATA_DIR",
            "/data/quantus": "QM_QUANTUS_DATA_DIR",
            "/data/quantbc": "QM_QUANTBC_DATA_DIR",
            "/data/quantdb": "QM_QUANTDB_DATA_DIR",
            "/data/quantfutures": "QM_QUANTFUTURES_DATA_DIR",
        }
        env_name = env_map.get(container_path)
        if env_name:
            env_val = os.getenv(env_name, "").strip()
            if env_val:
                return Path(env_val)
        project_data = (
            Path(__file__).resolve().parents[4] / "data" / Path(container_path).name
        )
        return project_data

    @staticmethod
    def _to_qlib_symbol(symbol: str) -> str:
        """suffix 格式 600036.SH -> Qlib 格式 sh600036。"""
        s = symbol.strip()
        if "." in s:
            code, exchange = s.split(".", 1)
            return f"{exchange.lower()}{code}"
        return s.lower()

    def _generate_futures_h5(
        self, futures_dir: Path | str, output_path: str, *, debug: bool = False
    ) -> bool:
        """从 QuantFutures parquet 生成 RD-Agent 的 daily_pv.h5。

        与 A 股版区别:
        - instrument 用 fut_ 前缀（对齐 QlibDataBuilder._MARKET_QLIB_PREFIX）
        - 期货无复权概念，$factor 恒为 1
        - 行情从 2016 年起量价才完整，起点对齐 Qlib 因子训练窗口
        """
        import numpy as np

        if os.path.exists(output_path):
            try:
                h5_mtime = os.path.getmtime(output_path)
                kline_dir = Path(futures_dir) / "1_kline_data" / "daily_forward"
                if kline_dir.is_dir():
                    partitions = sorted(
                        d for d in os.listdir(kline_dir) if d.startswith("dt=")
                    )
                    if partitions:
                        latest_parquet = kline_dir / partitions[-1] / "data.parquet"
                        if (
                            os.path.exists(latest_parquet)
                            and os.path.getmtime(latest_parquet) > h5_mtime
                        ):
                            logger.info(
                                "[%s] Parquet newer than h5, regenerating: %s",
                                self.market,
                                output_path,
                            )
                        else:
                            return True
                    else:
                        return True
                else:
                    return True
            except Exception:
                return True

        try:
            from backend.services.engine.data_platform.quantfutures_hub import (
                QuantFuturesDataHub,
            )

            hub = QuantFuturesDataHub(Path(futures_dir))
            if not hub.available:
                logger.error(
                    "[%s] QuantFuturesDataHub not available for h5 generation",
                    self.market,
                )
                return False

            # 期货无 instrument_detail parquet，从 daily_forward 分区推导 symbol 列表
            symbols: set[str] = set()
            kline_dir = Path(futures_dir) / "1_kline_data" / "daily_forward"
            if not kline_dir.is_dir():
                logger.error("[%s] QuantFutures daily_forward missing", self.market)
                return False
            import duckdb

            con = duckdb.connect(config={"memory_limit": "4GB", "threads": "2"})
            try:
                df_syms = con.execute(
                    f"SELECT DISTINCT symbol FROM read_parquet('{kline_dir / 'dt=*' / 'data.parquet'}', hive_partitioning=1)"
                ).fetchdf()
            finally:
                con.close()
            symbols = {str(s) for s in df_syms["symbol"].dropna().unique()}
            if not symbols:
                logger.error("[%s] No futures symbols found in parquet", self.market)
                return False

            symbol_list = sorted(symbols)
            if debug:
                symbol_list = symbol_list[:50]

            # 行情起点对齐 Qlib 因子训练窗口（2016 起量价完整）
            df = hub.fetch_daily_kline_batch(
                symbol_list, date(2016, 1, 1), date(2026, 12, 31), adjust="qfq"
            )
            if df is None or df.empty:
                logger.error("[%s] No futures K-line data read", self.market)
                return False

            # 剔除无效行情行（价格 <= 0 或非有限值会污染因子计算）
            cols = ["open", "high", "low", "close"]
            valid = np.isfinite(df[cols].to_numpy(dtype="float64")).all(axis=1) & (
                df[cols].to_numpy(dtype="float64") > 0
            ).all(axis=1)
            dropped = int((~valid).sum())
            if dropped:
                logger.warning(
                    "[%s] Dropped %d invalid futures rows", self.market, dropped
                )
                df = df.loc[valid].reset_index(drop=True)
            if df.empty:
                logger.error("[%s] No valid futures K-line rows", self.market)
                return False

            instruments = [f"fut_{s}" for s in df["symbol"]]
            combined = pd.DataFrame(
                {
                    "$open": df["open"].to_numpy(dtype="float64"),
                    "$high": df["high"].to_numpy(dtype="float64"),
                    "$low": df["low"].to_numpy(dtype="float64"),
                    "$close": df["close"].to_numpy(dtype="float64"),
                    "$volume": df["volume"].to_numpy(dtype="float64"),
                    "$factor": np.ones(len(df)),
                },
                index=pd.MultiIndex.from_arrays(
                    [pd.to_datetime(df["trade_date"]), instruments],
                    names=["datetime", "instrument"],
                ),
            )
            combined = combined.sort_index()

            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            combined.to_hdf(output_path, key="data", mode="w")
            logger.info(
                "[%s] Generated futures h5 from parquet: %s (%d rows, %d symbols)",
                self.market,
                output_path,
                len(combined),
                len(symbol_list),
            )
            return True

        except Exception as exc:
            logger.error(
                "[%s] Failed to generate futures h5 from parquet: %s", self.market, exc
            )
            return False

    @property
    def is_running(self) -> bool:
        return self._running

    def _extract_factors(self, log_dir: str) -> list[dict[str, Any]]:
        """从 RD-Agent 日志目录提取因子

        RDLoop 输出结构 (pickle):
        - experiment generation/**/*.pkl → 实验任务 (因子名 + 表达式)
        - coder result/**/*.pkl → 编码结果 (因子代码)
        - feedback/**/*.pkl → 反馈 (IC 等指标)
        """
        log_path = Path(log_dir)
        if not log_path.exists():
            logger.warning("[%s] Log dir not found: %s", self.market, log_dir)
            return []

        pkl_count = len(list(log_path.glob("**/*.pkl")))
        logger.info(
            "[%s] Scanning log dir: %s (%d pkl files)", self.market, log_dir, pkl_count
        )

        # 1. Factor metadata
        factor_meta: dict[str, dict] = {}
        for pkl_path in sorted(log_path.glob("**/experiment generation/**/*.pkl")):
            try:
                tasks = load_pickle(pkl_path)
                if not isinstance(tasks, list):
                    tasks = [tasks]
                for t in tasks:
                    name = getattr(t, "factor_name", None) or getattr(t, "name", None)
                    if not name:
                        continue
                    factor_meta[name] = {
                        "name": name,
                        "formulation": getattr(t, "factor_formulation", "") or "",
                        "description": getattr(t, "description", "") or "",
                        "category": getattr(t, "category", "") or "",
                    }
            except Exception as e:
                logger.debug("Failed to read %s: %s", pkl_path, e)

        # 2. Factor code —— 直接用 workspace.target_task.factor_name 映射，
        #    不再依赖函数名与因子名一致（LLM 函数命名可能与因子名不同）。
        factor_code: dict[str, str] = {}
        for pkl_path in sorted(log_path.glob("**/coder result/**/*.pkl")):
            try:
                workspaces = load_pickle(pkl_path)
                if not isinstance(workspaces, list):
                    workspaces = [workspaces]
                for ws in workspaces:
                    file_dict = getattr(ws, "file_dict", None) or {}
                    code = file_dict.get("factor.py", "")
                    if not code:
                        for v in file_dict.values():
                            if isinstance(v, str) and "def " in v:
                                code = v
                                break
                    if not code:
                        continue
                    name = ""
                    target = getattr(ws, "target_task", None)
                    if target is not None:
                        name = str(
                            getattr(target, "factor_name", None)
                            or getattr(target, "name", None)
                            or ""
                        ).strip()
                    if not name:
                        fn_match = re.search(r"def\s+(\w+)\s*\(", code)
                        name = (
                            fn_match.group(1).removeprefix("calculate_")
                            if fn_match
                            else f"factor_{len(factor_code)}"
                        )
                    factor_code[name] = code
            except Exception as e:
                logger.debug("Failed to read coder result %s: %s", pkl_path, e)

        # 3. Feedback
        feedback_text = ""
        for pkl_path in sorted(log_path.glob("**/feedback/**/*.pkl")):
            try:
                fb = load_pickle(pkl_path)
                if isinstance(fb, str):
                    feedback_text += fb + "\n"
                elif isinstance(fb, (list, tuple)):
                    for item in fb:
                        if isinstance(item, str):
                            feedback_text += item + "\n"
            except Exception as e:
                logger.debug("Failed to read feedback %s: %s", pkl_path, e)

        # 4. Merge —— 只保留**已完成 coding 阶段**的因子（有代码）。
        #    半成品（如 loop_n 预算截断时第二轮只有 experiment generation、无 coder result）
        #    不落库，避免因子库里出现无代码、无法回测的条目。
        factors: list[dict] = []
        for name, code in sorted(factor_code.items()):
            meta = factor_meta.get(name, {})
            factors.append(
                {
                    "name": meta.get("name", name),
                    "formulation": meta.get("formulation", ""),
                    "description": meta.get("description", ""),
                    "category": meta.get("category", ""),
                    "code": code,
                    "market": self.market,
                    "feedback": feedback_text[:5000] if feedback_text else "",
                }
            )

        missing = sorted(set(factor_meta.keys()) - set(factor_code.keys()))
        if missing:
            logger.info(
                "[%s] %d factors generated without code (partial/incomplete loop), skipped: %s",
                self.market,
                len(missing),
                missing,
            )

        logger.info("[%s] Extracted %d factors", self.market, len(factors))
        return factors


# ── Runner script entry point (subprocess) ──


def run_factor_mining_subprocess(
    market: str,
    task_id: str,
    user_id: str,
    loop_n: int,
    log_dir: str,
    direction: str = "",
) -> dict[str, Any]:
    """在子进程中执行因子挖掘（用于 launcher 调用）

    这是同步入口点，由 launcher 的 subprocess 调用。
    """
    wrapper = RDLoopWrapper(market=market)
    logger.info(
        "Starting factor mining: market=%s, task=%s, loops=%d", market, task_id, loop_n
    )
    result = asyncio.run(
        wrapper.run(
            loop_n=loop_n,
            task_log_dir=log_dir,
            direction=direction,
        )
    )
    result["task_id"] = task_id
    result["user_id"] = user_id
    return result
