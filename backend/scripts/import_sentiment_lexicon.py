"""导入金融情绪词表到 finance_lexicon（可复现）。

词表来源（data/finance_sentiment_lexicon.tsv，51,213 词）：
- RSS 40 万标题提炼（5,705 词）：41.5 万 Huntly 标题 + enrichment 情绪标签共现统计 LLR 筛选
- DLUT 情感本体库（22,001 词）
- pysenti 内置词典（9,870 词）
- NTUSD 台大情感词典（20,485 词，繁体已转简体）

幂等：ON CONFLICT (term, kind) DO UPDATE，可反复执行。

用法:
  python3 backend/scripts/import_sentiment_lexicon.py
"""
from __future__ import annotations

import os
from pathlib import Path

import psycopg2
from psycopg2.extras import execute_values

_LEXICON_FILE = Path(
    os.getenv("QM_SENTIMENT_LEXICON", "")
) if os.getenv("QM_SENTIMENT_LEXICON") else Path(__file__).parent / "data" / "finance_sentiment_lexicon.tsv"


def load_lexicon() -> list[tuple[str, str, float, str, bool]]:
    rows: list[tuple[str, str, float, str, bool]] = []
    with open(_LEXICON_FILE, encoding="utf-8") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) != 3:
                continue
            term, kind, w = parts[0], parts[1], float(parts[2])
            if kind == "pos":
                kind_sql = "sentiment_pos"
            elif kind == "neg":
                kind_sql = "sentiment_neg"
            else:
                continue
            rows.append((term, kind_sql, w, "金融情绪词典(RSS提炼+DLUT+pysenti+NTUSD)", True))
    return rows


def main() -> None:
    conn = psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "quantmind-db"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        user=os.getenv("POSTGRES_USER", "quantmind"),
        password=os.getenv("POSTGRES_PASSWORD", "quantmind2026"),
        dbname=os.getenv("POSTGRES_DB", "quantmind"),
    )
    rows = load_lexicon()
    sql = """
        INSERT INTO finance_lexicon (term, kind, weight, note, enabled)
        VALUES %s
        ON CONFLICT (term, kind) DO UPDATE SET
            weight = EXCLUDED.weight,
            note = EXCLUDED.note,
            enabled = TRUE;
    """
    with conn.cursor() as cur:
        execute_values(cur, sql, rows, page_size=2000)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT kind, COUNT(*) FROM finance_lexicon GROUP BY kind ORDER BY 2 DESC")
        print("导入完成:", rows.__len__(), "词 | 表统计:", cur.fetchall())
    conn.close()


if __name__ == "__main__":
    main()
