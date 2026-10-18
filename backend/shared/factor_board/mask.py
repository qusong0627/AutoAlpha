"""因子概率板：可交易性掩码读取（P2-d）。

掩码产物：``5_technical_derived/trade_mask/trade_mask.parquet``（稀疏表：
``dt(int32)/symbol(后缀式)/blocked_long/blocked_short``），由
``backend/scripts/build_trade_mask.py`` 全量+增量维护、beat 18:40 刷新。

**纪律：本模块不得 import ``factor_report.tradability``**——
``factor_report/__init__.py`` 顶层 ``from .router import router`` 连坐 fastapi，
宿主（无 fastapi 的解释器）直接 ImportError（2026-10-11 实测）。此处自读
parquet，读取/对齐语义与 ``tradability.load_mask/align_blocked`` 保持一致，
容器内由验收测试对拍（tests/test_factor_board_mask.py 有 fake 版；
实机对拍在 P2 验收清单）。

口径注记：掩码按 trade_date 近似——决策日实际可交易性以执行面风控为准
（l0.session/涨跌停/停牌闸在决策层另有实现）。
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd

from backend.shared.factor_board import config as C

log = logging.getLogger("factor_board.mask")

MASK_COLUMNS = ("dt", "symbol", "blocked_long", "blocked_short")


def resolve_mask_path(quantdb_root: Path) -> Path:
    """掩码产物路径（QuantDB 根 + TRADE_MASK_REL）。"""
    return Path(quantdb_root) / C.TRADE_MASK_REL


def load_mask(mask_path: Path) -> dict[int, tuple[frozenset[str], frozenset[str]]]:
    """读掩码 → ``{dt_int: (多头被挡集合, 空头被挡集合)}``。

    读不到/坏 → **空 dict**（调用方据此降级为「不过滤 + warning」），不抛。
    """
    p = Path(mask_path)
    if not p.exists():
        return {}
    try:
        import pyarrow.parquet as pq

        df = pq.read_table(p, columns=list(MASK_COLUMNS)).to_pandas()
    except Exception as exc:  # noqa: BLE001 — 掩码坏了不该拖垮产板
        log.warning("可交易掩码读取失败（%s）：%s", p, exc)
        return {}
    out: dict[int, tuple[frozenset[str], frozenset[str]]] = {}
    for dt_int, grp in df.groupby("dt", sort=False):
        syms = grp["symbol"].astype(str).to_numpy()
        out[int(dt_int)] = (
            frozenset(syms[grp["blocked_long"].to_numpy(dtype=bool)]),
            frozenset(syms[grp["blocked_short"].to_numpy(dtype=bool)]),
        )
    return out


def align_blocked(
    symbols: pd.Index,
    entry: tuple[frozenset[str], frozenset[str]] | None,
) -> tuple[np.ndarray, np.ndarray]:
    """把掩码集合对齐到**当日截面行序** → 两个全长度布尔数组。

    必须按 ``symbols`` 行序逐位对齐（错位会静默把 A 股的涨跌停状态扣到 B 股
    头上）。entry 为 None/空 → 返回全 False（等价「不过滤」，与
    tradability.align_blocked 的 None 语义在名单层等价——名单层全 False 即
    无剔除，且计数列显形）。
    """
    n = len(symbols)
    if not entry:
        return np.zeros(n, dtype=bool), np.zeros(n, dtype=bool)
    long_set, short_set = entry
    syms = np.asarray([str(s) for s in symbols], dtype=object)
    blk_long = np.fromiter((s in long_set for s in syms), dtype=bool, count=n)
    blk_short = np.fromiter((s in short_set for s in syms), dtype=bool, count=n)
    return blk_long, blk_short


def mask_columns(
    quantdb_root: Path,
    trade_date: str,
    index: pd.Index,
) -> tuple[pd.Series, pd.Series, dict, str | None]:
    """产板接线（builder 用）：当日掩码对齐到 ``index``（symbol 后缀式）。

    返回 ``(blocked_long, blocked_short, info, warning)``：

    - ``info = {"mask_date": int|None, "mask_max_dt": int|None, "stale_days": int|None,
      "missing": bool}``——mask_date 为命中行日期（=trade_date 或 None）；
    - 文件缺失/该日无行 → 全 False + warning（**不过滤**，对照
      build_factor_report 的 missing→降级惯例）；涨跌停/停牌是**当日**状态，
      绝不沿用旧日行（跨日沿用会把「昨日涨停、今日已开板」的名字错剔）；
    - 命中当日行 → 使用、无 warning（此时 stale_days ≤ 0 恒成立）；
    - 无当日行且掩码最新 dt 距今超 ``MASK_STALE_DAYS`` 天 → warning 升级为
      「陈旧」措辞（掩码停更的显形信号），结果同为不过滤。
    """
    mask_path = resolve_mask_path(quantdb_root)
    table = load_mask(mask_path)
    dt_int = int(trade_date)
    if not table:
        warning = f"可交易掩码缺失（{mask_path}）——名单层不过滤"
        info = {
            "mask_date": None,
            "mask_max_dt": None,
            "stale_days": None,
            "missing": True,
        }
        blk_l, blk_s = align_blocked(index, None)
        return (
            pd.Series(blk_l, index=index),
            pd.Series(blk_s, index=index),
            info,
            warning,
        )
    max_dt = max(table)
    stale_days = None
    try:
        stale_days = int(
            (pd.Timestamp(str(dt_int)).date() - pd.Timestamp(str(max_dt)).date()).days
        )
    except ValueError:
        stale_days = None
    entry = table.get(dt_int)
    if entry is None:
        if stale_days is not None and stale_days > C.MASK_STALE_DAYS:
            warning = (
                f"可交易掩码陈旧：无 {trade_date} 行且最新仅 {max_dt}"
                f"（距今 {stale_days} 天 >{C.MASK_STALE_DAYS}）——名单层不过滤"
            )
        else:
            warning = f"可交易掩码无 {trade_date} 行（最新 {max_dt}）——名单层不过滤"
    else:
        warning = None
    blk_l, blk_s = align_blocked(index, entry)
    info = {
        "mask_date": dt_int if entry is not None else None,
        "mask_max_dt": int(max_dt),
        "stale_days": stale_days,
        "missing": entry is None,
    }
    return pd.Series(blk_l, index=index), pd.Series(blk_s, index=index), info, warning
