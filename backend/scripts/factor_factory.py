#!/usr/bin/env python3
"""因子工厂: QuantDB 富字段 × 算子 × 窗口（+ 二元组合）→ 组合因子 → IC 筛选 → 训练 parquet

与 alpha_library_factors.py 的区别:
  - alpha_library 只用 OHLCV 生成 429 个经典因子；
  - 因子工厂用 QuantDB 的几百个数值字段（l1_factors / features_daily 等）
    组合生成成千上万个「表达式因子」，再用 IC/ICIR 筛选、相关性去重，
    产出可训练 parquet（默认写用户自定义市场 quantcustom）。

性能设计:
  - 逐日秩相关 IC 全 numpy 向量化（无 pandas rank，数千因子分钟级）；
  - 两遍流式：pass1 逐字段生成并只保留指标（内存 O(一个字段)），
    pass2 只重算进入候选池的 top 因子再去重写盘；
  - zstd 压缩落盘，体积较 snappy 再降。

产物:
  <out>/dt=YYYYMMDD/data.parquet   (列: symbol(suffix) + date + OHLCV + 因子 float32)
  <out>/MANIFEST.csv                (factor_name, expression, ic, icir, coverage, kept)
  <out>/PROPOSALS.json              (表达式清单，供量化研究/特征目录导入)

用法（容器内）:
  python /app/backend/scripts/factor_factory.py --smoke
  python /app/backend/scripts/factor_factory.py --start-date 2025-09-01 --end-date 2026-08-31 --top-n 200
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import alpha_library_factors as alf  # noqa: E402  复用算子/写盘

log = logging.getLogger("factor_factory")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

EPS = 1e-12


# ---------------------------------------------------------------------------
# 1. 路径解析（容器 /data 优先，回退项目 data/）
# ---------------------------------------------------------------------------


def _resolve_dir(env_key: str, container: str, project_sub: str) -> Path:
    candidates = [os.getenv(env_key), container, str(PROJECT_ROOT / "data" / project_sub)]
    for c in candidates:
        if c and Path(c).is_dir():
            return Path(c)
    fallback = os.getenv(env_key) or container or str(PROJECT_ROOT / "data" / project_sub)
    return Path(fallback)


QUANTDB_ROOT = _resolve_dir("QM_QUANTDB_DATA_DIR", "/data/quantdb", "quantdb")
CUSTOM_ROOT = _resolve_dir("QM_QUANTCUSTOM_DATA_DIR", "/data/quantcustom", "quantcustom")

ML_DIR = QUANTDB_ROOT / "6_ml_datasets"


# ---------------------------------------------------------------------------
# 2. 基字段白名单（剔除 OHLCV/ID，取 QuantDB 预计算数值字段）
# ---------------------------------------------------------------------------

L1_FIELDS = [
    # 换手 / 流动性
    "turn_5", "turn_20", "turn_std_20", "turn_z_20", "turn_ratio_1_5",
    "turn_ratio_1_20", "turn_trend_5_20", "turn_acc_5",
    # 资金流
    "amt_net_flow_5", "amt_net_flow_20", "amt_z_20", "amt_ratio_1_5",
    "amt_ratio_5_20", "amt_skew_20", "amt_up_ratio_20", "amt_vol_ratio_20",
    "mfi_14", "obv_slope_20",
    # 动量
    "mom_ret_1d", "mom_ret_3d", "mom_ret_5d", "mom_ret_10d", "mom_ret_20d",
    "mom_ret_60d", "mom_ma_gap_5", "mom_ma_gap_20", "mom_macd_hist",
    "mom_rsi_14", "mom_kdj_k",
    # 波动率
    "vol_std_5", "vol_std_20", "vol_atr_14", "vol_parkinson_20", "vol_gk_20",
    "vol_amp_20",
    # 技术指标
    "tech_bb_width", "tech_bb_pos", "tech_cci_20", "tech_adx_14",
    "tech_close_to_high_20", "tech_max_drawdown_20",
    # 基本面 / 估值
    "fun_float_mv", "fun_total_mv", "fun_mv_rank", "fun_pe", "fun_pb",
    "fun_bp", "fun_ep", "fun_value_zscore", "fun_roe", "fun_peg", "fun_np_growth",
    # 筹码
    "chip_profit_ratio_20", "chip_profit_ratio_60", "chip_concentration_20",
    "chip_floating_ratio", "chip_cost_90_width", "chip_profit_delta_5",
    # 风格
    "style_beta_20", "style_beta_60", "style_idio_vol_20", "style_idio_vol_60",
    "style_residual_ret_20",
    # 行业
    "ind_ret_5", "ind_ret_20", "ind_rotation_speed_20", "ind_strength_20",
    "ind_dispersion_20", "ind_breadth_up_20", "ind_crowding_20",
    "ind_relative_momentum_20", "ind_relative_pe",
    # 概念
    "concept_hot_score", "concept_momentum_top3", "concept_rotation_score",
    "concept_crowding_max", "concept_flow_rank", "concept_leader_score",
]

FEATURES_FIELDS = [
    "rsi_14", "kdj_k", "kdj_d", "kdj_j", "macd_hist", "vol_atr_14", "beta_20",
    "pe_ttm", "pb", "ps_ttm", "dividend_rate", "total_mv", "float_mv",
    "net_profit_ttm", "revenue_ttm",
    # 2026-09 宽表新增（字符串存储，load_fields 会转数值；未同步时自动跳过）
    "hs_turnover", "seal_strength", "zaf", "beta_now",
    "dyna_pe", "static_pe_ttm", "div_yield", "pb_mrq",
    "ever_zt_count", "year_zt_days",
    "total_cap_yi", "float_mv_yi", "free_float_shares",
    "ipo_price", "zt_price", "dt_price",
]

DATASET_SOURCES = {
    "l1_factors": L1_FIELDS,
    "features_daily": FEATURES_FIELDS,
}

SMOKE_L1_FIELDS = [
    "turn_20", "turn_z_20", "amt_net_flow_20", "mfi_14", "mom_ret_20d",
    "mom_rsi_14", "vol_std_20", "vol_parkinson_20", "tech_bb_pos", "tech_adx_14",
    "fun_bp", "fun_roe", "chip_profit_ratio_20", "style_idio_vol_20",
    "ind_strength_20", "concept_hot_score",
]

# 二元组合的锚字段（覆盖价值/质量/成长/动量/波动/流动性/资金/风格/行业/概念）
BINARY_ANCHORS = [
    "fun_bp", "fun_ep", "fun_roe", "fun_np_growth", "fun_peg",
    "mom_ret_20d", "mom_ret_60d", "vol_std_20", "turn_20", "mfi_14",
    "amt_net_flow_20", "style_idio_vol_20", "ind_strength_20", "concept_hot_score",
]


# ---------------------------------------------------------------------------
# 3. 算子定义
# ---------------------------------------------------------------------------

WINDOW_OPS = [
    ("tsrank", lambda x, w: alf.TSRANK(x, w), lambda f, w: f"TSRANK(${f},{w})"),
    ("tsstd", lambda x, w: alf.STD(x, w), lambda f, w: f"STD(${f},{w})"),
    ("delta", lambda x, w: x - x.shift(w), lambda f, w: f"(${f} - Ref(${f},{w}))"),
    ("roc", lambda x, w: x / x.shift(w) - 1.0, lambda f, w: f"(${f} / Ref(${f},{w}) - 1)"),
    (
        "zscore",
        lambda x, w: (x - alf.MEAN(x, w)) / (alf.STD(x, w) + EPS),
        lambda f, w: f"((${f} - MEAN(${f},{w})) / (STD(${f},{w}) + 1e-12))",
    ),
    ("decay", lambda x, w: alf.DECAY(x, w), lambda f, w: f"DECAYLINEAR(${f},{w})"),
    (
        "slope",
        lambda x, w: alf.REG(x, w)[0]
        / (x.abs().rolling(w, min_periods=w).mean() + EPS),
        lambda f, w: f"(SLOPE(${f},{w}) / (MEAN(ABS(${f}),{w}) + 1e-12))",
    ),
]

CS_OPS = [
    ("csrank", lambda x: alf.R(x), lambda f: f"RANK(${f})"),
    (
        "cszscore",
        lambda x: x.sub(x.mean(axis=1), axis=0).div(x.std(axis=1) + EPS, axis=0),
        lambda f: f"ZSCORE(${f})",
    ),
]

OP_BY_NAME = {name: (fn, expr) for name, fn, expr in WINDOW_OPS}
CS_OP_BY_NAME = {name: (fn, expr) for name, fn, expr in CS_OPS}

_SANITIZE = re.compile(r"[^a-zA-Z0-9_]+")


def _feature_name(op: str, field: str, window: int | None = None) -> str:
    base = f"ff_{op}{window}_{field}" if window else f"ff_{op}_{field}"
    return _SANITIZE.sub("_", base).lower()[:64]


# ---------------------------------------------------------------------------
# 4. 数据加载
# ---------------------------------------------------------------------------


def _parse_dt(series: pd.Series) -> pd.Series:
    s = series.astype(str)
    out = pd.to_datetime(s, format="%Y%m%d", errors="coerce")
    bad = out.isna()
    if bad.any():
        out.loc[bad] = pd.to_datetime(s[bad], errors="coerce")
    return out


def load_fields(
    dataset: str,
    fields: list[str],
    *,
    start_year: int | None = None,
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
    max_symbols: int | None = None,
) -> dict[str, pd.DataFrame]:
    """读取 6_ml_datasets/<dataset> 指定列 → {field: 宽表(index=time, cols=symbol)}。"""
    glob = str(ML_DIR / dataset / "dt=*" / "data.parquet")
    if not list(ML_DIR.glob(f"{dataset}/dt=*")):
        log.warning("数据集无分区，跳过: %s", glob)
        return {}
    con = duckdb.connect()
    try:
        # 先探 schema：只请求实际存在的列，避免尚未同步的新字段（如 2026-09 新增）
        # 导致整条 SELECT 因 BinderException 失败。
        try:
            schema = con.execute(
                f"DESCRIBE SELECT * FROM read_parquet('{glob}', hive_partitioning=true, union_by_name=true)"
            ).fetchdf()
            available = {str(c) for c in schema["column_name"]}
        except Exception as exc:  # noqa: BLE001
            log.warning("读取 %s schema 失败，按请求列尝试: %s", dataset, exc)
            available = set(fields)
        use_fields = [f for f in fields if f in available]
        if not use_fields:
            log.warning("%s 无可用请求列（可能尚未同步该字段）", dataset)
            return {}

        cols = ", ".join(["symbol", "dt"] + [f'"{f}"' for f in use_fields])
        q = f"SELECT {cols} FROM read_parquet('{glob}', hive_partitioning=true, union_by_name=true)"
        conds = []
        if start_year:
            conds.append(f"CAST(dt AS VARCHAR) >= '{start_year}0101'")
        if start is not None:
            conds.append(f"CAST(dt AS VARCHAR) >= '{start.strftime('%Y%m%d')}'")
        if end is not None:
            conds.append(f"CAST(dt AS VARCHAR) <= '{end.strftime('%Y%m%d')}'")
        if conds:
            q += " WHERE " + " AND ".join(conds)
        df = con.execute(q).fetchdf()
    finally:
        con.close()
    if df.empty:
        return {}
    df["_dt"] = _parse_dt(df["dt"])
    df["symbol"] = df["symbol"].astype(str)
    df = df.dropna(subset=["_dt"]).drop_duplicates(subset=["symbol", "_dt"])
    if max_symbols:
        syms = sorted(df["symbol"].unique())[:max_symbols]
        df = df[df["symbol"].isin(syms)]

    out: dict[str, pd.DataFrame] = {}
    for f in use_fields:
        if f not in df.columns:
            continue
        wide = df.pivot_table(index="_dt", columns="symbol", values=f, aggfunc="last")
        wide = wide.sort_index()
        # 新字段可能是字符串存储 → 先转数值（非法转 NaN），再统一 float32
        wide = wide.apply(pd.to_numeric, errors="coerce")
        if wide.notna().to_numpy().any():
            out[f] = wide.astype("float32")
    return out


def load_close(
    *,
    start_year: int | None = None,
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
    max_symbols: int | None = None,
) -> pd.DataFrame:
    return load_ohlcv(
        ["close"], start_year=start_year, start=start, end=end, max_symbols=max_symbols
    )["close"]


def load_ohlcv(
    cols: list[str],
    *,
    start_year: int | None = None,
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
    max_symbols: int | None = None,
) -> dict[str, pd.DataFrame]:
    """daily_forward 行情宽表 {col: index=time, cols=symbol}。

    CUSTOM 市场的训练读取器需要因子源自带 OHLCV，故工厂产物需内联这些列。
    """
    glob = str(QUANTDB_ROOT / "1_kline_data" / "daily_forward" / "dt=*" / "data.parquet")
    sel = ", ".join(["symbol", "time"] + [f'"{c}"' for c in cols])
    q = f"SELECT {sel} FROM read_parquet('{glob}', hive_partitioning=true)"
    conds = []
    if start_year:
        conds.append(f"year(time) >= {start_year}")
    if start is not None:
        conds.append(f"time >= TIMESTAMP '{start.strftime('%Y-%m-%d')}'")
    if end is not None:
        conds.append(f"time <= TIMESTAMP '{end.strftime('%Y-%m-%d')}'")
    if conds:
        q += " WHERE " + " AND ".join(conds)
    con = duckdb.connect()
    try:
        df = con.execute(q).fetchdf()
    finally:
        con.close()
    df["time"] = pd.to_datetime(df["time"])
    df["symbol"] = df["symbol"].astype(str)
    df = df.drop_duplicates(subset=["symbol", "time"])
    if max_symbols:
        syms = sorted(df["symbol"].unique())[:max_symbols]
        df = df[df["symbol"].isin(syms)]
    out: dict[str, pd.DataFrame] = {}
    for c in cols:
        if c in df.columns:
            out[c] = df.pivot(index="time", columns="symbol", values=c).sort_index()
    return out


# ---------------------------------------------------------------------------
# 5. 因子生成（逐字段 + 二元组合）
# ---------------------------------------------------------------------------


def generate_field_factors(
    x: pd.DataFrame,
    field: str,
    *,
    windows: list[int],
    ops: list[str],
    cs_ops: list[str],
    only: set[str] | None = None,
) -> dict[str, tuple[pd.DataFrame, str]]:
    """对单字段生成 {feature_name: (values, expression)}；only 给定时只算其中因子。"""
    out: dict[str, tuple[pd.DataFrame, str]] = {}
    for op in cs_ops:
        if op not in CS_OP_BY_NAME:
            continue
        name = _feature_name(op, field)
        if only is not None and name not in only:
            continue
        fn, expr = CS_OP_BY_NAME[op]
        try:
            out[name] = (fn(x).astype(np.float32), expr(field))
        except Exception as exc:  # noqa: BLE001
            log.debug("op %s on %s failed: %s", op, field, exc)
    for op in ops:
        if op not in OP_BY_NAME:
            continue
        fn, expr = OP_BY_NAME[op]
        for w in windows:
            name = _feature_name(op, field, w)
            if only is not None and name not in only:
                continue
            try:
                out[name] = (fn(x, w).astype(np.float32), expr(field, w))
            except Exception as exc:  # noqa: BLE001
                log.debug("op %s(w=%d) on %s failed: %s", op, w, field, exc)
    return out


def _binary_pairs(anchors: list[str], base: dict[str, pd.DataFrame]) -> list[tuple[str, str]]:
    avail = [a for a in anchors if a in base]
    return [(a, b) for i, a in enumerate(avail) for b in avail[i + 1:]]


def generate_binary_factors(
    base: dict[str, pd.DataFrame],
    anchors: list[str],
    *,
    windows: list[int],
    ops: tuple[str, ...] = ("csdiff", "csratio", "tscorr"),
    only: set[str] | None = None,
    pairs: list[tuple[str, str]] | None = None,
) -> dict[str, tuple[pd.DataFrame, str]]:
    """锚字段两两组合：截面差/比值/滚动相关。only 给定时只算其中因子。"""
    if pairs is None:
        pairs = _binary_pairs(anchors, base)
    out: dict[str, tuple[pd.DataFrame, str]] = {}
    for a, b in pairs:
        xa, xb = base[a], base[b]
        if "csdiff" in ops:
            name = _feature_name("bincsdiff", f"{a}_{b}")
            if only is None or name in only:
                out[name] = (
                    (alf.R(xa) - alf.R(xb)).astype(np.float32),
                    f"(RANK(${a}) - RANK(${b}))",
                )
        if "csratio" in ops:
            name = _feature_name("bincsratio", f"{a}_{b}")
            if only is None or name in only:
                out[name] = (
                    (alf.R(xa) / (alf.R(xb) + EPS)).astype(np.float32),
                    f"(RANK(${a}) / (RANK(${b}) + 1e-12))",
                )
        if "tscorr" in ops:
            for w in windows:
                name = _feature_name("bintscorr", f"{a}_{b}", w)
                if only is not None and name not in only:
                    continue
                out[name] = (
                    alf.CORR(xa, xb, w).astype(np.float32),
                    f"CORR(${a}, ${b}, {w})",
                )
    return out


# ---------------------------------------------------------------------------
# 6. 向量化逐日秩相关 IC
# ---------------------------------------------------------------------------


def _rank_rows(a: np.ndarray) -> np.ndarray:
    """按行平均秩（NaN 保持 NaN）。全向量化，无逐日 Python 循环。a: (n_days, n_syms)。"""
    d, n = a.shape
    valid = np.isfinite(a)
    aa = np.where(valid, a, np.inf)
    order = np.argsort(aa, axis=1, kind="mergesort")
    a_sorted = np.take_along_axis(aa, order, axis=1)
    j = np.arange(n)
    change = np.ones((d, n), dtype=bool)
    if n > 1:
        change[:, 1:] = a_sorted[:, 1:] != a_sorted[:, :-1]
    start_sorted = np.maximum.accumulate(np.where(change, j, 0), axis=1)
    bwd = np.where(change, j, 10**9)
    nxt_from_right = np.minimum.accumulate(bwd[:, ::-1], axis=1)[:, ::-1]
    nxt_strict = np.concatenate([nxt_from_right[:, 1:], np.full((d, 1), 10**9)], axis=1)
    end_sorted = np.minimum(nxt_strict, n) - 1
    avg = (start_sorted + end_sorted + 2) / 2.0  # 1-based 平均秩
    ranks = np.empty((d, n), dtype=np.float64)
    ranks[np.arange(d)[:, None], order] = avg
    ranks[~valid] = np.nan
    return ranks


def _rank_ic_from_ranks(rf: np.ndarray, rr: np.ndarray) -> np.ndarray:
    """由两侧秩矩阵计算逐日秩相关（Spearman = 秩的 Pearson）。"""
    m = np.isfinite(rf) & np.isfinite(rr)
    cnt = m.sum(axis=1).astype(np.float64)
    den = np.maximum(cnt, 1.0)
    fm = np.where(m, rf, 0.0).sum(axis=1) / den
    rm = np.where(m, rr, 0.0).sum(axis=1) / den
    fc = np.where(m, rf - fm[:, None], 0.0)
    rc = np.where(m, rr - rm[:, None], 0.0)
    cov = (fc * rc).sum(axis=1) / den
    vf = (fc**2).sum(axis=1) / den
    vr = (rc**2).sum(axis=1) / den
    sd = np.sqrt(vf * vr)
    ic = np.where(sd > 1e-12, cov / np.where(sd > 1e-12, sd, 1.0), np.nan)
    keep = (cnt >= 5) & np.isfinite(ic)
    return ic[keep]


def screen_one(
    name: str,
    fac: pd.DataFrame,
    fwd_rank: np.ndarray,
    expr: str,
    field: str,
    *,
    min_coverage: float,
) -> dict:
    vals = fac.to_numpy(dtype=np.float32)
    coverage = float(np.isfinite(vals).mean())
    if coverage < min_coverage or vals.shape[0] != fwd_rank.shape[0]:
        return {"factor_name": name, "expression": expr, "field": field,
                "ic": np.nan, "icir": np.nan, "coverage": coverage, "n_ic_days": 0}
    ic = _rank_ic_from_ranks(_rank_rows(vals), fwd_rank)
    ic_mean = float(ic.mean()) if ic.size else np.nan
    ic_std = float(ic.std(ddof=1)) if ic.size > 1 else np.nan
    icir = float(ic_mean / ic_std) if ic_std and ic_std > 0 else 0.0
    return {"factor_name": name, "expression": expr, "field": field,
            "ic": ic_mean, "icir": icir, "coverage": coverage, "n_ic_days": int(ic.size)}


# ---------------------------------------------------------------------------
# 7. 相关性去重（先按日期采样再堆叠，控内存）
# ---------------------------------------------------------------------------


def dedup_by_correlation(
    pool: pd.DataFrame,
    factors: dict[str, tuple[pd.DataFrame, str, str]],
    close: pd.DataFrame,
    *,
    top_n: int,
    corr_threshold: float = 0.85,
    max_samples: int = 20000,
    score_col: str = "ic",
) -> pd.DataFrame:
    """候选池内按 |score| 排序后贪心去重（|corr|>阈值丢弃），保留 top_n。"""
    pool = pool.dropna(subset=["ic"])
    if pool.empty:
        return pool.head(0)
    n_syms = max(1, close.shape[1])
    max_dates = max(5, min(len(close.index), max_samples // n_syms))
    step = max(1, len(close.index) // max_dates)
    dates = close.index[::step][:max_dates]

    mat = {}
    for name in pool["factor_name"]:
        if name not in factors:
            continue
        s = factors[name][0].reindex(index=dates, columns=close.columns)
        mat[name] = s.to_numpy(dtype=np.float32).ravel()
    stack = pd.DataFrame(mat)
    corr = stack.corr(min_periods=200)

    kept: list[str] = []
    for name in pool["factor_name"]:
        if len(kept) >= top_n or name not in corr.index:
            continue
        if all(
            not (np.isfinite(corr.at[name, k]) and abs(corr.at[name, k]) > corr_threshold)
            for k in kept
        ):
            kept.append(name)
    log.info("dedup: %d pool → %d kept (threshold=%.2f)", len(pool), len(kept), corr_threshold)
    return pool.set_index("factor_name").loc[kept].reset_index()


# ---------------------------------------------------------------------------
# 8. 写盘
# ---------------------------------------------------------------------------


def write_factor_partitions(
    factors: dict[str, tuple[pd.DataFrame, str, str]],
    names: list[str],
    *,
    out_root: Path,
    start_dt: str = "20160101",
    rebuild: bool = False,
    ohlcv: dict[str, pd.DataFrame] | None = None,
    compression: str = "zstd",
) -> int:
    """按日写 <out_root>/dt=YYYYMMDD/data.parquet（symbol(suffix)+date+OHLCV+因子, float32）。"""
    frames = [factors[n][0] for n in names]
    dates = frames[0].index
    syms = list(frames[0].columns)
    out_root.mkdir(parents=True, exist_ok=True)

    ohlcv_arrs = [
        (v.reindex(index=dates, columns=syms).values, c) for c, v in (ohlcv or {}).items()
    ]
    written = 0
    chunk = 40
    n = len(dates)
    arrs = [f.values for f in frames]
    for b in range(0, n, chunk):
        b_end = min(b + chunk, n)
        block = np.stack([a[b:b_end] for a in arrs], axis=2)
        for k in range(b, b_end):
            dt_str = pd.Timestamp(dates[k]).strftime("%Y%m%d")
            if dt_str < start_dt:
                continue
            target = out_root / f"dt={dt_str}" / "data.parquet"
            if target.exists() and not rebuild:
                continue
            day = pd.DataFrame(block[k - b], index=syms, columns=names)
            day = day.replace([np.inf, -np.inf], np.nan)
            day = day.reset_index().rename(columns={"index": "symbol"})
            day.insert(1, "date", pd.Timestamp(dates[k]))
            for arr, c in ohlcv_arrs:
                day[c] = arr[k].astype("float32")
            for c in names:
                day[c] = day[c].astype("float32")
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.parent / ".tmp-data.parquet"
            try:
                day.to_parquet(tmp, index=False, compression=compression)
                tmp.replace(target)
                written += 1
            finally:
                tmp.unlink(missing_ok=True)
        del block
    log.info("wrote %d partitions → %s", written, out_root)
    return written


# ---------------------------------------------------------------------------
# 9. 主流程
# ---------------------------------------------------------------------------


def _select_fields(smoke: bool, limit: int | None) -> list[tuple[str, str]]:
    if smoke:
        l1 = SMOKE_L1_FIELDS
        fd: list[str] = ["rsi_14", "macd_hist", "pe_ttm", "pb"]
    else:
        l1 = L1_FIELDS
        fd = FEATURES_FIELDS
    pairs: list[tuple[str, str]] = [("l1_factors", f) for f in l1]
    seen = set(l1)
    pairs += [("features_daily", f) for f in fd if f not in seen]
    if limit:
        pairs = pairs[:limit]
    return pairs


def _slice(df: pd.DataFrame, start: pd.Timestamp | None, end: pd.Timestamp | None) -> pd.DataFrame:
    if start is None and end is None:
        return df
    idx = df.index
    mask = np.ones(len(idx), dtype=bool)
    if start is not None:
        mask &= idx >= start
    if end is not None:
        mask &= idx <= end
    return df.loc[mask]


# pass1 并行上下文：fork 子进程通过 COW 继承 base，无需序列化大对象
_P1: dict = {}


def _field_worker(job) -> list[dict]:
    """单字段 / 二元组合分片生成 + 筛选，返回指标行。

    job 为字段名(str)，或 ("bin", start, end) 表示二元对分片。
    """
    start, end = _P1["start"], _P1["end"]
    if isinstance(job, tuple):
        _, s, e = job
        facs = generate_binary_factors(
            _P1["base"], _P1["anchors"],
            windows=_P1["windows"], ops=_P1["binary_ops"],
            pairs=_P1["bin_pairs"][s:e],
        )
        field = "binary"
    else:
        facs = generate_field_factors(
            _P1["base"][job], job,
            windows=_P1["windows"], ops=_P1["ops"], cs_ops=_P1["cs_ops"],
        )
        field = job
    return [
        screen_one(name, _slice(v, start, end), _P1["fwd_rank"], expr, field,
                   min_coverage=_P1["min_cov"])
        for name, (v, expr) in facs.items()
    ]


def _select_pool_fields(screened: pd.DataFrame, pool_names: set[str]) -> dict[str, set[str]]:
    """pool 因子按 field 归组，便于 pass2 只重算需要的字段/因子。"""
    by_field: dict[str, set[str]] = {}
    for r in screened.itertuples():
        if r.factor_name in pool_names:
            by_field.setdefault(r.field, set()).add(r.factor_name)
    return by_field


def run(args: argparse.Namespace) -> None:
    t0 = time.time()
    windows = [int(w) for w in str(args.windows).split(",") if w.strip()]
    ops = [o.strip() for o in str(args.ops).split(",") if o.strip()]
    cs_ops = [o.strip() for o in str(args.cs_ops).split(",") if o.strip()]
    binary_ops = tuple(o.strip() for o in str(args.binary_ops).split(",") if o.strip())
    pairs = _select_fields(args.smoke, args.limit_fields)

    start_ts = pd.Timestamp(args.start_date) if args.start_date else None
    end_ts = pd.Timestamp(args.end_date) if args.end_date else None
    warmup = (max(windows) + 10) if windows else 70
    load_start = (start_ts - pd.Timedelta(days=warmup)) if start_ts is not None else None
    log.info(
        "mode=%s fields=%d windows=%s ops=%s cs_ops=%s binary=%s range=%s~%s warmup=%dd score=%s",
        "SMOKE" if args.smoke else "FULL", len(pairs), windows, ops, cs_ops,
        list(binary_ops), start_ts.date() if start_ts is not None else "-",
        end_ts.date() if end_ts is not None else "-", warmup, args.score,
    )

    # 1) 加载基字段（含 warmup）
    by_dataset: dict[str, list[str]] = {}
    for ds, f in pairs:
        by_dataset.setdefault(ds, []).append(f)
    base: dict[str, pd.DataFrame] = {}
    for ds, fs in by_dataset.items():
        base.update(load_fields(
            ds, fs, start_year=args.start_year, start=load_start, end=end_ts,
            max_symbols=args.max_symbols,
        ))
    log.info("loaded %d base fields (%.0fs)", len(base), time.time() - t0)

    close = load_close(
        start_year=args.start_year, start=load_start, end=end_ts, max_symbols=args.max_symbols
    )
    base = {k: v.reindex(index=close.index, columns=close.columns) for k, v in base.items()}
    fields = [f for _, f in pairs if f in base]

    close_win = _slice(close, start_ts, end_ts)
    fwd_win = (close_win.shift(-args.horizon) / close_win - 1.0).to_numpy(dtype=np.float32)
    fwd_rank = _rank_rows(fwd_win)  # 前瞻收益秩只算一次，全因子复用

    # 2) pass1：逐字段（可选多进程）生成 + 只保留指标（内存 O(字段)）
    n_jobs = args.jobs or min(8, os.cpu_count() or 4)
    bin_pairs = _binary_pairs(BINARY_ANCHORS, base) if binary_ops else []
    _P1.update(
        base=base, start=start_ts, end=end_ts, fwd_rank=fwd_rank,
        windows=windows, ops=ops, cs_ops=cs_ops, binary_ops=binary_ops,
        anchors=BINARY_ANCHORS, min_cov=args.min_coverage, bin_pairs=bin_pairs,
    )
    jobs: list = list(fields)
    if bin_pairs:
        step = max(1, (len(bin_pairs) + n_jobs - 1) // n_jobs)
        jobs += [
            ("bin", i, min(i + step, len(bin_pairs)))
            for i in range(0, len(bin_pairs), step)
        ]
        log.info("binary pairs: %d → %d chunk jobs", len(bin_pairs), len(jobs) - len(fields))
    rows: list[dict] = []
    if n_jobs > 1 and len(jobs) > 1:
        import multiprocessing as mp

        try:
            ctx = mp.get_context("fork")
        except ValueError:
            ctx = None
        if ctx is not None:
            with ctx.Pool(processes=n_jobs) as pool_proc:
                for i, res in enumerate(pool_proc.imap_unordered(_field_worker, jobs), 1):
                    rows.extend(res)
                    if i % 10 == 0 or i == len(jobs):
                        log.info("pass1 progress %d/%d jobs (%.0fs)", i, len(jobs), time.time() - t0)
        else:
            for job in jobs:
                rows.extend(_field_worker(job))
    else:
        for i, job in enumerate(jobs, 1):
            rows.extend(_field_worker(job))
            if i % 10 == 0 or i == len(jobs):
                log.info("pass1 progress %d/%d jobs (%.0fs)", i, len(jobs), time.time() - t0)
    screened = pd.DataFrame(rows)
    if screened.empty:
        log.warning("no candidates; abort")
        return
    screened["abs_score"] = screened[args.score].abs()
    log.info("pass1 done: %d candidates (mean |IC|=%.4f, %.0fs)",
             len(screened), screened["ic"].abs().mean(), time.time() - t0)

    # 3) 选池
    cand = screened.dropna(subset=["ic"]).loc[
        screened["coverage"] >= args.min_coverage
    ].sort_values("abs_score", ascending=False)
    pool = cand.head(max(args.top_n, int(args.top_n * args.pool_factor)))
    pool_names = set(pool["factor_name"])
    log.info("pool selected: %d (top_n=%d × pool_factor=%.1f)", len(pool), args.top_n, args.pool_factor)

    # 4) pass2：只重算候选池涉及的字段/因子 → 去重 → 写盘
    pool_by_field = _select_pool_fields(screened, pool_names)
    factors: dict[str, tuple[pd.DataFrame, str, str]] = {}
    for field, names in pool_by_field.items():
        if field == "binary":
            facs = generate_binary_factors(
                base, BINARY_ANCHORS, windows=windows, ops=binary_ops, only=names
            )
        else:
            facs = generate_field_factors(
                base[field], field, windows=windows, ops=ops, cs_ops=cs_ops, only=names
            )
        for name, (v, expr) in facs.items():
            if name in names:
                factors[name] = (_slice(v, start_ts, end_ts), expr, field)
    log.info("pass2 recomputed %d factors from %d fields", len(factors), len(pool_by_field))

    kept = dedup_by_correlation(
        pool, factors, close_win,
        top_n=args.top_n, corr_threshold=args.corr_threshold, score_col=args.score,
    )
    kept = kept.sort_values("abs_score", ascending=False)
    kept["kept"] = True
    kept_names = [n for n in kept["factor_name"] if n in factors]
    log.info("final kept: %d", len(kept_names))

    out_root = Path(args.out) if args.out else (CUSTOM_ROOT / "6_ml_datasets" / "l1_factors")
    start_dt = args.start_dt or (start_ts.strftime("%Y%m%d") if start_ts is not None else "20160101")

    if args.dry_run:
        log.info("dry-run: skip partition write")
    elif kept_names:
        out_root.mkdir(parents=True, exist_ok=True)
        ohlcv = load_ohlcv(
            ["open", "high", "low", "close", "volume", "amount"],
            start_year=args.start_year, start=load_start, end=end_ts, max_symbols=args.max_symbols,
        )
        n = write_factor_partitions(
            factors, kept_names, out_root=out_root, start_dt=start_dt,
            rebuild=args.rebuild, ohlcv=ohlcv, compression=args.compression,
        )
        log.info("partitions: %d", n)
        kept.to_csv(out_root / "MANIFEST.csv", index=False, encoding="utf-8")
        (out_root / "PROPOSALS.json").write_text(
            json.dumps(
                [{"name": r.factor_name, "expression": r.expression,
                  "ic": None if pd.isna(r.ic) else round(float(r.ic), 5),
                  "icir": None if pd.isna(r.icir) else round(float(r.icir), 5),
                  "coverage": round(float(r.coverage), 4)}
                 for r in kept.itertuples()],
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
    log.info("DONE in %.0fs (kept=%d, out=%s)", time.time() - t0, len(kept_names), out_root)


def main() -> None:
    ap = argparse.ArgumentParser(description="QuantDB 因子工厂")
    ap.add_argument("--smoke", action="store_true", help="冒烟: 小字段集 × 小窗口")
    ap.add_argument("--max-symbols", type=int, default=None)
    ap.add_argument("--start-year", type=int, default=None)
    ap.add_argument("--start-date", default=None, help="目标窗口起始 YYYY-MM-DD")
    ap.add_argument("--end-date", default=None, help="目标窗口结束 YYYY-MM-DD")
    ap.add_argument("--limit-fields", type=int, default=None)
    ap.add_argument("--windows", default="5,10,20,60")
    ap.add_argument("--ops", default="tsrank,tsstd,roc,zscore,delta,decay,slope")
    ap.add_argument("--cs-ops", default="csrank,cszscore")
    ap.add_argument("--binary-ops", default="csdiff,csratio,tscorr", help="二元组合算子；传空禁用")
    ap.add_argument("--max-candidates", type=int, default=None)
    ap.add_argument("--jobs", type=int, default=0, help="pass1 并行进程数（0=自动, 1=串行）")
    ap.add_argument("--top-n", type=int, default=200, help="IC 去重后保留因子数")
    ap.add_argument("--pool-factor", type=float, default=3.0, help="候选池 = top_n × pool_factor")
    ap.add_argument("--score", default="ic", choices=["ic", "icir"], help="排序/去重依据")
    ap.add_argument("--horizon", type=int, default=1, help="前瞻收益天数")
    ap.add_argument("--screen-days", type=int, default=500)
    ap.add_argument("--min-coverage", type=float, default=0.5)
    ap.add_argument("--corr-threshold", type=float, default=0.85)
    ap.add_argument("--compression", default="zstd", help="parquet 压缩: zstd/snappy/none")
    ap.add_argument("--start-dt", default="20160101")
    ap.add_argument("--out", default=None, help="输出根目录（默认 quantcustom/6_ml_datasets/l1_factors）")
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    if args.smoke:
        args.max_symbols = args.max_symbols or 50
        args.start_year = args.start_year or 2024
        args.windows = "20"
        args.ops = "tsrank,roc,zscore,delta"
        args.binary_ops = "csdiff"
    run(args)


if __name__ == "__main__":
    main()
