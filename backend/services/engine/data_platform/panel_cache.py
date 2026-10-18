"""Panel parquet 缓存 —— H5 退役后的因子基础面板（参考 superalpha 同名模块）。

形态：``<data_root>/.panel_cache/daily_pv.parquet`` 单文件，索引内嵌
``MultiIndex[datetime, instrument]``（instrument 为 qlib 小写格式 ``sh600036``），
列为 ``open/high/low/close/volume/amount/factor``（前复权 + 复权因子），zstd 压缩。

口径（与 H5 时代 ``daily_pv_all.h5`` 生成器一致，已退役）：
- 数据源：``daily_forward``（前复权 OHLCV）+ ``daily_unadjusted``（不复权收盘）；
- ``factor = close_qfq / close_unadj``（不复权收盘 <= 0 时取 1.0）；
- 剔除前复权收盘 <= 0 的行（高分红股多年除权后 qfq 价转负）；
- 窗口自 ``PANEL_START``（2020-01-01）起。

与 superalpha 的差异：本模块经既有 ``QuantDBDataHub``（DuckDB 精确分区直读）
构建，不引入 ``quantdb-sdk`` 新依赖；富化列（技术指标/估值/L2 等）不在面板内，
因子代码经 ``quantdb_data.daily()`` 按需读取。

缓存失效：``daily_forward`` 分区指纹（文件数 + 最大 mtime），记入
``daily_pv.meta.json``（schema ``panel-cache/v1``）。
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

PANEL_SCHEMA = "panel-cache/v1"
PANEL_DIRNAME = ".panel_cache"
PANEL_FILENAME = "daily_pv.parquet"
PANEL_DEBUG_FILENAME = "daily_pv_debug.parquet"
PANEL_META = "daily_pv.meta.json"
PANEL_START = "2020-01-01"

_BASE_COLS = ["open", "high", "low", "close", "volume", "amount"]
_PANEL_COLS = ["symbol", "time", *_BASE_COLS, "factor"]

ProgressCB = Callable[[int, int, str], None]


def panel_dir(data_root: str | Path) -> Path:
    return Path(data_root) / PANEL_DIRNAME


def panel_path(data_root: str | Path, *, debug: bool = False) -> Path:
    return panel_dir(data_root) / (PANEL_DEBUG_FILENAME if debug else PANEL_FILENAME)


def _source_fingerprint(dataset_dir: Path) -> str | None:
    """数据集分区指纹：``<分区数>:<最大 mtime>``（廉价失效判据）。"""
    try:
        count = 0
        latest = 0.0
        with os.scandir(dataset_dir) as it:
            for entry in it:
                if entry.is_dir(follow_symlinks=False) and entry.name.startswith("dt="):
                    count += 1
                    try:
                        mtime = entry.stat().st_mtime
                    except OSError:
                        continue
                    if mtime > latest:
                        latest = mtime
        if count == 0:
            return None
        return f"{count}:{latest:.3f}"
    except OSError:
        return None


def _read_meta(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - 元文件损坏视为不存在
        return None


def _write_meta(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def is_fresh(data_root: str | Path, *, source_fingerprint: str | None = None) -> bool:
    """缓存存在且源数据未变化。"""
    meta_path = panel_dir(data_root) / PANEL_META
    if not panel_path(data_root).exists() or not meta_path.exists():
        return False
    meta = _read_meta(meta_path)
    if not meta or meta.get("schema") != PANEL_SCHEMA:
        return False
    fp = source_fingerprint or _source_fingerprint(
        Path(data_root) / "1_kline_data" / "daily_forward"
    )
    return bool(fp) and meta.get("source_fingerprint") == fp


def _to_qlib_symbol(symbol: str) -> str:
    """交易所后缀式 → qlib 小写（600036.SH → sh600036）。"""
    s = str(symbol).strip()
    if "." in s and not s[:1].isalpha():
        code, exch = s.split(".", 1)
        return f"{exch.lower()}{code}"
    return s.lower()


def _fetch_kline_frame(hub: Any, subdir: str, cols: list[str], start: str) -> Any:
    """全市场 K 线分年拉取（单次全量查询内存过大，按自然年分块）。"""
    import pandas as pd

    frames: list[Any] = []
    start_year = int(start[:4])
    end_year = pd.Timestamp.today().year
    for year in range(start_year, end_year + 1):
        dates = hub._partition_dates(
            f"1_kline_data/{subdir}",
            start=pd.Timestamp(f"{year}-01-01").date(),
            end=pd.Timestamp(f"{year}-12-31").date(),
        )
        if not dates:
            continue
        df = hub._read_partitioned(
            f"1_kline_data/{subdir}",
            dates,
            cols=", ".join(cols),
            order_by="symbol, dt",
        )
        if df is not None and not df.empty:
            frames.append(df)
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def ensure_panel_cache(
    data_root: str | Path,
    *,
    debug_symbols: int = 50,
    progress_cb: ProgressCB | None = None,
) -> tuple[Path | None, Path | None]:
    """确保面板缓存就绪，返回 ``(panel_path, debug_path)``（失败段为 None）。

    缓存命中直接返回；源数据变化或缺失时重建（全量构建约 1 分钟，写盘
    zstd 约 273MB）。debug 面板始终从全量面板派生（前 N 只）。
    """
    import pandas as pd

    data_root = str(data_root)
    fwd_dir = Path(data_root) / "1_kline_data" / "daily_forward"
    fp = _source_fingerprint(fwd_dir)
    out = panel_path(data_root)
    debug_out = panel_path(data_root, debug=True)

    if fp and is_fresh(data_root, source_fingerprint=fp):
        logger.info("panel-cache: hit %s", out)
        return (out if out.exists() else None), (debug_out if debug_out.exists() else None)

    def _report(done: int, total: int, label: str) -> None:
        if progress_cb:
            try:
                progress_cb(done, total, label)
            except Exception:  # noqa: BLE001 - 进度回调不阻断构建
                pass

    _report(0, 4, "构建基础面板缓存（首次约 1 分钟）")
    try:
        from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

        hub = QuantDBDataHub(data_root)
        if not hub.available:
            raise RuntimeError(f"QuantDB 数据目录不可用（root={data_root}）")
        fwd = _fetch_kline_frame(hub, "daily_forward", ["symbol", "open", "high", "low", "close", "volume", "amount"], PANEL_START)
        _report(1, 4, f"前复权 K 线就绪（{len(fwd):,} 行）")
        unadj = _fetch_kline_frame(hub, "daily_unadjusted", ["symbol", "close"], PANEL_START)
        _report(2, 4, "不复权收盘就绪，计算复权因子")
    except Exception as exc:  # noqa: BLE001 - 构建失败保留旧缓存
        logger.error("panel-cache: build failed: %s", exc)
        return (out if out.exists() else None), (debug_out if debug_out.exists() else None)

    if fwd.empty or unadj.empty:
        logger.error("panel-cache: K 线为空（root=%s）", data_root)
        return (out if out.exists() else None), (debug_out if debug_out.exists() else None)

    # dt（hive 分区整数 YYYYMMDD）→ time；symbol 保持交易所后缀式用于对齐
    for frame in (fwd, unadj):
        if "trade_date" in frame.columns and "time" not in frame.columns:
            frame.rename(columns={"trade_date": "time"}, inplace=True)
        if "dt" in frame.columns and "time" not in frame.columns:
            frame["time"] = pd.to_datetime(frame["dt"].astype(str), format="%Y%m%d", errors="coerce")
        else:
            frame["time"] = pd.to_datetime(frame["time"], errors="coerce")
        frame["symbol"] = frame["symbol"].astype(str)

    unadj_close = unadj[["symbol", "time", "close"]].rename(columns={"close": "_close_unadj"})
    df = fwd.merge(unadj_close, on=["symbol", "time"], how="left")
    valid = df["close"].to_numpy(dtype="float64") > 0
    dropped = int((~valid).sum())
    if dropped:
        logger.info("panel-cache: dropped %d rows with non-positive qfq close", dropped)
        df = df.loc[valid].reset_index(drop=True)
    if df.empty:
        logger.error("panel-cache: 无有效正价行（root=%s）", data_root)
        return (out if out.exists() else None), (debug_out if debug_out.exists() else None)

    import numpy as np

    unadj_close_np = df["_close_unadj"].to_numpy(dtype="float64", na_value=0.0)
    close_np = df["close"].to_numpy(dtype="float64")
    df["factor"] = np.where(
        unadj_close_np > 0,
        close_np / np.where(unadj_close_np > 0, unadj_close_np, 1.0),
        1.0,
    )
    if "amount" not in df.columns:
        df["amount"] = np.nan

    t = pd.to_datetime(df["time"])
    inst = df["symbol"].map(_to_qlib_symbol)
    midf = pd.DataFrame(
        {c: df[c].to_numpy() for c in _BASE_COLS + ["factor"]},
        index=pd.MultiIndex.from_arrays([t, inst], names=["datetime", "instrument"]),
    ).sort_index()

    _report(3, 4, f"面板组装完成（{len(midf):,} 行），写入缓存")
    try:
        panel_dir(data_root).mkdir(parents=True, exist_ok=True)
        tmp = out.with_name(out.name + f".tmp{os.getpid()}")
        midf.to_parquet(tmp, engine="pyarrow", compression="zstd")
        os.replace(tmp, out)
        _write_meta(
            panel_dir(data_root) / PANEL_META,
            {
                "schema": PANEL_SCHEMA,
                "source_fingerprint": fp,
                "rows": int(len(midf)),
                "columns": _BASE_COLS + ["factor"],
                "index": ["datetime", "instrument"],
                "start": PANEL_START,
                "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "dropped_nonpositive_qfq": dropped,
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("panel-cache: write failed: %s", exc)
        return (out if out.exists() else None), (debug_out if debug_out.exists() else None)
    _report(4, 4, "面板缓存就绪")

    debug_path = _ensure_debug_panel(data_root, debug_symbols)
    return out, debug_path


def _ensure_debug_panel(data_root: str | Path, debug_symbols: int) -> Path | None:
    """从全量面板派生前 N 只股票的小面板（供 debug 迭代）。"""
    src = panel_path(data_root)
    dst = panel_path(data_root, debug=True)
    if not src.exists():
        return dst if dst.exists() else None
    try:
        import pandas as pd

        df = pd.read_parquet(src)
        symbols = sorted(df.index.get_level_values("instrument").unique())[:debug_symbols]
        df = df[df.index.get_level_values("instrument").isin(symbols)]
        tmp = dst.with_name(dst.name + f".tmp{os.getpid()}")
        df.to_parquet(tmp, engine="pyarrow", compression="zstd")
        os.replace(tmp, dst)
        return dst
    except Exception as exc:  # noqa: BLE001
        logger.warning("panel-cache: debug panel failed: %s", exc)
        return dst if dst.exists() else None


def load_panel(
    path: str | Path,
    *,
    columns: list[str] | None = None,
    qlib_style: bool = False,
    start: str | None = None,
    end: str | None = None,
) -> Any:
    """读面板缓存 → MultiIndex[datetime, instrument] 宽表。

    ``columns=None``（默认）返回文件内全部列；显式传列名则列裁剪直读；
    ``qlib_style=True`` 时列名带 ``$`` 前缀（旧因子代码 ``df["$close"]`` 写法零改动）。
    """
    import pandas as pd

    if columns is None:
        out = pd.read_parquet(path)
    else:
        out = pd.read_parquet(path, columns=list(columns))
    if start or end:
        lo = pd.Timestamp(start) if start else None
        hi = pd.Timestamp(end) if end else None
        level = out.index.get_level_values("datetime")
        mask = pd.Series(True, index=out.index)
        if lo is not None:
            mask &= level >= lo
        if hi is not None:
            mask &= level <= hi
        out = out[mask]
    if qlib_style:
        out = out.rename(columns={c: f"${c}" for c in out.columns})
    return out
