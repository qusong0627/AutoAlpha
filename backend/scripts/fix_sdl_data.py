#!/usr/bin/env python3
"""
修复 stock_daily_latest 数据质量问题:
1. 复权不一致: 2026-05-28 之后的数据是不复权价格但 adj_factor=1.0,
   需要用 qlib factor 数据重新转为前复权
2. 技术指标缺失: pct_change, return_1d, rsi_14, macd_hist 等列全为 NULL
3. 基本面缺失: pe_ttm, roe, stock_name, industry 等列全为空

用法:
    # 仅诊断 (不修改数据)
    python backend/scripts/fix_sdl_data.py --diagnose

    # 修复复权不一致 (核心修复)
    python backend/scripts/fix_sdl_data.py --fix-adjust

    # 重算技术指标
    python backend/scripts/fix_sdl_data.py --fix-indicators

    # 填充基本面
    python backend/scripts/fix_sdl_data.py --fix-fundamentals

    # 一键全部修复
    python backend/scripts/fix_sdl_data.py --fix-all
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from sqlalchemy import create_engine, text


def _get_engine():
    """Create SQLAlchemy engine from environment."""
    db_url = os.getenv(
        "DATABASE_URL",
        f"postgresql://{os.getenv('DB_USER', 'quantmind')}:{os.getenv('DB_PASSWORD', 'quantmind2026')}"
        f"@{os.getenv('DB_HOST', 'db')}:{os.getenv('DB_PORT', '5432')}/{os.getenv('DB_NAME', 'quantmind')}",
    )
    # asyncpg driver not needed for sync scripts
    if "+asyncpg" in db_url:
        db_url = db_url.replace("+asyncpg", "+psycopg2")
    if not db_url.startswith("postgresql"):
        db_url = f"postgresql+psycopg2://{os.getenv('DB_USER', 'quantmind')}:{os.getenv('DB_PASSWORD', 'quantmind2026')}@{os.getenv('DB_HOST', 'db')}:5432/quantmind"
    return create_engine(db_url, pool_pre_ping=True, future=True)


def diagnose(engine):
    """诊断数据质量问题."""
    print("\n" + "=" * 70)
    print("  stock_daily_latest 数据质量诊断")
    print("=" * 70)

    with engine.connect() as conn:
        # 1. 总行数
        total = conn.execute(text("SELECT COUNT(*) FROM stock_daily_latest WHERE volume > 0")).scalar()
        print(f"\n📊 总行数: {total:,}")

        # 2. adj_factor 分布
        row = conn.execute(text("""
            SELECT
                COUNT(*) FILTER (WHERE adj_factor = 1.0) AS adj_1,
                COUNT(*) FILTER (WHERE adj_factor != 1.0 AND adj_factor IS NOT NULL) AS adj_not1,
                MIN(adj_factor) AS min_adj,
                MAX(adj_factor) AS max_adj
            FROM stock_daily_latest WHERE volume > 0
        """)).one()
        print(f"\n📈 adj_factor 分布:")
        print(f"   adj_factor=1.0:  {row.adj_1:,} 行 ({row.adj_1/total*100:.1f}%)")
        print(f"   adj_factor≠1.0:  {row.adj_not1:,} 行 ({row.adj_not1/total*100:.1f}%)")
        print(f"   范围: [{row.min_adj:.6f}, {row.max_adj:.6f}]")

        # 3. adj_factor 跳变检测
        jump_count = conn.execute(text("""
            SELECT COUNT(DISTINCT symbol) FROM (
                SELECT symbol,
                       ABS(adj_factor - LAG(adj_factor) OVER (PARTITION BY symbol ORDER BY trade_date)) AS adj_diff
                FROM stock_daily_latest WHERE volume > 0
            ) t WHERE adj_diff > 0.1
        """)).scalar()
        print(f"\n⚠️  adj_factor 跳变(>0.1)股票数: {jump_count}")

        # 4. 跳变日期分布
        rows = conn.execute(text("""
            WITH jumps AS (
                SELECT symbol, trade_date, adj_factor,
                       LAG(adj_factor) OVER (PARTITION BY symbol ORDER BY trade_date) AS prev_adj
                FROM stock_daily_latest WHERE volume > 0
            )
            SELECT trade_date, COUNT(*) AS cnt,
                   AVG(adj_factor)::numeric(10,6) AS avg_new,
                   AVG(prev_adj)::numeric(10,6) AS avg_old
            FROM jumps WHERE ABS(adj_factor - prev_adj) > 0.1
            GROUP BY trade_date ORDER BY trade_date
        """)).fetchall()
        if rows:
            print(f"   跳变日期分布:")
            for r in rows:
                print(f"     {r.trade_date}: {r.cnt} 只, adj {r.avg_old}→{r.avg_new}")

        # 5. 指标列覆盖率
        indicator_cols = [
            "pct_change", "return_1d", "return_3d", "return_5d",
            "rsi_14", "macd_hist", "ma_gap_5", "ma_gap_20", "vol_atr_14",
            "pe_ttm", "roe", "pb", "stock_name", "industry",
        ]
        print(f"\n📋 指标列覆盖率:")
        for col in indicator_cols:
            if col in ("stock_name", "industry"):
                has = conn.execute(text(
                    f"SELECT COUNT(*) FROM stock_daily_latest WHERE volume > 0 AND {col} IS NOT NULL AND {col} != ''"
                )).scalar()
            else:
                has = conn.execute(text(
                    f"SELECT COUNT(*) FROM stock_daily_latest WHERE volume > 0 AND {col} IS NOT NULL"
                )).scalar()
            pct = has / total * 100 if total > 0 else 0
            status = "✅" if pct > 80 else "⚠️ " if pct > 10 else "❌"
            print(f"   {status} {col:20s}: {has:,} / {total:,} ({pct:.1f}%)")

        # 6. 价格跳变样例
        print(f"\n🔍 价格跳变样例 (复权不一致):")
        samples = conn.execute(text("""
            WITH ranked AS (
                SELECT symbol, trade_date, close, adj_factor,
                       LAG(close) OVER (PARTITION BY symbol ORDER BY trade_date) AS prev_close,
                       LAG(adj_factor) OVER (PARTITION BY symbol ORDER BY trade_date) AS prev_adj
                FROM stock_daily_latest WHERE volume > 0
            )
            SELECT symbol, trade_date, close, prev_close, adj_factor, prev_adj,
                   (close / NULLIF(prev_close, 0) * 100 - 100)::numeric(10,2) AS implied_pct
            FROM ranked
            WHERE ABS(adj_factor - prev_adj) > 0.1
            ORDER BY trade_date DESC LIMIT 5
        """)).fetchall()
        for r in samples:
            print(f"   {r.symbol} {r.trade_date}: close {r.prev_close:.2f}→{r.close:.2f} (隐含涨跌{r.implied_pct}%) adj {r.prev_adj:.6f}→{r.adj_factor:.6f}")


def fix_adjust(engine, dry_run: bool = False):
    """
    修复复权不一致: 把 adj_factor=1.0 但实际是不复权价格的数据,
    用 qlib factor 重新计算前复权价格.

    策略:
    - 找到 adj_factor 跳变日期 (近期数据 adj_factor=1.0, 历史数据 adj_factor<1.0)
    - 对于跳变日之后的行: close_adjusted = close * prev_adj_factor / 1.0
      (用跳变前的 adj_factor 作为参考, 因为前复权 = 原始价 × 历史复权因子)
    - 同样调整 open, high, low
    - 更新 adj_factor 为正确的值
    """
    print("\n" + "=" * 70)
    print("  修复复权不一致")
    print("=" * 70)

    with engine.connect() as conn:
        # Step 1: 找到每只股票的 adj_factor 跳变点
        print("\n1️⃣  查找 adj_factor 跳变点...")
        jumps = conn.execute(text("""
            WITH ranked AS (
                SELECT symbol, trade_date, adj_factor,
                       LAG(adj_factor) OVER (PARTITION BY symbol ORDER BY trade_date) AS prev_adj
                FROM stock_daily_latest WHERE volume > 0
            )
            SELECT symbol, trade_date, adj_factor AS new_adj, prev_adj
            FROM ranked
            WHERE ABS(adj_factor - prev_adj) > 0.1
              AND adj_factor = 1.0
            ORDER BY trade_date
        """)).fetchall()

        if not jumps:
            print("   ✅ 没有发现 adj_factor 跳变问题")
            return

        print(f"   发现 {len(jumps)} 只股票有跳变问题")

        # Step 2: 对每只股票, 用跳变前的 adj_factor 修正跳变后的价格
        # 前复权价 = 不复权价 × 复权因子
        # 跳变后的数据存的是不复权价 (adj_factor=1.0), 需要乘以正确的 factor
        fixed_count = 0
        error_count = 0

        for jump in jumps:
            symbol = jump.symbol
            jump_date = jump.trade_date
            correct_factor = jump.prev_adj  # 跳变前的 adj_factor 就是正确的前复权因子

            if correct_factor is None or correct_factor == 0:
                print(f"   ⚠️  {symbol} 跳变前 adj_factor 无效 ({correct_factor}), 跳过")
                error_count += 1
                continue

            # 查看跳变后有多少行需要修复
            count = conn.execute(text(
                "SELECT COUNT(*) FROM stock_daily_latest "
                "WHERE symbol = :sym AND trade_date >= :jd AND volume > 0 AND adj_factor = 1.0"
            ), {"sym": symbol, "jd": jump_date}).scalar()

            if count == 0:
                continue

            if dry_run:
                print(f"   [DRY RUN] {symbol}: {count} 行需要修复, factor={correct_factor:.6f}")
                fixed_count += count
                continue

            # 修复: open/high/low/close 乘以 correct_factor
            result = conn.execute(text("""
                UPDATE stock_daily_latest
                SET open = open * :factor,
                    high = high * :factor,
                    low = low * :factor,
                    close = close * :factor,
                    adj_factor = :factor
                WHERE symbol = :sym
                  AND trade_date >= :jd
                  AND volume > 0
                  AND adj_factor = 1.0
            """), {"sym": symbol, "jd": jump_date, "factor": float(correct_factor)})

            fixed_count += result.rowcount
            if fixed_count % 500 == 0:
                print(f"   已修复 {fixed_count} 行...")

        if not dry_run:
            conn.commit()

        print(f"\n   {'[DRY RUN] ' if dry_run else ''}修复完成: {fixed_count} 行已修正, {error_count} 个错误")

    # 修复后验证: 检查茅台和招商银行
    print("\n2️⃣  验证修复结果:")
    with engine.connect() as conn2:
        for sym, name in [("600519.SH", "贵州茅台"), ("600036.SH", "招商银行")]:
            rows = conn2.execute(text("""
                SELECT trade_date, close, adj_factor
                FROM stock_daily_latest
                WHERE symbol = :sym AND trade_date BETWEEN '2026-05-26' AND '2026-05-30'
                ORDER BY trade_date
            """), {"sym": sym}).fetchall()
            print(f"\n   {name} ({sym}):")
            for r in rows:
                print(f"     {r.trade_date}  close={r.close:.2f}  adj={r.adj_factor:.6f}")


def fix_indicators(engine, days: int = 365, dry_run: bool = False):
    """
    重新计算技术指标: pct_change, returns, MA, MA gaps, vol_std.
    与 daily_data_sync.py 的 _calibrate_indicators 逻辑一致,
    但支持全量重算.
    """
    print("\n" + "=" * 70)
    print(f"  重算技术指标 (最近 {days} 天)")
    print("=" * 70)

    cutoff = date.today() - timedelta(days=days)

    with engine.connect() as conn:
        print(f"\n1️⃣  读取 {cutoff} 至今的 OHLCV 数据...")
        rows = conn.execute(text("""
            SELECT symbol, trade_date, open, high, low, close, volume, amount, adj_factor
            FROM stock_daily_latest
            WHERE trade_date >= :cutoff
            ORDER BY symbol, trade_date
        """), {"cutoff": cutoff}).fetchall()

    if not rows:
        print("   ❌ 没有数据")
        return

    df = pd.DataFrame(rows, columns=["symbol", "trade_date", "open", "high", "low",
                                      "close", "volume", "amount", "adj_factor"])
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    for c in ("open", "high", "low", "close", "volume", "amount", "adj_factor"):
        df[c] = pd.to_numeric(df[c], errors="coerce")

    print(f"   {len(df):,} 行, {df['symbol'].nunique()} 只股票")

    # 计算指标
    df = df.sort_values(["symbol", "trade_date"])

    # MA
    for p in (5, 10, 20, 60):
        df[f"ma{p}"] = df.groupby("symbol")["close"].transform(
            lambda x: x.rolling(p, min_periods=1).mean()
        )
        df[f"ma_gap_{p}"] = ((df["close"] / df[f"ma{p}"]) - 1) * 100

    # 收益率
    df["ret"] = df.groupby("symbol")["close"].pct_change()
    for n in (1, 3, 5, 10, 20, 60):
        df[f"return_{n}d"] = df.groupby("symbol")["close"].pct_change(n)

    # 波动率
    df["vol_std_5"] = df.groupby("symbol")["ret"].transform(
        lambda x: x.rolling(5, min_periods=1).std() * 100
    )
    df["vol_std_20"] = df.groupby("symbol")["ret"].transform(
        lambda x: x.rolling(20, min_periods=1).std() * 100
    )
    df["vol_std_60"] = df.groupby("symbol")["ret"].transform(
        lambda x: x.rolling(60, min_periods=1).std() * 100
    )

    # 涨跌幅
    df["pct_change"] = df["ret"] * 100

    # RSI
    def _rsi(series, period=14):
        delta = series.diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.rolling(period, min_periods=period).mean()
        avg_loss = loss.rolling(period, min_periods=period).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        return 100 - (100 / (1 + rs))

    df["rsi_14"] = df.groupby("symbol")["close"].transform(lambda x: _rsi(x, 14))
    df["rsi_6"] = df.groupby("symbol")["close"].transform(lambda x: _rsi(x, 6))

    # MACD hist - 只计算 macd_hist (DB 只有这一列)
    print("   计算 MACD...")
    df["macd_hist"] = np.nan

    def _compute_macd_for_group(grp):
        if len(grp) < 26:
            return grp
        ema12 = grp["close"].ewm(span=12, adjust=False).mean()
        ema26 = grp["close"].ewm(span=26, adjust=False).mean()
        dif = ema12 - ema26
        dea = dif.ewm(span=9, adjust=False).mean()
        grp["macd_hist"] = ((dif - dea) * 2).values
        return grp

    df = df.groupby("symbol").apply(_compute_macd_for_group).reset_index(drop=True)

    # ATR
    print("   计算 ATR...")

    def _compute_atr_for_group(grp):
        h = grp["high"]
        l = grp["low"]
        pc = grp["close"].shift(1)
        tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
        grp["vol_atr_14"] = tr.rolling(14, min_periods=1).mean().values
        return grp

    df = df.groupby("symbol").apply(_compute_atr_for_group).reset_index(drop=True)

    # Volume ratio
    print("   计算 Volume ratio...")
    df["volume_ratio_5"] = df.groupby("symbol")["volume"].transform(
        lambda x: x / x.rolling(5, min_periods=1).mean()
    )
    df["volume_ratio_20"] = df.groupby("symbol")["volume"].transform(
        lambda x: x / x.rolling(20, min_periods=1).mean()
    )

    # 写回数据库
    update_cols = [
        "ma5", "ma10", "ma20", "ma60",
        "ma_gap_5", "ma_gap_10", "ma_gap_20",
        "return_1d", "return_3d", "return_5d", "return_10d", "return_20d", "return_60d",
        "vol_std_5", "vol_std_20", "vol_std_60",
        "pct_change",
        "rsi_14", "rsi_6",
        "macd_hist",
        "vol_atr_14",
        "volume_ratio_5", "volume_ratio_20",
    ]

    if dry_run:
        print(f"\n   [DRY RUN] 将更新 {len(df)} 行, 列: {update_cols}")
        return

    print(f"\n2️⃣  写回数据库 ({len(df)} 行)...")
    batch_size = 500
    total_updated = 0
    t0 = time.time()

    with engine.begin() as conn:
        for i in range(0, len(df), batch_size):
            batch = df.iloc[i:i + batch_size]
            for _, row in batch.iterrows():
                sets = []
                params = {"sym": row["symbol"], "td": row["trade_date"]}
                for col in update_cols:
                    val = row.get(col)
                    if pd.notna(val) and not (isinstance(val, float) and (np.isinf(val) or np.isnan(val))):
                        sets.append(f"{col} = :{col}")
                        params[col] = float(val)
                if sets:
                    conn.execute(text(
                        f"UPDATE stock_daily_latest SET {', '.join(sets)} "
                        "WHERE symbol = :sym AND trade_date = :td"
                    ), params)
                    total_updated += 1

            if (i + batch_size) % 10000 == 0:
                elapsed = time.time() - t0
                print(f"   {total_updated:,} 行已更新 ({elapsed:.1f}s)...")

    elapsed = time.time() - t0
    print(f"\n   ✅ 指标重算完成: {total_updated:,} 行已更新 ({elapsed:.1f}s)")


def fix_fundamentals(engine, dry_run: bool = False):
    """
    填充基本面数据: stock_name, industry, pe_ttm, roe, pb 等.
    策略: 从 akshare 获取最新基本面, 或从历史非空行前向填充.
    """
    print("\n" + "=" * 70)
    print("  填充基本面数据")
    print("=" * 70)

    with engine.connect() as conn:
        # 检查是否有参考日期的数据
        ref_date = conn.execute(text("""
            SELECT MAX(trade_date) FROM stock_daily_latest
            WHERE volume > 0 AND industry IS NOT NULL AND industry != ''
        """)).scalar()

        if ref_date is None:
            print("   ❌ 没有找到包含 industry 数据的参考日期")
            print("   尝试从 akshare 获取...")

            # 使用 akshare 获取股票列表和行业
            try:
                import akshare as ak
                print("   正在从 akshare 获取 A 股股票列表...")
                stock_info = ak.stock_info_a_code_name()
                print(f"   获取到 {len(stock_info)} 只股票信息")

                if dry_run:
                    print(f"   [DRY RUN] 将更新 stock_name 和 industry")
                    return

                # 写入 stock_name
                count = 0
                with engine.begin() as conn2:
                    for _, row in stock_info.iterrows():
                        code = str(row.get("code", ""))
                        name = str(row.get("name", ""))
                        if not code or not name:
                            continue
                        # 转换为 suffix 格式
                        if code.startswith("6"):
                            sym = f"{code}.SH"
                        elif code.startswith("0") or code.startswith("3"):
                            sym = f"{code}.SZ"
                        elif code.startswith("4") or code.startswith("8"):
                            sym = f"{code}.BJ"
                        else:
                            continue

                        result = conn2.execute(text(
                            "UPDATE stock_daily_latest SET stock_name = :name "
                            "WHERE symbol = :sym AND (stock_name IS NULL OR stock_name = '')"
                        ), {"sym": sym, "name": name})
                        count += result.rowcount

                print(f"   ✅ stock_name 已更新 {count} 行")

                # 获取行业分类
                print("   正在从 akshare 获取行业分类...")
                try:
                    industry_df = ak.stock_board_industry_name_em()
                    print(f"   获取到 {len(industry_df)} 个行业板块")

                    # 获取每只股票的行业
                    count = 0
                    with engine.begin() as conn2:
                        for _, board_row in industry_df.head(100).iterrows():  # 限制100个板块
                            board_name = str(board_row.get("板块名称", ""))
                            if not board_name:
                                continue
                            try:
                                members = ak.stock_board_industry_cons_em(symbol=board_name)
                                for _, member in members.iterrows():
                                    code = str(member.get("代码", ""))
                                    if code.startswith("6"):
                                        sym = f"{code}.SH"
                                    elif code.startswith("0") or code.startswith("3"):
                                        sym = f"{code}.SZ"
                                    else:
                                        continue
                                    result = conn2.execute(text(
                                        "UPDATE stock_daily_latest SET industry = :ind "
                                        "WHERE symbol = :sym AND (industry IS NULL OR industry = '')"
                                    ), {"sym": sym, "ind": board_name})
                                    count += result.rowcount
                            except Exception:
                                continue

                    print(f"   ✅ industry 已更新 {count} 行")
                except Exception as e:
                    print(f"   ⚠️  行业分类获取失败: {e}")

            except ImportError:
                print("   ❌ akshare 未安装, 无法获取基本面数据")
                print("   请运行: pip install akshare")
                return
        else:
            print(f"   参考日期: {ref_date}")

            if dry_run:
                print("   [DRY RUN] 将从参考日期前向填充基本面数据")
                return

            # 从参考日期前向填充
            print("   正在从参考日期前向填充...")
            with engine.begin() as conn2:
                # stock_name, industry: 直接从有值的最近日期复制
                result = conn2.execute(text("""
                    UPDATE stock_daily_latest t
                    SET stock_name = src.stock_name,
                        industry = src.industry
                    FROM (
                        SELECT DISTINCT ON (symbol) symbol, trade_date, stock_name, industry
                        FROM stock_daily_latest
                        WHERE stock_name IS NOT NULL AND stock_name != ''
                          AND industry IS NOT NULL AND industry != ''
                        ORDER BY symbol, trade_date DESC
                    ) src
                    WHERE t.symbol = src.symbol
                      AND (t.stock_name IS NULL OR t.stock_name = ''
                           OR t.industry IS NULL OR t.industry = '')
                """))
                print(f"   ✅ stock_name/industry 已更新 {result.rowcount} 行")

                # pe_ttm, roe, pb: 从最近非空行前向填充
                for col in ["pe_ttm", "pb", "roe", "total_mv", "float_mv"]:
                    result = conn2.execute(text(f"""
                        UPDATE stock_daily_latest t
                        SET {col} = src.{col}
                        FROM (
                            SELECT DISTINCT ON (symbol) symbol, trade_date, {col}
                            FROM stock_daily_latest
                            WHERE {col} IS NOT NULL AND {col} > 0
                            ORDER BY symbol, trade_date DESC
                        ) src
                        WHERE t.symbol = src.symbol
                          AND (t.{col} IS NULL OR t.{col} = 0)
                    """))
                    print(f"   ✅ {col} 已更新 {result.rowcount} 行")


def main():
    parser = argparse.ArgumentParser(description="修复 stock_daily_latest 数据质量问题")
    parser.add_argument("--diagnose", action="store_true", help="仅诊断, 不修改数据")
    parser.add_argument("--fix-adjust", action="store_true", help="修复复权不一致")
    parser.add_argument("--fix-indicators", action="store_true", help="重算技术指标")
    parser.add_argument("--fix-fundamentals", action="store_true", help="填充基本面数据")
    parser.add_argument("--fix-all", action="store_true", help="一键全部修复")
    parser.add_argument("--dry-run", action="store_true", help="试运行, 不实际写入")
    parser.add_argument("--days", type=int, default=365, help="指标重算天数 (默认365)")
    args = parser.parse_args()

    engine = _get_engine()

    if args.diagnose:
        diagnose(engine)
        return

    if not any([args.fix_adjust, args.fix_indicators, args.fix_fundamentals, args.fix_all]):
        # 默认只诊断
        diagnose(engine)
        return

    # 先诊断
    diagnose(engine)

    dry = args.dry_run

    if args.fix_all or args.fix_adjust:
        fix_adjust(engine, dry_run=dry)

    if args.fix_all or args.fix_indicators:
        fix_indicators(engine, days=args.days, dry_run=dry)

    if args.fix_all or args.fix_fundamentals:
        fix_fundamentals(engine, dry_run=dry)

    # 修复后再诊断
    if not dry:
        print("\n" + "=" * 70)
        print("  修复后验证")
        print("=" * 70)
        diagnose(engine)


if __name__ == "__main__":
    main()
