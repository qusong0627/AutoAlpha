#!/usr/bin/env python3
"""
复权因子交叉校验 — 用公司行为数据审计上游复权序列
====================================================

原理:
  后复权因子可由价格序列反推: implicit_factor(t) = hfq_close(t) / unadj_close(t)
  无除权事件的日子该因子恒定; 除权日发生跳变, 跳变倍数由公司行为决定:

      expected_ratio = prev_close * (1 + stockBonus + stockGift) / (prev_close - interest)

  其中 prev_close 为除权日前一交易日不复权收盘价, interest 为每股派息(元),
  stockBonus/stockGift 为送股/转增比例。

  将 dividend_factors (公司行为明细) 推导的预期跳变与价格序列反推的实际跳变
  逐一比对, 可审计上游 daily_backward / daily_unadjusted 复权处理是否正确;
  反向可发现 dividend_factors 未收录的除权事件 (如配股) — UNEXPLAINED_JUMP。

用法:
  # 查看公司行为数据样本 (确认字段语义)
  python backend/scripts/verify_adjustment_factors.py --inspect

  # 全市场校验
  python backend/scripts/verify_adjustment_factors.py

  # 指定股票 / 随机抽样 / 明细输出
  python backend/scripts/verify_adjustment_factors.py --symbols 600036.SH,000001.SZ
  python backend/scripts/verify_adjustment_factors.py --sample 100 --output report.json

  # 容差调整 (默认 0.5% 相对误差; 价格保留两位小数自带 ~0.2% 舍入噪声)
  python backend/scripts/verify_adjustment_factors.py --tolerance 0.005

退出码: 0 = 未发现不一致; 1 = 存在 MISMATCH/UNEXPLAINED/MISSING; 2 = 运行错误
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import sys
from pathlib import Path

import duckdb
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("verify_adj_factors")

QUANTDB_DATA_DIR = Path(
    os.getenv("QM_QUANTDB_DATA_DIR", str(PROJECT_ROOT / "data" / "quantdb"))
)
KLINE_DIR = QUANTDB_DATA_DIR / "1_kline_data"
DIVIDEND_DIR = QUANTDB_DATA_DIR / "3_financial_data" / "dividend_factors"

# 跳变检测阈值: 低于该幅度的事件交由二阶段精确比对 (避免价格舍入噪声淹没)
JUMP_DETECT_THRESHOLD = 0.005
# 除权事件与跳变的最大自然日间隔 (覆盖除权日停牌、跳变顺延至复牌日)
MATCH_WINDOW_DAYS = 30

STATUS_OK = "OK"
STATUS_MISMATCH = "MISMATCH"
STATUS_MISSING_JUMP = "MISSING_JUMP"
STATUS_UNEXPLAINED_JUMP = "UNEXPLAINED_JUMP"
STATUS_NO_PRICE = "NO_PRICE"


def _glob_posix(p: Path) -> str:
    return str(p).replace("\\", "/")


def load_jump_table(
    conn: duckdb.DuckDBPyConnection, symbols: list[str] | None
) -> pd.DataFrame:
    """计算全市场隐含复权因子的日间跳变表 (只保留显著跳变行)。

    Returns: DataFrame[symbol, dt(int), factor, unadj_close, prev_close, ratio]
    """
    hfq_glob = _glob_posix(KLINE_DIR / "daily_backward" / "dt=*" / "*.parquet")
    unadj_glob = _glob_posix(KLINE_DIR / "daily_unadjusted" / "dt=*" / "*.parquet")

    sym_filter = ""
    params: list[str] = []
    if symbols is not None:
        if not symbols:
            return pd.DataFrame()
        placeholders = ", ".join("?" for _ in symbols)
        sym_filter = f"WHERE h.symbol IN ({placeholders})"
        params = list(symbols)

    sql = f"""
        WITH hfq AS (
            SELECT symbol, dt, close AS hfq_close
            FROM read_parquet('{hfq_glob}', hive_partitioning=1)
        ),
        unadj AS (
            SELECT symbol, dt, close AS unadj_close
            FROM read_parquet('{unadj_glob}', hive_partitioning=1)
        ),
        merged AS (
            SELECT h.symbol, h.dt, h.hfq_close, u.unadj_close,
                   h.hfq_close / NULLIF(u.unadj_close, 0) AS factor
            FROM hfq h
            JOIN unadj u ON h.symbol = u.symbol AND h.dt = u.dt
            {sym_filter}
        )
        SELECT symbol, dt, factor, unadj_close,
               lag(unadj_close) OVER w AS prev_close,
               factor / NULLIF(lag(factor) OVER w, 0) AS ratio
        FROM merged
        WINDOW w AS (PARTITION BY symbol ORDER BY dt)
        QUALIFY ratio IS NOT NULL AND abs(ratio - 1) > {JUMP_DETECT_THRESHOLD}
        ORDER BY symbol, dt
    """
    return conn.execute(sql, params).fetchdf()


def load_dividend_events(symbols: list[str] | None) -> dict[str, pd.DataFrame]:
    """读取公司行为明细, 按股票分组。返回 {symbol: events_df}。"""
    events: dict[str, pd.DataFrame] = {}
    if not DIVIDEND_DIR.exists():
        log.warning("dividend_factors 目录不存在: %s", DIVIDEND_DIR)
        return events

    files = sorted(DIVIDEND_DIR.glob("*.parquet"))
    if symbols is not None:
        wanted = set(symbols)
        files = [f for f in files if f.stem in wanted]
    for f in files:
        try:
            df = pd.read_parquet(f)
        except Exception as exc:  # noqa: BLE001
            log.warning("读取 %s 失败: %s", f.name, exc)
            continue
        if df.empty or "time" not in df.columns:
            continue
        df = df.copy()
        df["event_date"] = pd.to_datetime(
            df["time"].astype(str).str[:10], errors="coerce"
        )
        df = df.dropna(subset=["event_date"])
        if not df.empty:
            events[f.stem.upper()] = df.sort_values("event_date")
    return events


def _safe_num(v) -> float:
    """数值列安全取值: NaN/None/解析失败 → 0。"""
    try:
        f = float(v)
        return 0.0 if pd.isna(f) else f
    except (TypeError, ValueError):
        return 0.0


def match_symbol(
    symbol: str,
    events: pd.DataFrame,
    jumps: pd.DataFrame,
    tolerance: float,
    details: list[dict],
) -> list[str]:
    """对单只股票做事件↔跳变贪心匹配, 返回该股出现的状态列表。"""
    statuses: list[str] = []
    jump_rows = (
        jumps[jumps["symbol"] == symbol].sort_values("dt")
        if not jumps.empty
        else pd.DataFrame()
    )
    jump_list = jump_rows.to_dict("records")
    used: set[int] = set()

    for _, ev in events.iterrows():
        interest = _safe_num(ev.get("interest"))
        bonus = _safe_num(ev.get("stockBonus"))
        gift = _safe_num(ev.get("stockGift"))
        has_effect = interest > 0 or bonus > 0 or gift > 0
        ev_date = ev["event_date"]
        ev_dt_int = int(ev_date.strftime("%Y%m%d"))

        # 找事件日之后 MATCH_WINDOW_DAYS 内第一个未使用的跳变
        cand_idx = None
        for i, j in enumerate(jump_list):
            if i in used or int(j["dt"]) < ev_dt_int:
                continue
            jump_dt = pd.to_datetime(str(int(j["dt"])), format="%Y%m%d")
            if (jump_dt - ev_date).days > MATCH_WINDOW_DAYS:
                break
            cand_idx = i
            break

        record = {
            "symbol": symbol,
            "event_date": ev_date.strftime("%Y-%m-%d"),
            "interest": interest,
            "bonus": bonus,
            "gift": gift,
        }

        if cand_idx is None:
            # 一阶段未捕捉到显著跳变 → 二阶段精确比对 (可能是小额分红)
            record["status"] = STATUS_MISSING_JUMP
            details.append(record)
            statuses.append(STATUS_MISSING_JUMP)
            continue

        j = jump_list[cand_idx]
        used.add(cand_idx)
        record["jump_date"] = str(int(j["dt"]))
        record["actual_ratio"] = round(float(j["ratio"]), 6)

        prev_close = float(j["prev_close"]) if pd.notna(j["prev_close"]) else None
        if prev_close is None or prev_close <= 0:
            record["status"] = STATUS_NO_PRICE
            details.append(record)
            statuses.append(STATUS_NO_PRICE)
            continue
        record["prev_close"] = prev_close

        if not has_effect:
            # 无实质分红送转的行 (如纯股改): 只要有跳变即认为上游已处理
            record["expected_ratio"] = None
            record["status"] = STATUS_OK
            details.append(record)
            statuses.append(STATUS_OK)
            continue

        denom = prev_close - interest
        if denom <= 0:
            record["status"] = STATUS_MISMATCH
            record["note"] = "派息 >= 前收盘, 公司行为数据异常"
            details.append(record)
            statuses.append(STATUS_MISMATCH)
            continue
        expected = prev_close * (1 + bonus + gift) / denom
        record["expected_ratio"] = round(expected, 6)

        if abs(float(j["ratio"]) - expected) / expected <= tolerance:
            record["status"] = STATUS_OK
            statuses.append(STATUS_OK)
        else:
            record["status"] = STATUS_MISMATCH
            record["rel_err"] = round(abs(float(j["ratio"]) - expected) / expected, 6)
            statuses.append(STATUS_MISMATCH)
        details.append(record)

    # 未被事件解释的跳变 → 上游复权用了 dividend_factors 未收录的公司行为 (如配股)
    for i, j in enumerate(jump_list):
        if i not in used:
            details.append(
                {
                    "symbol": symbol,
                    "jump_date": str(int(j["dt"])),
                    "actual_ratio": round(float(j["ratio"]), 6),
                    "status": STATUS_UNEXPLAINED_JUMP,
                }
            )
            statuses.append(STATUS_UNEXPLAINED_JUMP)
    return statuses


def precise_check(
    conn: duckdb.DuckDBPyConnection,
    missing: list[dict],
    tolerance: float,
) -> None:
    """二阶段: 对一阶段未匹配到显著跳变的事件做精确比对。

    直接取相关股票完整隐含因子序列, 计算事件日(或其后首个交易日)实际跳变,
    避免 JUMP_DETECT_THRESHOLD 漏掉小额分红。
    """
    by_symbol: dict[str, list[dict]] = {}
    for rec in missing:
        by_symbol.setdefault(rec["symbol"], []).append(rec)
    if not by_symbol:
        return

    hfq_glob = _glob_posix(KLINE_DIR / "daily_backward" / "dt=*" / "*.parquet")
    unadj_glob = _glob_posix(KLINE_DIR / "daily_unadjusted" / "dt=*" / "*.parquet")
    symbols = list(by_symbol)
    placeholders = ", ".join("?" for _ in symbols)
    sql = f"""
        WITH hfq AS (
            SELECT symbol, dt, close AS hfq_close
            FROM read_parquet('{hfq_glob}', hive_partitioning=1)
            WHERE symbol IN ({placeholders})
        ),
        unadj AS (
            SELECT symbol, dt, close AS unadj_close
            FROM read_parquet('{unadj_glob}', hive_partitioning=1)
            WHERE symbol IN ({placeholders})
        ),
        merged AS (
            SELECT h.symbol, h.dt, u.unadj_close,
                   h.hfq_close / NULLIF(u.unadj_close, 0) AS factor
            FROM hfq h JOIN unadj u ON h.symbol = u.symbol AND h.dt = u.dt
        )
        SELECT symbol, dt, unadj_close, factor,
               lag(unadj_close) OVER w AS prev_close,
               factor / NULLIF(lag(factor) OVER w, 0) AS ratio
        FROM merged
        WINDOW w AS (PARTITION BY symbol ORDER BY dt)
        ORDER BY symbol, dt
    """
    factor_df = conn.execute(sql, symbols + symbols).fetchdf()

    for sym, recs in by_symbol.items():
        rows = factor_df[factor_df["symbol"] == sym]
        if rows.empty:
            for rec in recs:
                rec["status"] = STATUS_NO_PRICE
                rec["note"] = "该股票无日线数据"
            continue
        for rec in recs:
            ev_int = int(rec["event_date"].replace("-", ""))
            after = rows[rows["dt"] >= ev_int]
            if after.empty:
                rec["status"] = STATUS_NO_PRICE
                rec["note"] = "事件晚于数据末尾"
                continue
            row = after.iloc[0]
            ratio, prev_close = row["ratio"], row["prev_close"]
            if pd.isna(ratio) or pd.isna(prev_close) or prev_close <= 0:
                rec["status"] = STATUS_NO_PRICE
                continue
            rec["jump_date"] = str(int(row["dt"]))
            rec["actual_ratio"] = round(float(ratio), 6)
            interest, bonus, gift = rec["interest"], rec["bonus"], rec["gift"]
            denom = float(prev_close) - interest
            if denom <= 0:
                rec["status"] = STATUS_MISMATCH
                rec["note"] = "派息 >= 前收盘, 公司行为数据异常"
                continue
            expected = float(prev_close) * (1 + bonus + gift) / denom
            rec["expected_ratio"] = round(expected, 6)
            if abs(float(ratio) - expected) / expected <= tolerance:
                rec["status"] = STATUS_OK
            else:
                rec["status"] = STATUS_MISMATCH
                rec["rel_err"] = round(abs(float(ratio) - expected) / expected, 6)


def inspect_samples(events: dict[str, pd.DataFrame]) -> None:
    """打印公司行为数据样本, 供确认字段语义。"""
    if not events:
        log.warning("无 dividend_factors 数据")
        return
    log.info("dividend_factors 共 %d 只股票, 样本:", len(events))
    for sym in list(events)[:3]:
        df = events[sym]
        cols = [
            c
            for c in ("time", "interest", "stockBonus", "stockGift", "gugai", "dr")
            if c in df.columns
        ]
        log.info("---- %s (%d 条) ----", sym, len(df))
        for _, r in df.tail(8).iterrows():
            log.info("  %s", {c: r.get(c) for c in cols})


def main() -> int:
    parser = argparse.ArgumentParser(
        description="复权因子交叉校验 (公司行为 vs 价格序列)"
    )
    parser.add_argument(
        "--symbols",
        type=str,
        default=None,
        help="逗号分隔股票列表, 如 600036.SH,000001.SZ",
    )
    parser.add_argument(
        "--sample", type=int, default=0, help="随机抽样 N 只股票 (0=全量)"
    )
    parser.add_argument(
        "--tolerance",
        type=float,
        default=0.005,
        help="expected/actual 相对误差容差 (默认 0.005)",
    )
    parser.add_argument("--output", type=str, default=None, help="明细 JSON 输出路径")
    parser.add_argument("--inspect", action="store_true", help="仅打印公司行为数据样本")
    args = parser.parse_args()

    symbols: list[str] | None = None
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    if not KLINE_DIR.exists():
        log.error("K线数据目录不存在: %s", KLINE_DIR)
        return 2

    events_map = load_dividend_events(symbols)
    if args.inspect:
        inspect_samples(events_map)
        return 0
    if not events_map:
        log.error("无 dividend_factors 数据, 无法校验")
        return 2

    target_symbols = list(events_map.keys())
    if args.sample and args.sample < len(target_symbols):
        random.seed(42)
        target_symbols = sorted(random.sample(target_symbols, args.sample))
    log.info("校验股票数: %d (容差 %.3f%%)", len(target_symbols), args.tolerance * 100)

    conn = duckdb.connect()
    log.info("计算全市场隐含复权因子跳变表 ...")
    jumps = load_jump_table(conn, symbols=None if symbols is None else target_symbols)
    log.info("显著跳变事件: %d 条", len(jumps))

    details: list[dict] = []
    status_counter: dict[str, int] = {}
    for i, sym in enumerate(target_symbols, 1):
        if i % 500 == 0:
            log.info("进度 %d/%d", i, len(target_symbols))
        for st in match_symbol(sym, events_map[sym], jumps, args.tolerance, details):
            status_counter[st] = status_counter.get(st, 0) + 1

    # 二阶段精确比对: 一阶段 MISSING_JUMP 的事件可能是小额分红未过检测阈值
    missing = [d for d in details if d["status"] == STATUS_MISSING_JUMP]
    if missing:
        log.info("二阶段精确比对 %d 条未匹配事件 ...", len(missing))
        precise_check(conn, missing, args.tolerance)
        status_counter = {}
        for d in details:
            status_counter[d["status"]] = status_counter.get(d["status"], 0) + 1

    total = sum(status_counter.values())
    log.info("=" * 60)
    log.info("校验完成, 共 %d 条记录:", total)
    for st in (
        STATUS_OK,
        STATUS_MISMATCH,
        STATUS_MISSING_JUMP,
        STATUS_UNEXPLAINED_JUMP,
        STATUS_NO_PRICE,
    ):
        if st in status_counter:
            log.info(
                "  %-18s %6d  (%.2f%%)",
                st,
                status_counter[st],
                status_counter[st] * 100 / max(total, 1),
            )

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {
                    "generated_at": pd.Timestamp.now().isoformat(),
                    "symbols_checked": len(target_symbols),
                    "tolerance": args.tolerance,
                    "summary": status_counter,
                    "details": details,
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            ),
            encoding="utf-8",
        )
        log.info("明细已写入 %s", out)

    bad = sum(v for k, v in status_counter.items() if k != STATUS_OK)
    if bad:
        log.warning("发现 %d 条不一致记录 (MISMATCH/UNEXPLAINED/MISSING/NO_PRICE)", bad)
        return 1
    log.info("全部一致, 上游复权与公司行为数据交叉验证通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
