"""
A 股全量股票主数据同步：从 QuantDB instrument_list.parquet 刷新 stocks 表。

背景：stocks 表由 baostock/akshare 种子脚本填充，但容器环境无外网时拉取失败，
导致表为空，推理明细/选股等 LEFT JOIN stocks.name 时股票名称恒为空。
QuantDB 本地 parquet（data/quantdb/2_base_sector/instrument_detail/instrument_list.parquet）
自带 Symbol(后缀格式 000001.SZ) ↔ Name 中文名 ↔ rs_hyname 行业 映射，离线可用。

幂等：UPSERT；重复运行安全。覆盖 SH/SZ/BJ 全市场（含北交所，可替代 sync_bj_stocks.py）。
运行：
  docker exec quantmind python3 /app/backend/scripts/sync_stocks_from_quantdb.py
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import pandas as pd
import psycopg2
from psycopg2.extras import execute_batch

logger = logging.getLogger("sync_stocks_from_quantdb")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

_EXCHANGE_BY_SUFFIX = {"SH": "SH", "SZ": "SZ", "BJ": "BJ"}


def _conn():
    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "quantmind-db"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        user=os.getenv("POSTGRES_USER", "quantmind"),
        password=os.getenv("POSTGRES_PASSWORD", "quantmind2026"),
        dbname=os.getenv("POSTGRES_DB", "quantmind"),
    )


def _find_instrument_list() -> Path | None:
    """定位 instrument_list.parquet（QuantDB 全量挂载优先，feature_snapshots 兜底）。"""
    base_dirs = [
        Path(os.getenv("QUANTDB_DATA_DIR", "/data/quantdb")),
        Path("/app/db/feature_snapshots"),
        Path("/tmp/quantdb_data"),
    ]
    for base in base_dirs:
        p = base / "2_base_sector" / "instrument_detail" / "instrument_list.parquet"
        if p.exists():
            return p
    return None


def main() -> None:
    src = _find_instrument_list()
    if src is None:
        logger.error("instrument_list.parquet 未找到（检查 QUANTDB_DATA_DIR）")
        return
    logger.info("使用数据源: %s", src)

    df = pd.read_parquet(src)
    for col in ("Symbol", "Name"):
        if col not in df.columns:
            logger.error("parquet 缺少 %s 列: %s", col, df.columns.tolist()[:8])
            return
    industry_col = "rs_hyname" if "rs_hyname" in df.columns else None

    rows: list[tuple[str, str, str, str]] = []
    seen: set[str] = set()
    for _, r in df.iterrows():
        sym = str(r.get("Symbol") or "").strip().upper()
        name = str(r.get("Name") or "").strip()
        if not sym or not name or sym in seen:
            continue
        suffix = sym.split(".")[-1] if "." in sym else ""
        if suffix not in _EXCHANGE_BY_SUFFIX:
            continue  # 只收 A 股三所规范后缀
        if not sym.endswith(f".{suffix}"):
            code = "".join(ch for ch in sym if ch.isdigit())
            if len(code) != 6:
                continue
            sym = f"{code}.{suffix}"
        industry = str(r.get(industry_col) or "").strip() if industry_col else ""
        seen.add(sym)
        rows.append((sym, name, suffix, industry))

    sql = """
        INSERT INTO stocks (symbol, name, exchange, industry, is_active)
        VALUES (%s, %s, %s, %s, TRUE)
        ON CONFLICT (symbol) DO UPDATE
        SET name = EXCLUDED.name,
            exchange = EXCLUDED.exchange,
            industry = CASE WHEN EXCLUDED.industry <> '' THEN EXCLUDED.industry ELSE stocks.industry END,
            is_active = TRUE,
            updated_at = NOW();
    """
    with _conn() as conn:
        with conn.cursor() as cur:
            execute_batch(cur, sql, rows, page_size=500)
        conn.commit()

    by_ex: dict[str, int] = {}
    for _, _, ex, _ in rows:
        by_ex[ex] = by_ex.get(ex, 0) + 1
    logger.info("股票主数据 UPSERT 完成: 共 %d 条 %s", len(rows), by_ex)
    logger.info("样例: %s", rows[:3])


if __name__ == "__main__":
    main()
