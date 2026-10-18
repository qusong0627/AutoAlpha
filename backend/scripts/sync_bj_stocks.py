"""
北交所股票补充脚本：从 QuantDB instrument_detail.parquet 提取北交所股票
(代码 920xxx / 8xxxxx / 4xxxxx) 插入 stocks 表。

背景：stocks 表由 baostock 种子脚本填充，但 baostock 不覆盖北交所，
导致推理研究/选股等 join stocks.name 时北交所股票名称为空。

幂等：UPSERT；重复运行安全。
运行：
  docker exec quantmind python3 /app/backend/scripts/sync_bj_stocks.py
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import pandas as pd
import psycopg2
from psycopg2.extras import execute_batch

logger = logging.getLogger("sync_bj_stocks")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

_BJ_SYMBOL_PATTERN = r"^(BJ|[489][0-9]{5})"


def _conn():
    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "quantmind-db"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        user=os.getenv("POSTGRES_USER", "quantmind"),
        password=os.getenv("POSTGRES_PASSWORD", "quantmind2026"),
        dbname=os.getenv("POSTGRES_DB", "quantmind"),
    )


def _find_instrument_detail() -> Path | None:
    """定位 instrument_detail.parquet（QuantDB 全量挂载优先，feature_snapshots 兜底）。"""
    candidates = [
        Path(os.getenv("QUANTDB_DATA_DIR", "/tmp/quantdb_data")) / "2_base_sector" / "instrument_detail" / "instrument_list.parquet",
        Path(os.getenv("QUANTDB_DATA_DIR", "/tmp/quantdb_data")) / "2_base_sector" / "instrument_detail" / "instrument_detail.parquet",
        Path("/app/db/feature_snapshots") / "2_base_sector" / "instrument_detail" / "instrument_list.parquet",
        Path("/app/db/feature_snapshots") / "2_base_sector" / "instrument_detail" / "instrument_detail.parquet",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def main() -> None:
    src = _find_instrument_detail()
    if src is None:
        logger.error("instrument_detail.parquet 未找到")
        return

    df = pd.read_parquet(src)
    if "Symbol" not in df.columns or "Name" not in df.columns:
        logger.error("instrument_detail.parquet 缺少 Symbol/Name 列: %s", df.columns.tolist()[:5])
        return

    bj = df[df["Symbol"].astype(str).str.match(_BJ_SYMBOL_PATTERN, na=False)].copy()
    if bj.empty:
        logger.info("无北交所股票记录")
        return

    rows = []
    for _, r in bj.iterrows():
        sym = str(r["Symbol"]).strip().upper()
        name = str(r["Name"] or "").strip()
        if not sym or not name:
            continue
        # 归一化：920000.BJ → 920000.BJ（保持 suffix 格式，与 SH/SZ 一致）
        if not sym.endswith(".BJ"):
            code = "".join(ch for ch in sym if ch.isdigit())
            if code:
                sym = f"{code}.BJ"
        rows.append((sym, name, "BJ"))

    sql = """
        INSERT INTO stocks (symbol, name, exchange, is_active)
        VALUES (%s, %s, %s, TRUE)
        ON CONFLICT (symbol) DO UPDATE
        SET name = EXCLUDED.name,
            exchange = EXCLUDED.exchange,
            is_active = TRUE,
            updated_at = NOW();
    """
    with _conn() as conn:
        with conn.cursor() as cur:
            execute_batch(cur, sql, rows, page_size=500)
        conn.commit()

    logger.info("北交所股票 UPSERT 完成: %d", len(rows))
    logger.info("样例: %s", rows[:5])


if __name__ == "__main__":
    main()
