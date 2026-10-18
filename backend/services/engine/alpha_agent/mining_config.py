"""挖掘时间窗配置（自定义挖掘区间 / 检验区间）——纯函数层。

移植 P1。两个窗口的用户语义：

- **挖掘数据区间**（mining）：喂给挖掘全链路的数据窗口（h5 数据面、逐因子
  IC 评估、qlib 实验分段）。
- **因子检验区间**（validation，可选）：**样本外**检验窗口，与挖掘区间
  **不得重叠**（起点须 ≥ 挖掘终点，两端均含；与 参考实现同口径）。

本模块只做纯计算与校验（无 IO、无 env 读取），供路由层校验、launcher 生成
子进程环境变量、前端默认值展示三方共用。真正的数据窗口落在子进程 env
（``task_env`` 产物），由 runner / rd_loop_wrapper / alphaagent 工作区分段
消费——单点定义、单点校验。

``QLIB_FACTOR_*`` 旧变量同步注入（数值一致）：迁移前它们是写死的（且下游
无人读取，属历史债务），现在与 ``QM_*`` 同源取值，任何读取方拿到的窗口都
一致，不再存在"两套窗口"。

**回测末端保护**（2026-10-10 实证）：窗口终点一律退让到「回测可用末端」=
数据末端的前一个交易日（``data_end``，由 ``rd_loop_wrapper
.qlib_backtest_data_end`` 从 qlib 日历算）。qlib 回测按 t+1 成交，终点压在
最后一根 bar 上必然 IndexError；而这个末端常常正是用户/前端预填的默认终点
（默认锚数据末端），不退让就是"默认配置必然跑挂"。退让结果落在窗口本身
（任务行、预览、env、IC 窗口同源），不是只藏在回测层里。
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta

from backend.services.engine.rd_agent.mining_datasets import (
    DATASET_LABELS,
    DATASET_REL_PATHS,
    SUPPORTED_DATASETS,
)

#: 默认窗口比例（参考实现口径）：总跨度 5 年 = 4 年挖掘 + 1 年检验。
DEFAULT_TOTAL_YEARS = 5
DEFAULT_MINING_YEARS = 4

#: 挖掘频率（移植 P1.5）：day = 日线（默认，全市场）；1min = 分钟原生
#: 挖掘（因子表达式在 1min bar 上取数、IC/回测落在 1min 前向收益；执行器仍按
#: day 频率跑，数据面走「day 层 + 1min 层」混合 provider）。
FREQ_DAY = "day"
FREQ_MINUTE = "1min"
SUPPORTED_FREQS: tuple[str, ...] = (FREQ_DAY, FREQ_MINUTE)
#: 分钟挖掘只对 A 股开放（只有 A 股建了 1min 叠加 provider）
MINUTE_MARKETS: tuple[str, ...] = ("a_share",)
#: 任务级频率 env 键（与窗口变量同族；缺省 = day）
ENV_FREQ = "QM_MINING_FREQ"
#: 任务指纹 env 键（P3 去重指纹 → 统计派稳定因子 id 的身份来源；空 = 退回任务级）
ENV_TASK_FINGERPRINT = "QM_MINING_TASK_FINGERPRINT"

#: 无检验区间时，挖掘区间按自然日切分为 train / valid / test 的比例。
_SEG_TRAIN_RATIO = 0.70
_SEG_VALID_RATIO = 0.85  # train 起点→70%，valid 70%→85%，test 85%→终点

_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@dataclass(frozen=True)
class Window:
    """闭区间日期窗口（起止均含）。"""

    start: date
    end: date

    def iso(self) -> tuple[str, str]:
        return self.start.isoformat(), self.end.isoformat()


def _parse_iso(value: str, label: str) -> date:
    text = str(value or "").strip()
    if not _ISO_RE.match(text):
        raise ValueError(f"{label}日期格式须为 YYYY-MM-DD（收到 {value!r}）")
    try:
        return date.fromisoformat(text)
    except ValueError as exc:  # 形如 2021-13-40
        raise ValueError(f"{label}日期非法：{value!r}") from exc


def normalize_freq(value: str | None, *, market: str | None = None) -> str:
    """频率串 → 规范值（空 = day）；非法值 / 市场不支持抛 ``ValueError``。

    ``market`` 给定时一并做市场闸：分钟挖掘只有 A 股有数据底座，其它市场显式
    报错（而不是静默按日线挖出一份"不是用户要的东西"）。
    """
    freq = str(value or "").strip().lower() or FREQ_DAY
    if freq not in SUPPORTED_FREQS:
        raise ValueError(
            f"不支持的挖掘频率 {value!r}（可选：{'、'.join(SUPPORTED_FREQS)}）"
        )
    if freq == FREQ_MINUTE and market is not None and str(market).lower() not in MINUTE_MARKETS:
        raise ValueError(f"分钟级挖掘暂只支持 A 股（收到市场 {market}）")
    return freq


def freq_from_env(env: Mapping[str, str] | None = None) -> str:
    """读任务环境里的挖掘频率（``QM_MINING_FREQ``）；未设置/空 = day。"""
    src: Mapping[str, str] = os.environ if env is None else env
    return normalize_freq(src.get(ENV_FREQ))


def task_fingerprint_from_env(env: Mapping[str, str] | None = None) -> str:
    """读任务环境里的 P3 去重指纹（``QM_MINING_TASK_FINGERPRINT``）。

    未设置/空白 = ``""``（调用方如实退回任务级身份，不拿空串硬凑一份"配置
    身份"——不同任务的空串会互相幂等覆盖，那是静默数据损坏）。
    """
    src: Mapping[str, str] = os.environ if env is None else env
    return str(src.get(ENV_TASK_FINGERPRINT) or "").strip()


def parse_window(start: str, end: str, *, label: str) -> Window:
    """解析一个必填窗口；格式/顺序错误抛 ``ValueError``（消息含 label）。"""
    s = _parse_iso(start, label)
    e = _parse_iso(end, label)
    if s > e:
        raise ValueError(
            f"{label}起始日期 {s.isoformat()} 晚于结束日期 {e.isoformat()}"
        )
    return Window(s, e)


def parse_optional_window(
    start: str | None, end: str | None, *, label: str
) -> Window | None:
    """解析可选窗口：两端都空 = 不启用（None）；只填一端 = 显式报错。"""
    s_empty = not str(start or "").strip()
    e_empty = not str(end or "").strip()
    if s_empty and e_empty:
        return None
    if s_empty or e_empty:
        raise ValueError(f"{label}须同时填写起止日期")
    return parse_window(str(start), str(end), label=label)


def validate_coverage(window: Window, coverage: Window | None, *, label: str) -> None:
    """窗口须落在数据覆盖内；``coverage=None``（数据源不可用）不拦。"""
    if coverage is None:
        return
    if window.start < coverage.start or window.end > coverage.end:
        raise ValueError(
            f"{label} {window.start.isoformat()} ~ {window.end.isoformat()}"
            f" 超出数据覆盖范围 {coverage.start.isoformat()} ~ {coverage.end.isoformat()}"
        )


def validate_windows(mining: Window, validation: Window | None) -> None:
    """检验区间不得与挖掘区间重叠（样本外要求的硬边界）。"""
    if validation is None:
        return
    if validation.start < mining.end:
        raise ValueError(
            f"因子检验区间起点 {validation.start.isoformat()} 早于挖掘区间终点"
            f" {mining.end.isoformat()}，样本外检验必须从挖掘结束之后开始"
        )


def clamp_to_data_end(window: Window, data_end: date | None) -> Window:
    """窗口终点不越过 ``data_end``（回测末端保护）；``None`` = 不拦，原样返回。

    ``data_end`` = **回测可用末端**（交易日，由 ``rd_loop_wrapper
    .qlib_backtest_data_end`` 按 qlib 日历与数据覆盖算），即数据末端**前一个**
    交易日——qlib 回测按 t+1 成交，``TradeCalendarManager.get_step_time``
    要取下一根 bar 的时间步（qlib/backtest/utils.py:131），回测终点压在最后一根
    bar 上必然 ``IndexError: index N is out of bounds``（2026-10-10 实证：
    检验终点 2026-10-09 = 日历末位 → 崩；前移一个交易日即通过）。所以**回测段
    的终点**必须留出次日行情。

    只动终点：起点与"检验窗起点 ≥ 挖掘窗终点"的非重叠约束不受影响。退让后若
    起点反而晚于终点，说明该窗整体落在保护范围之外——由调用方显式报错
    （:func:`_reject_beyond_guard`），绝不静默截成空窗/反向窗。
    """
    if data_end is None or window.end <= data_end:
        return window
    return Window(window.start, data_end)


def _reject_beyond_guard(window: Window | None, *, label: str, data_end: date | None) -> None:
    """末端保护退让后窗口反了 → 显式报错（消息给出可用的最晚终点）。"""
    if window is None or window.start <= window.end:
        return
    raise ValueError(
        f"{label}起点 {window.start.isoformat()} 晚于回测可用末端"
        f" {window.end.isoformat()}（数据末端前一个交易日：qlib 回测按 t+1 成交，"
        "终点须留出次日行情）；请把区间整体前移或把终点提前到该日"
    )


def resolve_windows(
    mining_start: str | None,
    mining_end: str | None,
    valid_start: str | None,
    valid_end: str | None,
    *,
    coverage: Window | None = None,
    data_end: date | None = None,
) -> tuple[Window | None, Window | None]:
    """四端点 → 已校验的 (挖掘窗, 检验窗)（解析/顺序/非重叠/覆盖/末端保护）。

    请求入口（路由）、配置预览与任务启动（launcher）共用这一处——「预览说
    能挖、提交却 400」只可能来自覆盖变化，不会来自两套口径。语义：

    - 挖掘窗两端都空 = 不指定 → 返回 ``(None, None)``（默认窗口路径）。
    - 只给检验窗 = 拒（没有挖掘段做对照，「样本外」无从谈起）。
    - ``coverage`` 为 None（数据源不可用）时跳过覆盖校验，不做假校验。
    - ``data_end``（回测可用末端）给定时把两个窗口终点一并退让到该日
      （见 :func:`clamp_to_data_end`）；**覆盖校验仍按用户原始输入做**，
      所以「终点写超覆盖」照旧报错，不会因为退让而放行。
    """
    mining = parse_optional_window(mining_start, mining_end, label="挖掘数据区间")
    validation = parse_optional_window(valid_start, valid_end, label="因子检验区间")
    if validation is not None and mining is None:
        raise ValueError("启用因子检验区间时必须同时指定挖掘数据区间")
    if mining is not None:
        validate_coverage(mining, coverage, label="挖掘数据区间")
    if validation is not None:
        validate_coverage(validation, coverage, label="因子检验区间")
    mining = None if mining is None else clamp_to_data_end(mining, data_end)
    validation = None if validation is None else clamp_to_data_end(validation, data_end)
    _reject_beyond_guard(mining, label="挖掘数据区间", data_end=data_end)
    _reject_beyond_guard(validation, label="因子检验区间", data_end=data_end)
    validate_windows(mining, validation)
    return mining, validation


def default_windows(
    coverage: Window | None = None,
    *,
    data_end: date | None = None,
    today: date | None = None,
) -> tuple[Window, Window]:
    """默认窗口：锚定数据覆盖末端（无覆盖锚今天）前推 5 年 = 4 年挖掘 + 1 年检验。

    覆盖期不足 5 年时整体前移到覆盖起点；4:1 切分只在有空间时成立——跨度
    ≤ 4 年时 ``split`` 被顶到覆盖末端，检验窗退化为与挖掘窗**共享末日**的
    单日窗，前端会提示用户显式调整。

    正常分支（跨度 > 4 年）挖掘窗与检验窗**不共享边界日**：检验从挖掘终点的
    **次日**起算。旧实现两窗共用 split 日，那一天的前向收益在样本内、样本外
    各被算了一次——「样本外」名不副实。退化分支（跨度 ≤ 4 年）没有非重叠
    检验窗可放，如实保留共享末日的行为（**是共享，不是「相接」**——相接指
    ``[.., d]`` 与 ``[d+1, ..]``，那是正常分支）；:func:`validate_windows`
    两分支都按 ``valid.start >= mining.end`` 放行，统计派报告以
    ``validation_overlaps_mining`` 如实标 True。

    ``data_end``（回测可用末端）给定且早于覆盖末端时锚它——默认值本身就落在
    可回测范围内，前端预填的默认窗口不因末端保护被改（否则"预填范围 vs 实跑
    范围"差一天，用户看到的和跑的不是一回事）。
    """
    anchor_end = coverage.end if coverage is not None else (today or date.today())
    if data_end is not None:
        anchor_end = min(anchor_end, data_end)
    earliest = coverage.start if coverage is not None else None

    def _shift_years(d: date, years: int) -> date:
        """按年平移（正数向后、负数向前）；2/29 下落 28 日。"""
        try:
            return d.replace(year=d.year + years)
        except ValueError:  # 2/29 在平年不存在
            return d.replace(year=d.year + years, day=28)

    ideal_start = _shift_years(anchor_end, -DEFAULT_TOTAL_YEARS)
    start = ideal_start if earliest is None else max(ideal_start, earliest)
    split = _shift_years(start, DEFAULT_MINING_YEARS)
    if split < start:
        split = start
    if split > anchor_end:
        split = anchor_end
    return Window(start, split), Window(
        min(split + timedelta(days=1), anchor_end), anchor_end
    )


def qlib_segments(
    mining: Window, validation: Window | None
) -> dict[str, tuple[str, str]]:
    """派生 qlib 实验的 train/valid/test 分段（闭区间，边界日期共享）。

    有检验区间 → test = 检验区间，train/valid 在挖掘区间内 70% 处切分；
    无 → 挖掘区间按 70% / 85% 切成三段。切分按**自然日**（交易日历在此层
    不可得，段边界不做调仓对齐；qlib 在交易日粒度上取交集，不影响正确性）。
    """
    span = (mining.end - mining.start).days

    def _cut(ratio: float) -> date:
        return mining.start + timedelta(days=round(span * ratio))

    if validation is not None:
        cut = _cut(_SEG_TRAIN_RATIO)
        vs, ve = validation.iso()
        return {
            "train": (mining.start.isoformat(), cut.isoformat()),
            "valid": (cut.isoformat(), mining.end.isoformat()),
            "test": (vs, ve),
        }
    c1, c2 = _cut(_SEG_TRAIN_RATIO), _cut(_SEG_VALID_RATIO)
    return {
        "train": (mining.start.isoformat(), c1.isoformat()),
        "valid": (c1.isoformat(), c2.isoformat()),
        "test": (c2.isoformat(), mining.end.isoformat()),
    }


def handler_span(mining: Window, validation: Window | None) -> tuple[str, str]:
    """qlib data_handler 的取数跨度：挖掘起点 → 检验终点（含检验段数据）。"""
    end = validation.end if validation is not None else mining.end
    return mining.start.isoformat(), end.isoformat()


def task_env(
    mining: Window,
    validation: Window | None,
    datasets: tuple[str, ...] | list[str],
    *,
    coverage: Window | None = None,
    data_end: date | None = None,
    freq: str | None = None,
) -> dict[str, str]:
    """生成挖掘子进程的任务级环境变量（空值键一律不发）。

    ``QM_*`` 是本功能的主契约；``QLIB_FACTOR_*`` 为旧链路变量，取同一份
    分段结果同步注入（值一致，消除"两套窗口"）。``coverage`` 仅用于让调用方
    在生成前复验，不进入 env。

    ``data_end``（回测可用末端）在此**再退让一次**：正常路径（路由/launcher）
    传来的窗口已经退让过，这里等价幂等；它的价值是兜住手工驱动（直改
    ``QM_MINING_WINDOW_*`` 或直跑脚本）——env 里写出的端点永远是**实际会被
    消费**的那一份，qrun 不会因终点压线而崩在回测尾巴上。
    """
    validate_coverage(mining, coverage, label="挖掘数据区间")
    if validation is not None:
        validate_coverage(validation, coverage, label="因子检验区间")
    mining = clamp_to_data_end(mining, data_end)
    validation = None if validation is None else clamp_to_data_end(validation, data_end)
    _reject_beyond_guard(mining, label="挖掘数据区间", data_end=data_end)
    _reject_beyond_guard(validation, label="因子检验区间", data_end=data_end)
    validate_windows(mining, validation)

    seg = qlib_segments(mining, validation)
    m_start, m_end = mining.iso()
    env: dict[str, str] = {
        "QM_MINING_WINDOW_START": m_start,
        "QM_MINING_WINDOW_END": m_end,
        "QM_MINING_DATASETS": ",".join(datasets),
        "QM_QLIB_TRAIN_START": seg["train"][0],
        "QM_QLIB_TRAIN_END": seg["train"][1],
        "QM_QLIB_VALID_START": seg["valid"][0],
        "QM_QLIB_VALID_END": seg["valid"][1],
        "QM_QLIB_TEST_START": seg["test"][0],
        "QM_QLIB_TEST_END": seg["test"][1],
        # 旧链路变量：与分段同源（下游若读取，窗口一致）。六个端点缺一不可——
        # 少一个（如曾经的 TRAIN_END）会落到 FactorBasePropSetting 的类默认
        # （2014-12-31），与自定义的 train_start 组成倒置区间，train 段恒为空。
        "QLIB_FACTOR_TRAIN_START": seg["train"][0],
        "QLIB_FACTOR_TRAIN_END": seg["train"][1],
        "QLIB_FACTOR_VALID_START": seg["valid"][0],
        "QLIB_FACTOR_VALID_END": seg["valid"][1],
        "QLIB_FACTOR_TEST_START": seg["test"][0],
        "QLIB_FACTOR_TEST_END": seg["test"][1],
    }
    if validation is not None:
        v_start, v_end = validation.iso()
        env["QM_VALIDATION_WINDOW_START"] = v_start
        env["QM_VALIDATION_WINDOW_END"] = v_end
    if freq is not None:
        env[ENV_FREQ] = normalize_freq(freq)
    return {k: v for k, v in env.items() if v}


def window_from_env(
    env: Mapping[str, str] | None = None,
) -> tuple[Window, Window | None] | None:
    """读任务环境里的自定义窗口（``QM_MINING_WINDOW_*`` / ``QM_VALIDATION_*``）。

    未设置返回 ``None``（调用方保持默认行为——默认调用路径零变化）；只设了
    一半或区间非法抛 ``ValueError``。与 ``task_env`` 是同一套键的读/写两端：
    写入方（launcher）已在请求入口校验，这里再验一次是防手动运行的进程
    （或换了 env 的容器）拿到半截/倒置配置后静默按错误窗口挖。
    """
    src: Mapping[str, str] = os.environ if env is None else env
    start = str(src.get("QM_MINING_WINDOW_START") or "").strip()
    end = str(src.get("QM_MINING_WINDOW_END") or "").strip()
    if not start and not end:
        return None
    mining = parse_window(start, end, label="挖掘数据区间")
    validation = parse_optional_window(
        src.get("QM_VALIDATION_WINDOW_START"),
        src.get("QM_VALIDATION_WINDOW_END"),
        label="因子检验区间",
    )
    validate_windows(mining, validation)
    return mining, validation


def dataset_catalog() -> list[dict[str, str]]:
    """前端目录：优先列出 QuantDB 当前真实存在的可用数据集。"""
    available = set(SUPPORTED_DATASETS)
    try:
        from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

        hub = QuantDBDataHub.get_instance()
        if hub.available:
            available = {
                key for key, rel_path in DATASET_REL_PATHS.items()
                if (hub.data_dir / rel_path).is_dir()
                and any((hub.data_dir / rel_path).iterdir())
            }
    except Exception:  # noqa: BLE001 - keep the known catalog if discovery is unavailable
        pass
    return [
        {"key": key, "label": DATASET_LABELS.get(key, key)}
        for key in SUPPORTED_DATASETS
        if key in available
    ]
