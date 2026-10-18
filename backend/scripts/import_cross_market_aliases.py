"""导入港股/美股证券主表到 stock_aliases，实现新闻跨市场代码匹配。

数据源：
- /data/quanthk/2_base_sector/security_master/data.parquet  (2,801 只港股, 含 cn_name)
- /data/quantus/2_base_sector/security_master/data.parquet   (517 只美股, 含 cn_name)

写入 stock_aliases：
- 港股: ticker=0001.HK, 别名=中文名(name)/完整代码(code, 如 "0001.HK")
- 美股: ticker=AAPL, 别名=中文名(name)/英文ticker(code)

这样 NewsMatcher 加载 stock_aliases 后，新闻标题含"腾讯控股"能匹配到 0001.HK，
含"特斯拉"能匹配到 TSLA，实现跨市场新闻→代码。

用法:
  python3 backend/scripts/import_cross_market_aliases.py
"""
from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import psycopg2
from psycopg2.extras import execute_values


def _read_security_master(sub: str) -> pd.DataFrame:
    base = Path(os.getenv("QM_QUANTHK_DATA_DIR", "/data/quanthk"))
    if sub == "US":
        base = Path(os.getenv("QM_QUANTUS_DATA_DIR", "/data/quantus"))
    f = base / "2_base_sector" / "security_master" / "data.parquet"
    if not f.exists():
        print(f"⚠️ {f} 不存在，跳过 {sub}")
        return pd.DataFrame()
    df = pd.read_parquet(f)
    # 规范化列名
    df = df.rename(columns={"cn_name": "name", "en_name": "en_name", "symbol": "symbol"})
    return df


def build_rows() -> list[tuple[str, str, str, int]]:
    """返回 [(ticker, alias, alias_type, priority)]，已按 (ticker, alias) 去重取最高优先级。"""
    rows: list[tuple[str, str, str, int]] = []

    # 港股
    hk = _read_security_master("HK")
    for _, r in hk.iterrows():
        sym = str(r["symbol"]).strip()
        name = str(r.get("name") or "").strip()
        en = str(r.get("en_name") or "").strip()
        code = sym.split(".")[0]
        if not sym:
            continue
        ticker = sym if "." in sym else f"{code}.HK"
        if name and len(name) >= 2:
            rows.append((ticker, name, "name", 85))
        if en and len(en) >= 2:
            rows.append((ticker, en, "en_name", 60))
        if code and len(code) >= 4:
            # 只入完整形态 "XXXX.HK"。裸 4 位码会与正文里的年份/数值
            # 海量误命中（"2026年"→2026.HK、"2007亿元"→碧桂园），禁入。
            rows.append((ticker, ticker, "code", 50))

    # 美股
    us = _read_security_master("US")
    for _, r in us.iterrows():
        sym = str(r["symbol"]).strip()
        name = str(r.get("name") or "").strip()
        en = str(r.get("en_name") or "").strip()
        if not sym:
            continue
        ticker = sym  # 美股 ticker 就是代码
        if name and len(name) >= 2:
            rows.append((ticker, name, "name", 85))
        if en and len(en) >= 2 and en != sym:
            rows.append((ticker, en, "en_name", 60))
        # 单字母 ticker（如 A=安捷伦）边界匹配风险高，跳过 code alias
        if sym and len(sym) >= 2:
            rows.append((ticker, sym, "code", 50))

    # 去重：同一 (ticker, alias) 取最高 priority
    dedup: dict[tuple[str, str], tuple[str, int]] = {}
    for ticker, alias, atype, prio in rows:
        key = (ticker, alias)
        if key not in dedup or dedup[key][1] < prio:
            dedup[key] = (atype, prio)
    return [(t, a, at, p) for (t, a), (at, p) in dedup.items()]


def main() -> None:
    rows = build_rows()
    print(f"生成 {len(rows)} 条别名 (HK {sum(1 for r in rows if '.HK' in r[0])}, US {sum(1 for r in rows if '.' not in r[0])})")

    conn = psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", "quantmind-db"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        user=os.getenv("POSTGRES_USER", "quantmind"),
        password=os.getenv("POSTGRES_PASSWORD", "quantmind2026"),
        dbname=os.getenv("POSTGRES_DB", "quantmind"),
    )
    sql = """
        INSERT INTO stock_aliases (ticker, alias, alias_type, priority)
        VALUES %s
        ON CONFLICT (ticker, alias) DO UPDATE SET
            alias_type = EXCLUDED.alias_type,
            priority = EXCLUDED.priority;
    """
    with conn.cursor() as cur:
        execute_values(cur, sql, rows, page_size=1000)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM stock_aliases WHERE ticker LIKE '%.HK'")
        print("导入后 .HK 别名数:", cur.fetchone()[0])
        cur.execute("SELECT COUNT(DISTINCT ticker) FROM stock_aliases")
        print("stock_aliases 去重股票数:", cur.fetchone()[0])
    conn.close()


if __name__ == "__main__":
    main()
