"""quantdb_data —— AutoAlpha 因子取数入口（平台生成，因子代码勿修改）。

因子数据统一从 QuantDB 本地仓库（parquet）读取，两个入口::

    df = qd.panel()                # $open/$high/$low/$close/$volume/$amount/$factor
                                   # MultiIndex(datetime, instrument)，前复权口径
    df = qd.panel(columns=['close', 'volume'], style=None)  # 列裁剪 + 原始列名
    df = qd.daily('l1_factors', ['turn_20'])                # 日频特征宽表
    fin = qd.financial('pershare_index', ['du_return_on_equity'])

AutoAlpha 取数实现：本模块经仓库既有
``QuantDBDataHub``（DuckDB 精确分区直读）取数，不依赖 ``quantdb-sdk``；
平台把本文件下发（软链/复制）到因子执行目录，因子代码 ``import quantdb_data
as qd`` 即可取数。``panel()`` 读任务目录面板文件（``QUANTDB_PANEL_PATH``）
或仓库 ``.panel_cache/daily_pv.parquet``。

约定：
- 返回的 DataFrame 索引一律是 ``MultiIndex (datetime, instrument)``，
  ``instrument`` 为 qlib 小写格式（``sh600036``）；
- 财务数据默认按公告日（``m_anntime`` / ``declareDate``）前推对齐（``asof=True``），用报告期
  直接 join 会造成前视偏差；
- ``daily()/financial()`` 有单元预算（MAX_CELLS），超限报错并提示缩小范围；
  ``panel()`` 基础面板不受预算限制。

单位规范（全仓库统一）：价格元；日线 ``volume`` 股、``amount`` 万元；
市值元；股本股；财务报表金额元；衍生因子列无量纲直接用。
"""

from __future__ import annotations

import os
from pathlib import Path

# 取数 SDK 版本（语义化；改动取数语义/asof/口径/预算时必须递增）。
# 平台把该版本写入任务溯源与因子 metadata（qdVersion），保证重跑可追溯。
__version__ = "1.0.0"

try:
    import pandas as pd
except ImportError as exc:  # noqa: BLE001
    raise ImportError(
        "quantdb_data 需要 pandas（pip install \"pandas>=2.0\"）。"
    ) from exc

__all__ = ["catalog", "columns", "panel", "daily", "financial", "minute", "members", "root"]

_ROOT_ENV = "QM_QUANTDB_DATA_DIR"
_ROOT_ENV_FALLBACK = "QUANTDB_DATA_ROOT"
_CONTAINER_DEFAULT = "/data/quantdb"


def _os_default_root() -> str:
    return "E:/quantdb" if os.name == "nt" else "/mnt/e/quantdb"


def root() -> str:
    """仓库根目录（全环境变量驱动）。

    定位顺序：``QM_QUANTDB_DATA_DIR`` → ``QUANTDB_DATA_ROOT`` →
    ``/data/quantdb``（容器）→ 系统默认。平台调度时会把权威路径写进
    ``QM_QUANTDB_DATA_DIR``，因子代码无需关心落在哪。
    """
    for cand in (
        os.getenv(_ROOT_ENV, "").strip(),
        os.getenv(_ROOT_ENV_FALLBACK, "").strip(),
        _CONTAINER_DEFAULT,
    ):
        if cand and Path(cand).is_dir():
            return cand
    return _os_default_root()


def _hub() -> object:
    """QuantDBDataHub 懒加载（模块 import 时不碰 backend，保证下发目录可独立 import）。"""
    from backend.services.engine.data_platform.quantdb_hub import QuantDBDataHub

    return QuantDBDataHub(root())


def _to_qlib(sym: str) -> str:
    """600036.SH → sh600036；已是 sh600036 则原样返回。"""
    s = str(sym).strip()
    if "." in s and not s[:1].isalpha():
        code, exch = s.split(".", 1)
        return f"{exch.lower()}{code}"
    return s.lower()


def _to_exchange(sym: str) -> str:
    """qlib 小写符号 → 仓库交易所格式（sh600036 → 600036.SH）。"""
    s = str(sym).strip()
    if s[:2].lower() in ("sh", "sz", "bj") and "." not in s:
        return f"{s[2:]}.{s[:2].upper()}"
    return s.upper()


def _norm_symbols(symbols: object) -> set | None:
    if symbols is None:
        return None
    if isinstance(symbols, str):
        return {_to_qlib(symbols)}
    return {_to_qlib(s) for s in symbols}


_BASES = ("6_ml_datasets", "1_kline_data", "5_technical_derived", "2_base_sector",
          "3_financial_data", "4_bond_etf")
_FIN_TABLES = {"balance", "income", "cashflow", "capital", "per_share",
               "pershare_index", "dividend_factors", "holder_num"}
MAX_CELLS = 40_000_000


def _find_dataset(dataset: str) -> Path | None:
    r = Path(root())
    for base in _BASES:
        d = r / base / dataset
        if d.is_dir():
            return d
    return None


def _check_budget(df: pd.DataFrame, dataset: str) -> pd.DataFrame:
    if df.shape[0] * df.shape[1] > MAX_CELLS:
        raise ValueError(
            f"{dataset}: 结果过大（{df.shape[0]} 行 × {df.shape[1]} 列），"
            "请缩小 start/end 窗口、减少列数或限定 symbols"
        )
    return df


def catalog(refresh: bool = False) -> dict[str, int]:
    """仓库可用数据集 → 列数（目录枚举，不载入数据）。"""
    out: dict[str, int] = {}
    r = Path(root())
    for base in _BASES:
        b = r / base
        if not b.is_dir():
            continue
        try:
            names = [x.name for x in os.scandir(b) if x.is_dir()]
        except OSError:
            continue
        for name in sorted(names):
            try:
                out[name] = len(columns(name))
            except Exception:  # noqa: BLE001 - 单数据集失败不阻断目录
                out[name] = 0
    return out


def _dataset_columns(dataset: str) -> list[str]:
    """数据集列名（读最新分片文件头 schema，不载入数据）。"""
    import pyarrow.parquet as pq

    d = _find_dataset(dataset)
    if d is None:
        raise ValueError(f"未知数据集 {dataset}；可用：{sorted(catalog())}")
    if dataset in _FIN_TABLES:
        files = sorted(d.glob("*.parquet"))
        if not files:
            return []
        return [str(n) for n in pq.ParquetFile(files[0]).schema_arrow.names]
    cands: list[Path] = []
    try:
        names = [x.name for x in os.scandir(d)
                 if x.is_dir(follow_symlinks=False) and x.name.startswith("dt=")]
    except OSError:
        names = []
    if names:
        f = d / max(names) / "data.parquet"
        if f.is_file():
            cands.append(f)
    if not cands:
        files = sorted(d.glob("*.parquet"))
        if files:
            cands.append(files[-1])
    if not cands:
        return []
    return [str(n) for n in pq.ParquetFile(cands[0]).schema_arrow.names]


def columns(dataset: str) -> list[str]:
    """数据集列名（读最新分片文件头 schema，不载入数据）。"""
    return _dataset_columns(dataset)


def _finalize(df: pd.DataFrame, dataset: str) -> pd.DataFrame:
    if df.empty:
        return df
    if "datetime" not in df.columns:
        for cand in ("trade_date", "time", "date"):
            if cand in df.columns:
                df = df.rename(columns={cand: "datetime"})
                break
    if "instrument" not in df.columns:
        sym_col = "Symbol" if "Symbol" in df.columns else "symbol"
        df = df.assign(instrument=df[sym_col].map(_to_qlib))
    df = df.drop(columns=[c for c in ("symbol", "Symbol") if c in df.columns])
    df["datetime"] = pd.to_datetime(df["datetime"])
    if df.empty:
        return pd.DataFrame()
    df = df.set_index(["datetime", "instrument"]).sort_index()
    df = df[~df.index.duplicated(keep="last")]
    return _check_budget(df, dataset)


_PANEL_BASE = ("open", "high", "low", "close", "volume", "amount", "factor")
_PANEL_ENV = "QUANTDB_PANEL_PATH"


def _panel_cache_path() -> Path | None:
    """面板文件定位：任务目录注入 → 仓库 .panel_cache → None。"""
    p = os.getenv(_PANEL_ENV, "").strip()
    if p and Path(p).is_file():
        return Path(p)
    cand = Path(root()) / ".panel_cache" / "daily_pv.parquet"
    return cand if cand.is_file() else None


def panel(
    columns: list[str] | None = None,
    start: str | None = None,
    end: str | None = None,
    style: str | None = "qlib",
) -> pd.DataFrame:
    """基础价量面板 → MultiIndex(datetime, instrument) 宽表。

    默认列为文件内全部列（基础面板即 7 列；挖掘同源富化切片含富化列时
    一并返回——旧因子代码读到的仍是它挖掘时看到的那套列）；
    显式传 ``columns`` 则做列裁剪（非法列名抛错）。
    ``style="qlib"``（默认）列名带 ``$`` 前缀（``$close``），旧因子代码零改动；
    ``style=None`` 返回原始列名。基础面板不受 MAX_CELLS 预算限制。
    缓存缺失时尝试现场构建一次（约 1 分钟），失败则抛错。
    """
    cache = _panel_cache_path()
    if cache is None:
        from backend.services.engine.data_platform.panel_cache import ensure_panel_cache

        cache, _ = ensure_panel_cache(root())
        if cache is None or not cache.is_file():
            raise ValueError(
                "panel(): 基础面板缓存不可用且自建失败（检查 "
                f"{root()}/1_kline_data/daily_forward 是否就绪）"
            )
    if columns is None:
        out = pd.read_parquet(cache)
    else:
        want = [c for c in columns]
        bad = [c for c in want if c not in _PANEL_BASE]
        if bad:
            # 富化切片文件可能含面板之外的列：按文件实际列名校验，而非基础 7 列
            avail = set(pd.read_parquet(cache).columns)
            bad = [c for c in want if c not in avail and f"${c}" not in avail]
            if bad:
                raise ValueError(f"panel() 无这些列：{bad}（可用：{sorted(avail)}；"
                                 "扩展数据请用 daily()/financial()）")
        out = pd.read_parquet(cache, columns=want)
    if start or end:
        level = out.index.get_level_values("datetime")
        mask = pd.Series(True, index=out.index)
        if start:
            mask &= level >= pd.Timestamp(start)
        if end:
            mask &= level <= pd.Timestamp(end)
        out = out[mask]
    if style == "qlib":
        out = out.rename(columns={c: f"${c}" for c in out.columns})
    return out


def daily(
    dataset: str,
    columns: list[str] | None = None,
    start: str | None = None,
    end: str | None = None,
    symbols: list[str] | None = None,
) -> pd.DataFrame:
    """任意日期分区日频数据集 → MultiIndex(datetime, instrument) 宽表。

    数据集从 ``catalog()`` 发现，列名用 ``columns(dataset)`` 查询；调用时只读所选
    列和日期窗口。财务表请用 ``financial()``，分钟线请用 ``minute()``。
    """
    from datetime import date as _date

    if dataset in _FIN_TABLES:
        raise ValueError(f"{dataset} 是财务表，请用 financial('{dataset}', [字段...])（按公告日对齐）")
    hub = _hub()
    s = pd.Timestamp(start).date() if start else _date(2020, 1, 1)
    e = pd.Timestamp(end).date() if end else pd.Timestamp.today().date()
    syms = [_to_exchange(x) for x in symbols] if symbols else None
    dataset_dir = _find_dataset(dataset)
    if dataset_dir is None:
        raise ValueError(f"未知 QuantDB 数据集 {dataset}；可用：{sorted(catalog())}")
    rel_path = dataset_dir.relative_to(Path(root())).as_posix()
    schema = _dataset_columns(dataset)
    if not any(dataset_dir.glob("dt=*")):
        raise ValueError(f"{dataset} 不是日频分区数据；分钟线请用 minute()，财务表请用 financial()")
    key_columns = {"symbol", "Symbol", "wind_code", "datetime", "trade_date", "date", "time", "dt", "instrument"}
    symbol_column = next((name for name in ("symbol", "Symbol", "wind_code") if name in schema), None)
    if symbol_column is None:
        raise ValueError(f"{dataset} 不含股票代码列，不能作为日频因子数据读取")
    requested = list(dict.fromkeys(columns or [name for name in schema if name not in key_columns]))
    unknown = [name for name in requested if name not in schema]
    if unknown:
        raise ValueError(f"{dataset} 无这些列：{unknown[:10]}（可用 {len(schema)} 列，见 columns('{dataset}')）")
    df = hub.fetch_dataset_columns(
        rel_path, requested, start=s, end=e, symbols=syms,
    )
    if df is None or df.empty:
        return pd.DataFrame()
    if "dt" in df.columns:
        raw_dates = df["dt"].astype(str).str.replace(r"\.0$", "", regex=True)
        df["datetime"] = pd.to_datetime(raw_dates, format="%Y%m%d", errors="coerce")
        df = df.drop(columns=["dt"])
    elif "trade_date" in df.columns:
        df["datetime"] = pd.to_datetime(df["trade_date"], errors="coerce")
    elif "date" in df.columns:
        df["datetime"] = pd.to_datetime(df["date"], errors="coerce")
    elif "time" in df.columns:
        df["datetime"] = pd.to_datetime(df["time"], errors="coerce")
    df = df.drop(columns=[name for name in ("dt", "trade_date", "date", "time") if name in df.columns])
    want_syms = _norm_symbols(symbols)
    sym_col = next((name for name in ("symbol", "Symbol", "wind_code") if name in df.columns), None)
    if sym_col:
        df = df.assign(instrument=df[sym_col].map(_to_qlib))
        if want_syms is not None:
            df = df[df["instrument"].isin(want_syms)]
    return _finalize(df, dataset)


def financial(
    table: str,
    fields: list[str],
    start: str | None = None,
    end: str | None = None,
    symbols: list[str] | None = None,
    asof: bool = True,
) -> pd.DataFrame:
    """财务表 → 日频面板（默认按公告日列前推对齐，避免前视）。

    传 ``symbols`` 最快（只读这些股票的文件）；不传时逐全市场股票读取，
    结果可能较大（受 MAX_CELLS 预算限制）。
    """
    hub = _hub()
    if table not in _FIN_TABLES:
        raise ValueError(f"financial() 仅支持财务表 {sorted(_FIN_TABLES)}，收到 {table}")
    fields = list(dict.fromkeys(fields))
    start = start or "2018-01-01"
    end = end or "2100-01-01"

    if symbols is not None:
        frames = []
        for s in symbols:
            t = hub.fetch_financial(_to_exchange(s), statement_type=table)
            if t is None or t.empty:
                continue
            frames.append(t)
        if not frames:
            return pd.DataFrame()
        import pandas as _pd

        fin = _pd.concat(frames, ignore_index=True)
    else:
        d = _find_dataset(table)
        if d is None:
            raise ValueError(f"未知财务表 {table}")
        import pandas as _pd

        files = sorted(d.glob("*.parquet"))
        if not files:
            return pd.DataFrame()
        frames = []
        for f in files:
            try:
                frames.append(_pd.read_parquet(f))
            except Exception:  # noqa: BLE001 - 单文件损坏跳过
                continue
        if not frames:
            return pd.DataFrame()
        fin = _pd.concat(frames, ignore_index=True)

    announce_col = next((name for name in ("m_anntime", "declareDate", "declare_date", "ann_date") if name in fin.columns), None)
    if announce_col is None:
        raise ValueError(f"{table} 缺少公告日期列，不能保证财务数据无前视")
    raw_dates = fin[announce_col].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
    if raw_dates.str.fullmatch(r"\d{8}").all():
        fin["_announce_date"] = pd.to_datetime(raw_dates, format="%Y%m%d", errors="coerce")
    else:
        fin["_announce_date"] = pd.to_datetime(raw_dates, errors="coerce")
    fin = fin.dropna(subset=["_announce_date"]).sort_values("_announce_date")
    sym_col = "Symbol" if "Symbol" in fin.columns else "symbol"
    fin["instrument"] = fin[sym_col].map(_to_qlib)
    if symbols is not None:
        fin = fin[fin["instrument"].isin(_norm_symbols(symbols) or set())]
    missing = [field for field in fields if field not in fin.columns]
    if missing:
        raise ValueError(f"{table} 无这些字段：{missing[:10]}（先用 columns('{table}') 查询）")
    if not asof:
        out = fin[["_announce_date", "instrument", *fields]].rename(columns={"_announce_date": "datetime"})
        return _finalize(out, table)
    dates = pd.bdate_range(start, end if end < "2100-01-01" else pd.Timestamp.today())
    if len(dates) == 0:
        raise ValueError(f"{table}: 交易日历为空（{start}~{end}）")
    f = fin[["_announce_date", "instrument", *fields]].rename(columns={"_announce_date": "datetime"})
    f = f.sort_values("datetime")
    grid = pd.MultiIndex.from_product(
        [dates, sorted(f["instrument"].unique())], names=["datetime", "instrument"]
    ).to_frame(index=False)
    merged = pd.merge_asof(
        grid.sort_values("datetime"), f, on="datetime", by="instrument", direction="backward"
    )
    return _finalize(merged, table)


def minute(
    dataset: str = "min5_kline",
    start: str | None = None,
    end: str | None = None,
    symbols: list[str] | None = None,
    freq: str = "1D",
) -> pd.DataFrame:
    """分钟线 → 按 ``freq`` 重采样后的宽表（默认聚合到日频）。"""
    if dataset not in ("min1_kline", "min5_kline"):
        raise ValueError("minute() 适用于 min1_kline / min5_kline")
    hub = _hub()
    syms = [_to_exchange(x) for x in symbols] if symbols else None
    if syms is None:
        raise ValueError("minute(): 全市场分钟线不可物化，请传 symbols 限定股票")
    parts = []
    for s in syms:
        t = hub.fetch_minute_kline(
            s,
            start=pd.Timestamp(start).date() if start else pd.Timestamp("2020-01-01").date(),
            end=pd.Timestamp(end).date() if end else pd.Timestamp.today().date(),
            freq="1min" if dataset == "min1_kline" else "5min",
        )
        if t is None or t.empty:
            continue
        t = t.copy()
        t["instrument"] = _to_qlib(s)
        parts.append(t)
    if not parts:
        return pd.DataFrame()
    raw = pd.concat(parts, ignore_index=True)
    raw["datetime"] = pd.to_datetime(raw["time"] if "time" in raw.columns else raw["trade_date"])
    agg_map = {"open": "first", "high": "max", "low": "min", "close": "last",
               "volume": "sum", "amount": "sum"}
    agg_map = {k: v for k, v in agg_map.items() if k in raw.columns}
    g = raw.set_index("datetime").groupby("instrument")
    pieces = []
    for col_name, fn in agg_map.items():
        ser = g[col_name].resample(freq).agg(fn)
        ser.name = col_name
        pieces.append(ser)
    out = pd.concat(pieces, axis=1).dropna(how="all").reset_index()
    return _finalize(out, dataset)


def members(name: str) -> pd.DataFrame:
    """指数成分（``index_weights``）静态快照（无生效日期，仅代表当前）。"""
    hub = _hub()
    return hub.fetch_index_weights(name)
