"""分钟（1min）Qlib provider 构建器 —— 分钟级因子挖掘的数据底座。

**为什么是「混合叠加 provider」**（2026-10-10 实证，非猜测）：

qlib 的 qrun 流水线里，执行器 / 撮合 / 基准取数默认按 **day** 频率
（``PortAnaRecord`` 默认 ``time_per_step: "day"``，``Exchange`` 按 ``freq="day"``
取基准行情），而因子表达式的取数频率由 **loader 自身的 ``freq``** 决定
（``DataHandler.__init__`` 不吃 freq——实测在 handler 层传 freq 直接
``TypeError: unexpected keyword argument 'freq'``；把 ``freq: 1min`` 放进
``Alpha158DL`` 的 kwargs 才生效）。因此分钟挖掘的正确形态是**两层叠加**：

1. **day 层**（从日线 provider 原样复制）：``calendars/day.txt`` + 各标的
   ``*.day.bin`` —— 给执行器 / 撮合 / 基准（``sh000300``）用；
2. **1min 层**（本模块构建）：``calendars/1min.txt`` + 各标的 ``*.1min.bin``
   —— 给因子表达式（loader ``freq: 1min``）用。

日线日历天然延伸到分钟覆盖末端之后（日线 provider 每天在长），这正好提供
qlib 回测 t+1 时间步所需的次日行情缓冲——实测把 test_end 压在分钟数据最后
一天（2026-08-25）时 qrun 正常退出（returncode 0，``report_normal_1day.pkl``
50 行），不需要给 read_exp_res.py 打补丁。

**口径**（与日线 provider 对齐）：

- 价格用日线 provider 的逐日 step factor 复权（``close_bin = raw × factor``）：
  同一交易日内 factor 恒定 → 日内收益不变，跨夜除权跳空被抹平；「15:00 收盘
  bar × factor == day close_bin」是端到端验证里实际对拍过的恒等式；
- ``$volume`` / ``$amount`` **原样保留**（股数 / 金额不做复权），``$amount``
  单位与源 parquet 一致；
- ``$vwap`` = ``amount × scale / volume``，scale 按文件自动测定（量价反推成交
  均价，对 10 的幂吸附，残差超 2 倍即记 warning）。日线层没有 vwap 字段
  （既有陷阱：日线表达式引用 ``$vwap`` 静默得 NaN），分钟层补上；
- 时间戳原样取自源 parquet（含 09:25 集合竞价 bar），**不造 bar、不补缺**；
  全市场日历 = 所选标的 time 列的并集，缺 bar 的标的按 NaN 对齐（qlib bin 的
  index 语义要求数组长度 = ``last_idx - start_idx + 1``，空洞必须是 NaN）。

**内存**：两遍扫描——第一遍只读各文件 time 列建全市场日历，第二遍逐标的
读全列、写 bin、即弃（峰值 = 单标的数据 + 日历，不随池子大小线性涨）。

**原子性**：构建在目标目录旁的临时目录完成，再整体改名换入（同文件系统
rename）；并发构建由 ``<provider>.lock`` 排他文件锁串行化。任何一步失败都
不触碰已有 provider——正在跑的挖掘任务读到的永远是完整的一份。

**目录**（可用 env 覆盖，测试全部走显式参数、不碰 /data）：

- provider 产物：``QM_QLIB_MINUTE_DIR``（默认 ``/data/qlib/cn_data_1min``）
- 分钟源：``QM_MINUTE_KLINE_DIR``（默认 ``<quantdb>/1_kline_data/min1_kline``）
- day 层来源：``QM_MINUTE_DAY_PROVIDER``（默认走 ``shared.qlib_paths`` 解析）
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import shutil
import struct
import tempfile
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

#: 分钟频率串（qlib 口径；``calendars/<freq>.txt`` 与 ``<field>.<freq>.bin`` 同名）
MINUTE_FREQ = "1min"
#: 日线频率串（day 层复制用）
DAY_FREQ = "day"

#: 1min 层写入的 field 表（qlib bin 文件名 = ``<field>.<freq>.bin``）
MINUTE_FIELDS: tuple[str, ...] = (
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "vwap",
    "factor",
)

#: 源 parquet 需要读的列（volinstock / forwardfactor 不参与构建：
#: forwardfactor 实测恒为 1.0，复权一律以日线 provider 的 step factor 为准）
_SOURCE_COLUMNS: tuple[str, ...] = (
    "time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
)

#: 默认股票池文件名（qlib instruments/<name>.txt）
DEFAULT_UNIVERSE = "csi300"
#: 基准（其行情只有日线，day 层必须复制进分钟 provider）
BENCHMARK_SYMBOLS: tuple[str, ...] = ("sh000300",)
#: 源 parquet 金额单位缺省假设（QuantDB 口径：万元；实测 implied scale 中位数
#: 10000.0005、p10/p90 偏差 <0.1%）
DEFAULT_AMOUNT_SCALE = 1e4
#: 自动测定的 scale 残差超过该量级（log10）即记 warning：单位口径可疑
_SCALE_STEP_TOLERANCE = 0.5

#: 相邻交易日相隔超过该**自然日**数即判数据空洞（而非节假日连休）：A 股最长连休
#: 约 10 个自然日（春节/国庆），14 天既留余量，又能把真实断层
#: （实测 2026-01-06 → 2026-03-03，58 天）与节假日区分开。
MAX_GAP_DAYS = 14

#: 构建语义版本：**改动构建口径（复权/字段/区间/对齐规则）时必须 +1**。
#: 它进源指纹 → 已构建的旧产物自动判 stale、ensure 重建，不会出现"代码改了、
#: 盘上还是旧口径"的静默分裂（版本 2：instruments 末端留 t+1 缓冲日）。
_META_VERSION = 2

_ENV_PROVIDER_DIR = "QM_QLIB_MINUTE_DIR"
_ENV_SOURCE_DIR = "QM_MINUTE_KLINE_DIR"
_ENV_DAY_PROVIDER = "QM_MINUTE_DAY_PROVIDER"

_DEFAULT_PROVIDER_DIR = "/data/qlib/cn_data_1min"
_DEFAULT_SOURCE_REL = ("1_kline_data", "min1_kline")


class MinuteProviderError(RuntimeError):
    """分钟 provider 解析 / 构建 / 校验失败（消息面向运维，直接上日志/接口）。"""


@dataclass(frozen=True)
class MinuteCoverage:
    """分钟数据覆盖事实（源扫描或 meta 读取的产物）。

    ``bars`` 是**日历 bar 数**（全体标的去重后的时间戳个数），不是某只标的的
    行数；``days`` 是去重交易日数——前端用它展示「覆盖 121 个交易日」这类事实。
    """

    start: date
    end: date
    days: int
    bars: int
    symbols: int
    built_at: str = ""
    fingerprint: str = ""


@dataclass(frozen=True)
class MinuteDataFacts:
    """分钟数据**可用**事实（meta 覆盖 + 1min 日历现算；路由/包装层共用一份口径）。

    ``coverage`` 是构建产物的事实（起止/天数/bar 数）；``usable_start`` 是**末端
    连续块**起点——挖掘窗口只能落在 ``[usable_start, coverage.end]`` 内，跨空洞的
    窗口会悄悄少掉中间整段（实测 2026-01-06 → 2026-03-03 有 58 天断层），正是要
    拦的失败态。``gaps`` = 空洞表 ``(前段末日, 后段首日)``，供前端如实展示缺了哪段。
    """

    coverage: MinuteCoverage
    days: tuple[date, ...]
    usable_start: date
    gaps: tuple[tuple[date, date], ...]

    @property
    def usable_days(self) -> int:
        """末端连续块内的交易日数（前端展示"可用 N 个交易日"）。"""
        return sum(1 for d in self.days if d >= self.usable_start)

    def contains(self, start: date, end: date) -> bool:
        """窗口是否整个落在可用连续块内（交易日闭区间，含两端）。"""
        return self.usable_start <= start <= end <= self.coverage.end

    def gap_text(self, *, limit: int = 3) -> str:
        """空洞摘要（错误消息用）：``2026-01-06 ~ 2026-03-04``，多段顿号连接。"""
        if not self.gaps:
            return ""
        head = "、".join(
            f"{a.isoformat()} ~ {b.isoformat()}" for a, b in self.gaps[:limit]
        )
        return head if len(self.gaps) <= limit else f"{head}、…（共 {len(self.gaps)} 段）"


@dataclass(frozen=True)
class MinuteSourceScan:
    """源 parquet 扫描结果（未构建也能回答「有哪些分钟数据」）。"""

    coverage: MinuteCoverage
    symbols_found: tuple[str, ...]
    symbols_missing: tuple[str, ...]
    per_symbol_first: dict[str, date] = field(default_factory=dict)
    per_symbol_last: dict[str, date] = field(default_factory=dict)


@dataclass(frozen=True)
class BuildReport:
    """一次构建的结果（写进 meta.json 的同一份事实）。"""

    provider_dir: Path
    universe: str
    symbols: int
    symbols_missing: tuple[str, ...]
    bars: int
    days: int
    first: date
    last: date
    fingerprint: str
    elapsed_seconds: float
    warnings: tuple[str, ...]


# ── 路径解析 ─────────────────────────────────────────────────────────


def resolve_minute_provider_dir(provider_dir: str | os.PathLike[str] | None = None) -> Path:
    """分钟 provider 目录：显式参数 > ``QM_QLIB_MINUTE_DIR`` > 默认。"""
    if provider_dir is not None:
        return Path(provider_dir)
    env_val = os.getenv(_ENV_PROVIDER_DIR, "").strip()
    return Path(env_val or _DEFAULT_PROVIDER_DIR)


def resolve_minute_source_dir(source_dir: str | os.PathLike[str] | None = None) -> Path:
    """分钟源 parquet 目录：显式参数 > ``QM_MINUTE_KLINE_DIR`` > ``<quantdb>/1_kline_data/min1_kline``。

    quantdb 根按 ``QM_QUANTDB_DATA_DIR`` → ``/data/quantdb`` → ``/app/data/quantdb``
    顺序解析（与 ``rd_loop_wrapper._resolve_quantdb_dir`` 同一优先级；不 import
    它是为了避免模块间循环导入）。
    """
    if source_dir is not None:
        return Path(source_dir)
    env_val = os.getenv(_ENV_SOURCE_DIR, "").strip()
    if env_val:
        return Path(env_val)
    for cand in (
        os.getenv("QM_QUANTDB_DATA_DIR", "").strip(),
        "/data/quantdb",
        "/app/data/quantdb",
    ):
        if cand and Path(cand).is_dir():
            return Path(cand).joinpath(*_DEFAULT_SOURCE_REL)
    return Path("/data/quantdb").joinpath(*_DEFAULT_SOURCE_REL)


def resolve_day_provider_dir(day_provider_dir: str | os.PathLike[str] | None = None) -> Path:
    """day 层来源目录（日线 qlib provider）：显式参数 > ``QM_MINUTE_DAY_PROVIDER``
    > ``shared.qlib_paths`` 解析出的 A 股目录。"""
    if day_provider_dir is not None:
        return Path(day_provider_dir)
    env_val = os.getenv(_ENV_DAY_PROVIDER, "").strip()
    if env_val:
        return Path(env_val)
    from backend.shared.qlib_paths import resolve_qlib_provider_uri

    return Path(resolve_qlib_provider_uri("CN"))


# ── 就绪 / meta ──────────────────────────────────────────────────────


def is_minute_provider_ready(provider_dir: str | os.PathLike[str]) -> bool:
    """分钟 provider 是否具备最小可用布局（含 day 层与 meta）。

    缺 day 层（``calendars/day.txt``）的目录**不算就绪**：qrun 的执行器 /
    撮合 / 基准全在 day 频，缺了它只会在回测阶段抛
    ``does not contain data for day`` 这类误导性错误。
    """
    provider = Path(provider_dir)
    return (
        provider.is_dir()
        and (provider / "calendars" / f"{MINUTE_FREQ}.txt").is_file()
        and (provider / "calendars" / f"{DAY_FREQ}.txt").is_file()
        and (provider / "instruments").is_dir()
        and (provider / "features").is_dir()
        and (provider / "meta.json").is_file()
    )


def read_minute_meta(provider_dir: str | os.PathLike[str]) -> dict | None:
    """读 ``meta.json``（缺失 / 损坏返回 None，绝不抛——调用方按未构建处理）。"""
    path = Path(provider_dir) / "meta.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def minute_coverage(provider_dir: str | os.PathLike[str]) -> MinuteCoverage | None:
    """已构建 provider 的覆盖事实（读 meta；未构建返回 None）。"""
    meta = read_minute_meta(provider_dir)
    if not meta:
        return None
    try:
        return MinuteCoverage(
            start=date.fromisoformat(str(meta["first"])),
            end=date.fromisoformat(str(meta["last"])),
            days=int(meta["days"]),
            bars=int(meta["bars"]),
            symbols=int(meta["symbols"]),
            built_at=str(meta.get("built_at", "")),
            fingerprint=str(meta.get("fingerprint", "")),
        )
    except (KeyError, TypeError, ValueError) as exc:
        logger.warning("[minute] meta.json 字段不完整: %s（%s）", provider_dir, exc)
        return None


def calendar_gaps(
    days: Sequence[date], *, max_gap_days: int = MAX_GAP_DAYS
) -> tuple[tuple[date, date], ...]:
    """交易日序列里的**数据空洞**：``(前段末日, 后段首日)`` 升序元组（纯函数）。

    间隔**超过** ``max_gap_days`` 才判空洞（节假日连休走同一阈值，不单独建表：
    阈值 14 天已把 A 股最长连休 ~10 天与真实断层分开）。输入重复/乱序先规范化。
    """
    ordered = sorted(set(days))
    return tuple(
        (prev, cur)
        for prev, cur in zip(ordered, ordered[1:])
        if (cur - prev).days > max_gap_days
    )


def terminal_usable_start(
    days: Sequence[date], *, max_gap_days: int = MAX_GAP_DAYS
) -> date | None:
    """末端连续块起点：最后一个空洞之后的首个交易日；无空洞即首个交易日。

    取**末端**块的理由：分钟挖掘窗口的终点被回测 t+1 闸钉在数据末端，窗口只能向
    前延伸——能连续延伸多远，就是可用的挖掘区间。空序列回 ``None``。
    """
    ordered = sorted(set(days))
    if not ordered:
        return None
    gaps = calendar_gaps(ordered, max_gap_days=max_gap_days)
    return ordered[0] if not gaps else gaps[-1][1]


def _calendar_days(provider: Path) -> tuple[date, ...]:
    """读 ``calendars/1min.txt`` → 去重升序交易日元组（读不了抛 MinuteProviderError）。"""
    cal = _read_minute_calendar(provider)
    return tuple(_to_date(d) for d in np.unique(cal.astype("datetime64[D]")))


def minute_data_facts(provider_dir: str | os.PathLike[str]) -> MinuteDataFacts | None:
    """分钟可用事实单一入口（未构建回 ``None``，provider 损坏则响亮抛）。

    路由层的窗口校验/默认窗口与包装层的兜底校验都必须走这里——两边各自解释
    "哪些天有数据"必然分叉。日历与 meta 都读不到才回 ``None``；只坏日历（meta
    在）属于 provider 损坏，抛 :class:`MinuteProviderError` 由调用方上抛。
    """
    provider = Path(provider_dir)
    coverage = minute_coverage(provider)
    if coverage is None:
        return None
    days = _calendar_days(provider)
    if not days:
        raise MinuteProviderError(f"1min 日历为空：{provider}")
    usable = terminal_usable_start(days)
    assert usable is not None  # days 非空 → 必有值
    return MinuteDataFacts(
        coverage=coverage,
        days=days,
        usable_start=usable,
        gaps=calendar_gaps(days),
    )


# ── 源扫描 / 股票池 ──────────────────────────────────────────────────


def resolve_universe(
    day_provider_dir: str | os.PathLike[str], universe: str = DEFAULT_UNIVERSE
) -> list[str]:
    """读日线 provider 的 ``instruments/<universe>.txt`` → qlib 代码表（小写）。"""
    path = Path(day_provider_dir) / "instruments" / f"{universe}.txt"
    if not path.is_file():
        raise MinuteProviderError(
            f"股票池文件不存在：{path}（day 层 provider 未就绪或池名有误）"
        )
    codes: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text:
            continue
        codes.append(text.split("\t")[0].strip().lower())
    return codes


def _suffix_file(source_dir: Path, qlib_code: str) -> Path:
    """qlib 代码（``sh600000``）→ 源 parquet 文件名（``600000.SH.parquet``）。

    统一走 ``StockCodeUtil.to_suffix``（仓库单源：parquet/qlib 层后缀式），
    不在这里手写市场前缀映射。
    """
    from backend.shared.stock_utils import StockCodeUtil

    return source_dir / f"{StockCodeUtil.to_suffix(qlib_code)}.parquet"


def _normalize_times(values: object) -> np.ndarray:
    """任意时间列 → ``datetime64[us]``（全模块统一分辨率，跨数组比较不隐式提升）。"""
    arr = np.asarray(pd.to_datetime(values))
    if arr.dtype != np.dtype("datetime64[us]"):
        arr = arr.astype("datetime64[us]")
    return arr


def _to_date(value: np.datetime64) -> date:
    """``datetime64`` → ``datetime.date``（经 ISO 字符串，避免 astype 跨版本差异）。"""
    return date.fromisoformat(str(np.datetime64(value, "D")))


def _read_symbol_times(path: Path) -> np.ndarray | None:
    """只读一个 parquet 的 time 列（pyarrow 列裁剪，不加载行情）。失败返回 None。"""
    try:
        import pyarrow.parquet as pq

        table = pq.read_table(path, columns=["time"])
    except Exception as exc:  # noqa: BLE001 —— 单个文件坏不炸整个扫描，记 warning
        logger.warning("[minute] 读取 %s 失败: %s", path, exc)
        return None
    return _normalize_times(table.column("time").to_pandas())


def scan_source_coverage(
    source_dir: str | os.PathLike[str],
    symbols: Sequence[str],
    *,
    universe: str = DEFAULT_UNIVERSE,
) -> MinuteSourceScan:
    """扫描源 parquet，返回覆盖事实（未构建 provider 也能回答前端目录）。

    只读各文件 time 列；``symbols`` 是 qlib 代码表（如 csi300）。缺失文件不
    报错而是进 ``symbols_missing``——「池里 300 只、分钟数据只有 290 只」是
    事实，由调用方决定提示方式。
    """
    src = Path(source_dir)
    if not src.is_dir():
        raise MinuteProviderError(f"分钟源目录不存在：{src}")
    found: list[str] = []
    missing: list[str] = []
    first_by: dict[str, date] = {}
    last_by: dict[str, date] = {}
    unions: list[np.ndarray] = []
    for code in symbols:
        parsed = _read_symbol_times(_suffix_file(src, code))
        if parsed is None or parsed.size == 0:
            missing.append(code)
            continue
        found.append(code)
        unions.append(parsed)
        first_by[code] = _to_date(parsed.min())
        last_by[code] = _to_date(parsed.max())
    if not unions:
        raise MinuteProviderError(
            f"分钟源目录 {src} 中没有任何股票池 {universe} 标的的数据"
        )
    calendar = np.unique(np.concatenate(unions))
    days = np.unique(calendar.astype("datetime64[D]"))
    coverage = MinuteCoverage(
        start=_to_date(days[0]),
        end=_to_date(days[-1]),
        days=int(days.size),
        bars=int(calendar.size),
        symbols=len(found),
    )
    return MinuteSourceScan(
        coverage=coverage,
        symbols_found=tuple(found),
        symbols_missing=tuple(missing),
        per_symbol_first=first_by,
        per_symbol_last=last_by,
    )


def source_fingerprint(
    source_dir: str | os.PathLike[str],
    symbols: Sequence[str],
    *,
    universe: str,
    day_provider_dir: str | os.PathLike[str],
) -> str:
    """源数据指纹（构建口径版本 + 文件 mtime+size + 池名 + day 层 factor 文件 mtime）。

    用来判定「已构建的 provider 是否仍代表当前源数据与当前构建口径」：分钟源新增
    数据 / 池变更 / 日线复权因子更新（除权）/ :data:`_META_VERSION` 变更任一发生
    即指纹变化 → 触发重建。只 stat 不读内容，600 次 stat 毫秒级。``symbols`` 必须
    与构建时同一份（meta 的 ``built_symbols``）——两边算法一致才能比较。
    """
    import hashlib

    src = Path(source_dir)
    day = Path(day_provider_dir)
    parts: list[str] = [
        f"builder={_META_VERSION}",
        f"universe={universe}",
        f"n={len(symbols)}",
    ]
    for code in sorted(symbols):
        for path in (
            _suffix_file(src, code),
            day / "features" / code / f"factor.{DAY_FREQ}.bin",
        ):
            try:
                st = path.stat()
                parts.append(f"{code}:{path.name}:{st.st_mtime_ns}:{st.st_size}")
            except OSError:
                parts.append(f"{code}:{path.name}:missing")
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


# ── bin 读写 ─────────────────────────────────────────────────────────


def _read_bin(path: Path) -> tuple[int, np.ndarray]:
    """qlib bin：首个 float32 = 日历起始下标，其余为值（float32 数组）。"""
    raw = path.read_bytes()
    if len(raw) < 4 or (len(raw) - 4) % 4 != 0:
        raise MinuteProviderError(f"qlib bin 损坏（长度 {len(raw)}）：{path}")
    start = int(struct.unpack("<f", raw[:4])[0])
    return start, np.frombuffer(raw[4:], dtype="<f4")


def _write_bin(path: Path, start_idx: int, values: np.ndarray) -> None:
    """写 qlib bin（父目录自动创建）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(struct.pack("<f", float(start_idx)))
        fh.write(np.asarray(values, dtype="<f4").tobytes())


def _day_factor_series(
    day_provider_dir: Path, symbol: str, day_calendar: list[date]
) -> np.ndarray | None:
    """读日线 provider 的 ``factor.day.bin`` → 与 day 日历等长的复权因子。

    缺失 / 越界回 None（调用方按 1.0 处理并记 warning）。NaN 一律当 1.0
    （因子在日历空洞处为 NaN，乘 NaN 会把整段价格抹掉）。
    """
    path = day_provider_dir / "features" / symbol / f"factor.{DAY_FREQ}.bin"
    if not path.is_file():
        return None
    start, vals = _read_bin(path)
    if start < 0 or start >= len(day_calendar):
        return None
    out = np.ones(len(day_calendar), dtype="float64")
    end = min(len(day_calendar), start + len(vals))
    out[start:end] = vals[: end - start]
    np.nan_to_num(out, copy=False, nan=1.0, posinf=1.0, neginf=1.0)
    return out


def _read_minute_calendar(provider_dir: Path) -> np.ndarray:
    """读 ``calendars/1min.txt`` → ``datetime64[us]`` 数组。"""
    path = provider_dir / "calendars" / f"{MINUTE_FREQ}.txt"
    if not path.is_file():
        raise MinuteProviderError(f"分钟 provider 未就绪（缺 1min 日历）：{provider_dir}")
    values = [
        ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()
    ]
    if not values:
        raise MinuteProviderError(f"1min 日历为空：{path}")
    return _normalize_times(pd.to_datetime(pd.Index(values)))


def read_minute_frame(
    provider_dir: str | os.PathLike[str],
    symbols: Sequence[str],
    *,
    start: date | None = None,
    end: date | None = None,
) -> pd.DataFrame:
    """从已构建 provider 读 1min 行情 → MultiIndex[dattime, instrument] 宽表。

    给分钟任务的 ``panel.parquet``（因子代码的数据面）用：**与因子表达式读到的是
    同一份 bin**——代码侧和评估侧对同一根 bar 的取值逐位一致，不会出现「写代码
    时看到 A、评估时算的是 B」的分裂。

    ``start`` / ``end`` 收窄到交易日闭区间；缺 bar（bin 里是 NaN）不落行——
    数据面只呈现**真实存在的 bar**，不造行、不补缺。列与 provider 字段同名加
    ``$`` 前缀；``$factor`` = 当日复权 step factor（全列 float64，与日线 h5 的
    数值类型约定一致）。
    """
    provider = Path(provider_dir)
    calendar = _read_minute_calendar(provider)
    w0, w1 = 0, calendar.size
    if start is not None:
        w0 = int(np.searchsorted(calendar, np.datetime64(start, "us"), side="left"))
    if end is not None:
        # end 是交易日闭区间 → 上界取次日零点（不含）
        w1 = int(
            np.searchsorted(
                calendar, np.datetime64(end + timedelta(days=1), "us"), side="left"
            )
        )
    if w0 >= w1:
        return pd.DataFrame(
            columns=[f"${f}" for f in MINUTE_FIELDS],
            index=pd.MultiIndex.from_arrays([[], []], names=["datetime", "instrument"]),
        )

    frames: list[pd.DataFrame] = []
    for code in symbols:
        feat_dir = provider / "features" / code
        close_path = feat_dir / f"close.{MINUTE_FREQ}.bin"
        if not close_path.is_file():
            logger.warning("[minute] read_minute_frame: %s 缺 close bin，跳过", code)
            continue
        start_idx, close = _read_bin(close_path)
        lo = max(w0, start_idx)
        hi = min(w1, start_idx + len(close))
        if lo >= hi:
            continue
        rel0 = lo - start_idx
        data: dict[str, np.ndarray] = {}
        for field_name in MINUTE_FIELDS:
            bin_path = feat_dir / f"{field_name}.{MINUTE_FREQ}.bin"
            _, values = _read_bin(bin_path)
            data[f"${field_name}"] = values[rel0 : rel0 + (hi - lo)].astype("float64")
        times = calendar[lo:hi]
        frame = pd.DataFrame(
            data,
            index=pd.MultiIndex.from_arrays(
                [times.astype("datetime64[ns]"), [code] * len(times)],
                names=["datetime", "instrument"],
            ),
        )
        frames.append(frame)
    if not frames:
        return pd.DataFrame(
            columns=[f"${f}" for f in MINUTE_FIELDS],
            index=pd.MultiIndex.from_arrays([[], []], names=["datetime", "instrument"]),
        )
    out = pd.concat(frames)
    # close 缺失 = 该标的这根 bar 不存在（NaN 对齐的洞），不落进数据面
    out = out[np.isfinite(out["$close"])]
    return out.sort_index()


def detect_amount_scale(
    amount: np.ndarray, volume: np.ndarray, close: np.ndarray
) -> tuple[float, float]:
    """量价反推金额单位换算 scale：``vwap = amount × scale / volume``。

    返回 ``(scale, 残差比)``：残差 = 实测中位系数 / 吸附后的 scale，1.0 为理想。
    样本不足（<10 根有效 bar）回 ``(DEFAULT_AMOUNT_SCALE, nan)``——调用方据此
    记 warning，而不是静默产出一条单位可疑的 vwap。
    """
    mask = (
        np.isfinite(amount)
        & np.isfinite(volume)
        & np.isfinite(close)
        & (amount > 0)
        & (volume > 0)
        & (close > 0)
    )
    if int(mask.sum()) < 10:
        return DEFAULT_AMOUNT_SCALE, float("nan")
    implied = float(np.median(close[mask] * volume[mask] / amount[mask]))
    if not np.isfinite(implied) or implied <= 0:
        return DEFAULT_AMOUNT_SCALE, float("nan")
    scale = float(10.0 ** round(np.log10(implied)))
    scale = min(max(scale, 1e-2), 1e6)
    return scale, implied / scale


# ── 构建 ─────────────────────────────────────────────────────────────


@contextmanager
def _provider_lock(provider_dir: Path) -> Iterator[None]:
    """跨进程排他锁（``<provider>.lock``，阻塞获取）。

    构建是「读源 → 写临时目录 → 换入」的长事务，两个任务同时冷启动必然重复
    构建（300 标的 × 8 field 的写盘量），锁把并发构建串行化；后续进程拿到锁
    后会在 :func:`ensure_minute_provider` 里複检就绪态，直接复用。
    """
    lock_path = provider_dir.with_name(provider_dir.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "w", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _swap_into_place(tmp_dir: Path, provider_dir: Path) -> None:
    """临时目录整体换入目标位（先挪走旧目录再 rename，同文件系统）。"""
    if provider_dir.is_symlink():
        provider_dir.unlink()
    if provider_dir.exists():
        backup = provider_dir.with_name(f"{provider_dir.name}.old-{os.getpid()}")
        shutil.rmtree(backup, ignore_errors=True)
        os.replace(provider_dir, backup)
        try:
            os.replace(tmp_dir, provider_dir)
        except OSError:
            os.replace(backup, provider_dir)  # 换入失败回滚旧目录
            raise
        shutil.rmtree(backup, ignore_errors=True)
    else:
        os.replace(tmp_dir, provider_dir)


def _read_source_frame(path: Path, code: str, warnings: list[str]) -> pd.DataFrame | None:
    """读一个标的的源 parquet（按 time 排序、去重），失败/空回 None。"""
    try:
        df = pd.read_parquet(path, columns=list(_SOURCE_COLUMNS))
    except Exception as exc:  # noqa: BLE001 —— 单标的坏不炸全量，记 warning
        warnings.append(f"{code}: 源读取失败（{exc}）")
        return None
    if df.empty:
        warnings.append(f"{code}: 源文件为空")
        return None
    df["time"] = _normalize_times(df["time"])
    df = df.sort_values("time", kind="stable").reset_index(drop=True)
    dup = int(df["time"].duplicated().sum())
    if dup:
        warnings.append(f"{code}: 源数据有 {dup} 个重复时间戳，保留末条")
        df = df[~df["time"].duplicated(keep="last")].reset_index(drop=True)
    return df


def build_minute_provider(
    *,
    provider_dir: str | os.PathLike[str] | None = None,
    source_dir: str | os.PathLike[str] | None = None,
    day_provider_dir: str | os.PathLike[str] | None = None,
    universe: str = DEFAULT_UNIVERSE,
    symbols: Sequence[str] | None = None,
    log: Callable[[str], None] | None = None,
) -> BuildReport:
    """构建混合叠加分钟 provider（临时目录构建 → 原子换入）。

    ``symbols`` 显式给定时跳过股票池解析（测试 / 小范围重建）。任何失败都
    抛 :class:`MinuteProviderError`，已有 provider 保持原样。
    """
    t0 = time.time()
    emit = log or (lambda msg: logger.info("%s", msg))
    out_dir = resolve_minute_provider_dir(provider_dir)
    src_dir = resolve_minute_source_dir(source_dir)
    day_dir = resolve_day_provider_dir(day_provider_dir)
    if not src_dir.is_dir():
        raise MinuteProviderError(f"分钟源目录不存在：{src_dir}")
    if not (day_dir / "calendars" / f"{DAY_FREQ}.txt").is_file():
        raise MinuteProviderError(f"day 层 provider 未就绪：{day_dir}")

    if symbols is None:
        symbols = resolve_universe(day_dir, universe)
    symbols = [str(s).strip().lower() for s in symbols if str(s).strip()]
    if not symbols:
        raise MinuteProviderError(f"股票池 {universe} 为空（{day_dir}/instruments）")

    day_calendar = [
        date.fromisoformat(ln.strip())
        for ln in (day_dir / "calendars" / f"{DAY_FREQ}.txt")
        .read_text(encoding="utf-8")
        .splitlines()
        if ln.strip()
    ]
    if not day_calendar:
        raise MinuteProviderError(f"day 层日历为空：{day_dir}")
    day_index = {np.datetime64(d, "D"): i for i, d in enumerate(day_calendar)}

    warnings: list[str] = []
    emit(f"[minute] 构建开始：池={universe} 标的={len(symbols)} 源={src_dir}")

    # ── Pass 1：只读 time 列，得全市场 1min 日历与各标的可用性 ──
    times_by_symbol: dict[str, np.ndarray] = {}
    missing: list[str] = []
    for code in symbols:
        path = _suffix_file(src_dir, code)
        if not path.is_file():
            missing.append(code)
            continue
        parsed = _read_symbol_times(path)
        if parsed is None or parsed.size == 0:
            warnings.append(f"{code}: time 列读取失败或为空")
            missing.append(code)
            continue
        parsed = np.unique(parsed)
        times_by_symbol[code] = parsed
    if not times_by_symbol:
        raise MinuteProviderError(
            f"分钟源 {src_dir} 中没有池 {universe} 的任何数据（缺失 {len(missing)} 只）"
        )
    if missing:
        warnings.append(
            f"{len(missing)} 只标的在分钟源缺失，已跳过：{','.join(missing[:10])}"
            + ("…" if len(missing) > 10 else "")
        )
    calendar = np.unique(np.concatenate(list(times_by_symbol.values())))
    day_list = np.unique(calendar.astype("datetime64[D]"))
    emit(f"[minute] 日历：{calendar.size} 根 bar / {day_list.size} 个交易日")
    # 空洞如实记 warning：构建日志里"121 个交易日"不该把 58 天断层藏起来
    built_days = tuple(_to_date(d) for d in day_list)
    for gap_after, gap_before in calendar_gaps(built_days):
        warnings.append(
            f"源数据存在 {gap_after} → {gap_before} 的空洞"
            f"（{(gap_before - gap_after).days - 1} 个自然日无数据），"
            f"可用连续块从 {terminal_usable_start(built_days)} 起"
        )

    # ── Pass 2：逐标的读全列 → 对齐 → 写 1min bins（读一个、写一个、即弃） ──
    out_dir.parent.mkdir(parents=True, exist_ok=True)  # 首次运行父目录可能还不存在
    tmp_dir = Path(
        tempfile.mkdtemp(prefix=f".{out_dir.name}.building-", dir=str(out_dir.parent))
    )
    try:
        (tmp_dir / "calendars").mkdir(parents=True)
        (tmp_dir / "features").mkdir(parents=True)
        (tmp_dir / "instruments").mkdir(parents=True)
        calendar_text = "\n".join(
            pd.DatetimeIndex(calendar).strftime("%Y-%m-%d %H:%M:%S")
        )
        (tmp_dir / "calendars" / f"{MINUTE_FREQ}.txt").write_text(
            calendar_text + "\n", encoding="utf-8"
        )

        built_symbols: list[str] = []
        for code, times in times_by_symbol.items():
            df = _read_source_frame(_suffix_file(src_dir, code), code, warnings)
            if df is None:
                continue
            pos = np.searchsorted(calendar, df["time"].to_numpy())
            if not np.array_equal(calendar[pos], df["time"].to_numpy()):
                raise MinuteProviderError(f"{code}: 时间戳对齐失败（日历不含该 bar）")
            if pos.size and (np.diff(pos) <= 0).any():
                raise MinuteProviderError(f"{code}: 时间戳非升序或重复（数据完整性）")
            start_idx = int(pos[0])
            span = int(pos[-1]) - start_idx + 1

            close = df["close"].to_numpy(dtype="float64")
            volume = df["volume"].to_numpy(dtype="float64")
            amount = df["amount"].to_numpy(dtype="float64")
            scale, residual = detect_amount_scale(amount, volume, close)
            if not np.isfinite(residual):
                warnings.append(
                    f"{code}: amount/volume 样本不足，vwap 按缺省 scale={scale:g} 估算"
                )
            elif abs(np.log10(residual)) > _SCALE_STEP_TOLERANCE:
                warnings.append(
                    f"{code}: 金额单位测定残差异常（系数 {residual * scale:.4g}，"
                    f"吸附后 {scale:g}）"
                )
            with np.errstate(invalid="ignore", divide="ignore"):
                vwap = np.where(
                    volume > 0,
                    amount * scale / np.where(volume > 0, volume, 1.0),
                    np.nan,
                )

            # 复权：价格 × 当日 step factor（日内恒定 → 日内收益不变）
            factor = np.ones(len(df), dtype="float64")
            fac_series = _day_factor_series(day_dir, code, day_calendar)
            if fac_series is None:
                warnings.append(
                    f"{code}: day 层缺 factor.{DAY_FREQ}.bin，复权因子按 1.0"
                )
            else:
                day_keys = df["time"].to_numpy().astype("datetime64[D]")
                mapped = np.array(
                    [day_index.get(k, -1) for k in day_keys], dtype="int64"
                )
                known = mapped >= 0
                factor[known] = fac_series[mapped[known]]
                if not known.all():
                    warnings.append(
                        f"{code}: {int((~known).sum())} 根 bar 不在 day 日历内，因子按 1.0"
                    )

            series = {
                "open": df["open"].to_numpy(dtype="float64") * factor,
                "high": df["high"].to_numpy(dtype="float64") * factor,
                "low": df["low"].to_numpy(dtype="float64") * factor,
                "close": close * factor,
                "volume": volume,
                "amount": amount,
                "vwap": vwap * factor,
                "factor": factor,
            }
            feat_dir = tmp_dir / "features" / code
            for fname, values in series.items():
                aligned = np.full(span, np.nan, dtype="float64")
                aligned[pos - start_idx] = values
                _write_bin(feat_dir / f"{fname}.{MINUTE_FREQ}.bin", start_idx, aligned)
            built_symbols.append(code)
            del df

        # ── day 层：日历 + 池内标的 + 基准的日线 bins 原样复制 ──
        shutil.copyfile(
            day_dir / "calendars" / f"{DAY_FREQ}.txt",
            tmp_dir / "calendars" / f"{DAY_FREQ}.txt",
        )
        day_layer_symbols = sorted(set(built_symbols) | set(BENCHMARK_SYMBOLS))
        copied = 0
        for code in day_layer_symbols:
            src_feat = day_dir / "features" / code
            if not src_feat.is_dir():
                if code in BENCHMARK_SYMBOLS:
                    warnings.append(
                        f"基准 {code} 在 day 层无行情目录，回测基准取数会失败"
                    )
                continue
            dst_feat = tmp_dir / "features" / code
            dst_feat.mkdir(parents=True, exist_ok=True)
            for bin_file in src_feat.glob(f"*.{DAY_FREQ}.bin"):
                shutil.copyfile(bin_file, dst_feat / bin_file.name)
                copied += 1
        emit(f"[minute] day 层复制：{copied} 个 bin（{len(day_layer_symbols)} 只标的）")

        # ── instruments：日线各池 ∩ 已构建标的，区间收窄到分钟覆盖 ──
        coverage_start = _to_date(day_list[0])
        coverage_end = _to_date(day_list[-1])
        # 区间末端留出 day 日历的**下一个交易日**（不越过 day 日历本身）：qlib 回测
        # 按 t+1 成交，回测终点（≤ 分钟覆盖末端）的最后一笔结算要取次日行情，
        # 而 day 层本来就带着这些真实日线 bar。少了这一格，回测末日会被静默截成
        # 少一天（不是崩，是悄悄少算）。只放一格，不做无意义的整段延伸。
        day_successor = {
            day_calendar[i]: day_calendar[i + 1] for i in range(len(day_calendar) - 1)
        }.get(coverage_end)
        r_end_ceiling = day_successor or coverage_end
        for inst_file in sorted((day_dir / "instruments").glob("*.txt")):
            rows: list[str] = []
            for line in inst_file.read_text(encoding="utf-8").splitlines():
                parts = line.strip().split("\t")
                if not parts or not parts[0].strip():
                    continue
                code = parts[0].strip().lower()
                if code not in built_symbols:
                    continue
                r_start = max(
                    date.fromisoformat(parts[1]) if len(parts) > 1 else coverage_start,
                    coverage_start,
                )
                r_end = min(
                    date.fromisoformat(parts[2]) if len(parts) > 2 else r_end_ceiling,
                    r_end_ceiling,
                )
                if r_start > r_end:
                    continue
                rows.append(f"{code}\t{r_start.isoformat()}\t{r_end.isoformat()}")
            # 空池不落地：宁可 qlib 报「instruments 文件不存在」（响亮失败），
            # 也不要一个空文件把数据集静默清空
            if not rows and inst_file.name != "all.txt":
                continue
            (tmp_dir / "instruments" / inst_file.name).write_text(
                "\n".join(rows) + ("\n" if rows else ""), encoding="utf-8"
            )
        if not (tmp_dir / "instruments" / f"{universe}.txt").exists():
            if (day_dir / "instruments" / f"{universe}.txt").is_file():
                raise MinuteProviderError(
                    f"股票池 {universe} 内没有任何标的可用分钟数据（源缺数据？）"
                )
            raise MinuteProviderError(
                f"股票池 {universe} 在 day 层 instruments 里不存在（{day_dir}）"
            )

        fingerprint = source_fingerprint(
            src_dir,
            built_symbols,
            universe=universe,
            day_provider_dir=day_dir,
        )
        report = BuildReport(
            provider_dir=out_dir,
            universe=universe,
            symbols=len(built_symbols),
            symbols_missing=tuple(missing),
            bars=int(calendar.size),
            days=int(day_list.size),
            first=coverage_start,
            last=coverage_end,
            fingerprint=fingerprint,
            elapsed_seconds=round(time.time() - t0, 2),
            warnings=tuple(warnings),
        )
        meta = {
            "version": _META_VERSION,
            "freq": MINUTE_FREQ,
            "universe": universe,
            "provider_dir": str(out_dir),
            "source_dir": str(src_dir),
            "day_provider": str(day_dir),
            "symbols": len(built_symbols),
            "built_symbols": built_symbols,
            "symbols_missing": list(missing),
            "bars": int(calendar.size),
            "days": int(day_list.size),
            "first": coverage_start.isoformat(),
            "last": coverage_end.isoformat(),
            "fields": list(MINUTE_FIELDS),
            "day_layer_symbols": day_layer_symbols,
            "fingerprint": fingerprint,
            "built_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "elapsed_seconds": report.elapsed_seconds,
            "warnings": list(warnings),
        }
        (tmp_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _swap_into_place(tmp_dir, out_dir)
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    emit(
        f"[minute] 构建完成：{len(built_symbols)} 只 / {calendar.size} bar / "
        f"{day_list.size} 日（{report.elapsed_seconds}s，警告 {len(warnings)} 条）→ {out_dir}"
    )
    for msg in warnings[:20]:
        emit(f"[minute][警告] {msg}")
    return report


def ensure_minute_provider(
    *,
    provider_dir: str | os.PathLike[str] | None = None,
    source_dir: str | os.PathLike[str] | None = None,
    day_provider_dir: str | os.PathLike[str] | None = None,
    universe: str = DEFAULT_UNIVERSE,
    force: bool = False,
    log: Callable[[str], None] | None = None,
) -> tuple[Path, BuildReport | None]:
    """确保分钟 provider 就绪且新鲜；返回 ``(目录, 本次构建报告或 None)``。

    新鲜判据 = 目录就绪 + 池名一致 + 源指纹一致（源新增数据 / 除权因子更新
    即重建）。锁内複检，并发冷启动只构建一次。
    """
    out_dir = resolve_minute_provider_dir(provider_dir)
    src_dir = resolve_minute_source_dir(source_dir)
    day_dir = resolve_day_provider_dir(day_provider_dir)
    with _provider_lock(out_dir):
        if not force and is_minute_provider_ready(out_dir):
            meta = read_minute_meta(out_dir) or {}
            built_symbols = [str(s) for s in meta.get("built_symbols", [])]
            if meta.get("universe") == universe and built_symbols:
                fp = source_fingerprint(
                    src_dir,
                    built_symbols,
                    universe=universe,
                    day_provider_dir=day_dir,
                )
                if fp == meta.get("fingerprint"):
                    logger.info("[minute] provider 命中（新鲜）：%s", out_dir)
                    return out_dir, None
                logger.info("[minute] 源数据已变化，重建 provider：%s", out_dir)
        report = build_minute_provider(
            provider_dir=out_dir,
            source_dir=src_dir,
            day_provider_dir=day_dir,
            universe=universe,
            log=log,
        )
    return out_dir, report


def minute_provider_status(
    *,
    provider_dir: str | os.PathLike[str] | None = None,
    source_dir: str | os.PathLike[str] | None = None,
    day_provider_dir: str | os.PathLike[str] | None = None,
    universe: str = DEFAULT_UNIVERSE,
) -> dict:
    """前端/接口用的状态摘要（绝不抛：任何失败转 ``error`` 字段如实上屏）。

    返回 ``ready`` / ``coverage``（已构建）/ ``source_coverage``（源现扫）/
    ``stale``（源已变但未重建）/ ``error``。
    """
    out: dict = {
        "ready": False,
        "coverage": None,
        "source_coverage": None,
        "stale": None,
        "error": None,
    }
    try:
        pdir = resolve_minute_provider_dir(provider_dir)
        out["provider_dir"] = str(pdir)
        out["ready"] = is_minute_provider_ready(pdir)
        meta = read_minute_meta(pdir) or {}
        out["coverage"] = _coverage_json(minute_coverage(pdir))
        day_dir = resolve_day_provider_dir(day_provider_dir)
        src_dir = resolve_minute_source_dir(source_dir)
        try:
            scan = scan_source_coverage(
                src_dir, resolve_universe(day_dir, universe), universe=universe
            )
            out["source_coverage"] = _coverage_json(scan.coverage)
            out["source_symbols_missing"] = len(scan.symbols_missing)
        except MinuteProviderError as exc:
            # 源不可用不算整体失败：已构建的 provider 依然可用
            out["source_error"] = str(exc)
        built_symbols = [str(s) for s in meta.get("built_symbols", [])]
        if out["ready"] and built_symbols:
            fp = source_fingerprint(
                src_dir,
                built_symbols,
                universe=universe,
                day_provider_dir=day_dir,
            )
            out["stale"] = fp != meta.get("fingerprint")
    except Exception as exc:  # noqa: BLE001 —— 状态接口不炸调用方
        out["error"] = str(exc)
    return out


def _coverage_json(cov: MinuteCoverage | None) -> dict | None:
    if cov is None:
        return None
    return {
        "start": cov.start.isoformat(),
        "end": cov.end.isoformat(),
        "days": cov.days,
        "bars": cov.bars,
        "symbols": cov.symbols,
        "built_at": cov.built_at,
    }


def main(argv: Sequence[str] | None = None) -> int:
    """运维入口：``python -m backend.services.engine.minute_qlib_provider [--force]``。"""
    import argparse

    parser = argparse.ArgumentParser(description="构建分钟（1min）Qlib 混合 provider")
    parser.add_argument("--universe", default=DEFAULT_UNIVERSE)
    parser.add_argument("--provider-dir", default="")
    parser.add_argument("--source-dir", default="")
    parser.add_argument("--day-provider", default="")
    parser.add_argument("--force", action="store_true", help="忽略新鲜度检查强制重建")
    parser.add_argument("--status", action="store_true", help="只打印状态，不构建")
    args = parser.parse_args(argv)

    if args.status:
        print(json.dumps(minute_provider_status(
            provider_dir=args.provider_dir or None,
            source_dir=args.source_dir or None,
            day_provider_dir=args.day_provider or None,
            universe=args.universe,
        ), ensure_ascii=False, indent=2))
        return 0

    out_dir, report = ensure_minute_provider(
        provider_dir=args.provider_dir or None,
        source_dir=args.source_dir or None,
        day_provider_dir=args.day_provider or None,
        universe=args.universe,
        force=args.force,
        log=lambda msg: print(msg, flush=True),
    )
    print(json.dumps({
        "provider_dir": str(out_dir),
        "built": report is not None,
        "report": None if report is None else {
            "symbols": report.symbols,
            "bars": report.bars,
            "days": report.days,
            "first": report.first.isoformat(),
            "last": report.last.isoformat(),
            "elapsed_seconds": report.elapsed_seconds,
            "warnings": list(report.warnings),
        },
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover —— 运维入口
    raise SystemExit(main())
