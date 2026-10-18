"""因子概率板：模型信号（PG/CSV）+ 证券主表/ST/板块成员装载。

symbol 口径：PG ``engine_signal_scores``（CN）是**裸 6 位码**，一律
``StockCodeUtil.to_suffix()`` 归一（920→BJ；前缀式也会被正过来）。
"""

from __future__ import annotations

import logging
from datetime import date as _date
from pathlib import Path

import pandas as pd

from backend.shared.factor_board import config as C
from backend.shared.stock_utils import StockCodeUtil

log = logging.getLogger("factor_board.signals")

MODEL_COLS = ["symbol", "fusion_score", "rank_pct", "signal_side"]


def normalize_symbol(value) -> str:
    return StockCodeUtil.to_suffix(str(value or "").strip())


def load_model_signals(
    *,
    csv_path: str | Path | None = None,
    model_date: str | None = None,
    stale_days: int = C.MODEL_STALE_DAYS,
) -> tuple[pd.DataFrame, dict]:
    """模型信号：index=suffix symbol，列 fusion_score/rank_pct/signal_side/trade_date。

    返回 (mdl, meta)。meta: trade_date/run_id/n/source/stale/requested_date。
    未指定 model_date 时取 PG 最新 batch/CN 交易日；CSV 模式须显式给 model_date。
    """
    if csv_path is not None:
        # dtype=str 锁 symbol：read_csv 默认把 "002062" 推断成 int64 → "2062"
        # → to_suffix 认不出（无 4 位号段），该股从模型覆盖静默消失（实测
        # SZ 0 头 ~1200 只全丢，模型覆盖 3191→2000，四象限随之失真）。
        df = pd.read_csv(csv_path, dtype={"symbol": str})
        miss = [c for c in MODEL_COLS if c not in df.columns]
        if miss:
            raise ValueError(f"CSV 缺列 {miss}（需 {MODEL_COLS}）")
        trade_date = (model_date or "").replace("-", "") or None
        if trade_date is None:
            raise ValueError("CSV 模式必须用 --model-date 指定信号目标交易日")
        meta = {
            "trade_date": trade_date,
            "run_id": None,
            "n": int(len(df)),
            "source": f"csv:{Path(csv_path).name}",
            "stale": False,
            "requested_date": trade_date,
        }
        return _finalize(df, meta)

    if model_date:
        trade_date = model_date.replace("-", "")
    else:
        trade_date = _latest_batch_date()
    from backend.shared.sync_db import sync_session
    from sqlalchemy import text

    with sync_session() as s:
        run_row = s.execute(
            text(
                "SELECT run_id FROM engine_signal_scores "
                "WHERE trade_date = :d AND source = :src AND market = :mkt "
                "ORDER BY signal_ts DESC NULLS LAST LIMIT 1"
            ),
            {"d": trade_date, "src": C.MODEL_SOURCE, "mkt": C.MODEL_MARKET},
        ).fetchone()
        run_id = run_row[0] if run_row else None
        sql = (
            "SELECT symbol, fusion_score, rank_pct, signal_side, run_id "
            "FROM engine_signal_scores "
            "WHERE trade_date = :d AND source = :src AND market = :mkt"
        )
        params = {"d": trade_date, "src": C.MODEL_SOURCE, "mkt": C.MODEL_MARKET}
        if run_id is not None:
            sql += " AND run_id = :rid"
            params["rid"] = run_id
        df = pd.read_sql(text(sql), s.connection(), params=params)

    stale = _staleness_days(trade_date) > stale_days
    meta = {
        "trade_date": trade_date,
        "run_id": run_id,
        "n": int(len(df)),
        "source": "pg",
        "stale": bool(stale),
        "requested_date": model_date.replace("-", "") if model_date else None,
    }
    if len(df) == 0:
        raise LookupError(f"engine_signal_scores 无 {trade_date} batch/CN 信号")
    return _finalize(df, meta)


def _latest_batch_date() -> str:
    from sqlalchemy import text

    from backend.shared.sync_db import sync_session

    with sync_session() as s:
        row = s.execute(
            text(
                "SELECT MAX(trade_date) FROM engine_signal_scores "
                "WHERE source = :src AND market = :mkt"
            ),
            {"src": C.MODEL_SOURCE, "mkt": C.MODEL_MARKET},
        ).fetchone()
    if not row or row[0] is None:
        raise LookupError("engine_signal_scores 无 batch/CN 信号")
    return str(row[0]).replace("-", "")


def _staleness_days(trade_date: str) -> int:
    d = pd.Timestamp(trade_date).date()
    return (_date.today() - d).days


def _finalize(df: pd.DataFrame, meta: dict) -> tuple[pd.DataFrame, dict]:
    df = df.copy()
    df["sym"] = df["symbol"].map(normalize_symbol)
    # 归一失败（如被截断的裸码 "2062"）不报错但必须可见：这些行后续与因子截面
    # 对不上号，会静默拉低模型覆盖。CN 号段=6 位数字 + .SH/.SZ/.BJ。
    bad = df[~df["sym"].str.match(r"^\d{6}\.(SH|SZ|BJ)$", na=False)]
    if len(bad):
        log.warning(
            "模型信号 symbol 归一失败 %d/%d 行（如前 %s）——这些标的将从模型覆盖中缺失",
            len(bad),
            len(df),
            bad["symbol"].head(3).tolist(),
        )
    df = df.drop_duplicates("sym", keep="last").set_index("sym")
    meta["n"] = int(len(df))
    meta["by_side"] = (
        df["signal_side"].value_counts().to_dict() if "signal_side" in df else {}
    )
    return df, meta


def load_instruments(quantdb_root: Path) -> tuple[pd.Series, pd.Series]:
    """(name_of, st_of)：index=suffix symbol。ST 判定沿用 v2 口径（名称含 ST）。"""
    p = (
        quantdb_root
        / "2_base_sector"
        / "instrument_detail"
        / "instrument_detail.parquet"
    )
    inst = pd.read_parquet(p)
    nm = inst["Name"].astype(str)
    nm.index = inst["Symbol"]
    return nm, nm.str.contains("ST", case=False, na=False)


def load_sector_members(quantdb_root: Path) -> pd.DataFrame:
    """板块成员关系（Symbol/SectorType/SectorName，suffix 式）。"""
    p = quantdb_root / "2_base_sector" / "sector_concept" / "sector_members.parquet"
    return pd.read_parquet(p)
