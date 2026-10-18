#!/usr/bin/env python3
"""修复 valuation 分区的 TTM 字段（源侧中报回归的本地兜底）。

背景（2026-09-13 发现）：QuantDB 云端的 TTM（净利/营收）自 2026 年中报季起**逐票**
损坏（平安银行 7 月底起、全市场中位翻转于 9/7），net_profit_ttm / revenue_ttm 断崖
（同时污染 pe_ttm / ps_ttm）：如长江电力 9/4 净利 TTM=360.8 亿 → 9/11 仅 64.3 亿。
equity / total_mv / pb / dividend_rate 正常。

修复口径（在恢复前的健康日上与源值**逐股精确一致**，中位偏差 0.0000，见 --validate）：
    np_ttm  = 最近连续 4 个单季归母净利润的求和（income.net_profit_excl_min_int_inc，单季值）
    rev_ttm = 最近连续 4 个单季营业收入的求和（income.revenue，单季值）
    pe_ttm = total_mv / np_ttm；ps_ttm = total_mv / rev_ttm

修复方式为**逐格替换**：仅当 |源值 − 重算值| / |重算值| > 25% 时替换该单元格
（健康值原样保留；公式无法算出（新股/缺季报）时保留原值）。
（不用 pershare 的每股口径还原：EPS 两位小数取整，中小盘相对误差大。）

用法（仓库根）:
    python3 backend/scripts/repair_valuation_ttm.py --survey 20260401          # 体检（只读）
    python3 backend/scripts/repair_valuation_ttm.py --validate 20260630        # 健康日校验
    python3 backend/scripts/repair_valuation_ttm.py --apply-from 20260401      # 修复（含备份）
"""

from __future__ import annotations

import argparse
import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.shared.quantdb_paths import resolve_quantdb_dir  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("repair-valuation-ttm")

_CELL_REL = 0.25  # 逐格替换阈值：相对偏差超过该值才替换


def _valuation_dir() -> Path:
    return resolve_quantdb_dir() / "5_technical_derived" / "valuation"


def _partitions(start: str | None = None) -> list[tuple[str, Path]]:
    out = []
    for p in sorted(_valuation_dir().glob("dt=*")):
        f = p / "data.parquet"
        dt = p.name[3:]
        if f.exists() and (start is None or dt >= start):
            out.append((dt, f))
    return out


def _load_by_symbol(
    symbols: list[str], subdir_parts: tuple[str, ...], cols: tuple[str, ...]
) -> pd.DataFrame:
    """逐股票读取 parquet 关键列（线程池；缺文件跳过）。返回带 symbol 列的长表。"""
    d = resolve_quantdb_dir().joinpath(*subdir_parts)

    def _one(sym: str) -> pd.DataFrame | None:
        f = d / f"{sym}.parquet"
        if not f.exists():
            return None
        try:
            df = pd.read_parquet(f, columns=list(cols))
        except Exception:  # noqa: BLE001 - 单文件损坏不拖垮全量
            return None
        df = df.copy()
        df["symbol"] = sym
        return df

    with ThreadPoolExecutor(max_workers=8) as ex:
        parts = [p for p in ex.map(_one, symbols) if p is not None]
    if not parts:
        return pd.DataFrame(columns=[*cols, "symbol"])
    df = pd.concat(parts, ignore_index=True)
    for c in ("m_timetag", "m_anntime"):
        df[c] = pd.to_datetime(df[c].astype(str), format="%Y%m%d")
    return df


def _sum4_map(inc: pd.DataFrame, asof: pd.Timestamp, value_col: str) -> pd.Series:
    """PIT: asof 时点各股「最近连续 4 个单季值求和」的 TTM。index=symbol。"""
    vis = inc[inc["m_anntime"] <= asof].dropna(subset=["m_timetag", value_col])
    if vis.empty:
        return pd.Series(dtype=float)
    vis = vis.sort_values(["symbol", "m_timetag"])
    vis = vis[~vis.duplicated(["symbol", "m_timetag"], keep="last")]
    vis["_ord"] = vis["m_timetag"].dt.year * 4 + (vis["m_timetag"].dt.month - 1) // 3
    last4 = vis.groupby("symbol", as_index=False).tail(4)
    g = last4.groupby("symbol")
    cnt = g[value_col].count()
    tot = g[value_col].sum()
    ords = g["_ord"]
    ok = (cnt == 4) & ((ords.max() - ords.min()) == 3) & (ords.nunique() == 4)
    return tot.where(ok)


def _load_sources(symbols: list[str]) -> pd.DataFrame:
    """读取 income 关键列（一次加载，多个 asof 复用）。"""
    return _load_by_symbol(
        symbols,
        ("3_financial_data", "income"),
        ("m_timetag", "m_anntime", "revenue", "net_profit_excl_min_int_inc"),
    )


def _ttm_tables(
    asof: pd.Timestamp,
    symbols: list[str],
    inc: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """symbol / np_ttm / rev_ttm 三列（按 asof PIT，金额单位元）。inc 可复用。"""
    if inc is None:
        inc = _load_sources(symbols)
    np_ttm = _sum4_map(inc, asof, "net_profit_excl_min_int_inc")
    rev_ttm = _sum4_map(inc, asof, "revenue")
    out = pd.DataFrame({"symbol": symbols})
    out["np_ttm"] = out["symbol"].map(np_ttm)
    out["rev_ttm"] = out["symbol"].map(rev_ttm)
    return out


def _cell_fix(
    part: pd.DataFrame, ttm: pd.DataFrame
) -> tuple[pd.DataFrame, dict[str, int]]:
    """逐格修复：仅替换与重算值偏差 >25% 的单元格；pe/ps 由修复后的 np/rev 推导。"""
    m = part.merge(ttm, on="symbol", how="left")
    nan_col = pd.Series(np.nan, index=m.index)
    mv = pd.to_numeric(m.get("total_mv", nan_col), errors="coerce")

    def _pick(col: str, calc) -> tuple[pd.Series, pd.Series]:
        old = pd.to_numeric(m.get(col, nan_col), errors="coerce")
        calc = pd.to_numeric(calc, errors="coerce")
        ok = calc.notna() & np.isfinite(calc) & (calc != 0)
        bad = ok & ~np.isclose(old, calc, rtol=_CELL_REL, equal_nan=False)
        return pd.Series(np.where(bad, calc, old), index=m.index), bad

    np_new, bad_np = _pick("net_profit_ttm", m.get("np_ttm", nan_col))
    rev_new, bad_rv = _pick("revenue_ttm", m.get("rev_ttm", nan_col))
    pe_new, bad_pe = _pick("pe_ttm", mv / np_new.replace(0, np.nan))
    ps_new, bad_ps = _pick("ps_ttm", mv / rev_new.replace(0, np.nan))
    fixed = m[part.columns.tolist()].copy()
    fixed["net_profit_ttm"] = np_new
    fixed["revenue_ttm"] = rev_new
    fixed["pe_ttm"] = pe_new
    fixed["ps_ttm"] = ps_new
    counts = {
        "net_profit_ttm": int(bad_np.sum()),
        "revenue_ttm": int(bad_rv.sum()),
        "pe_ttm": int(bad_pe.sum()),
        "ps_ttm": int(bad_ps.sum()),
    }
    return fixed, counts


def _rel_stats(cur: pd.Series, calc: pd.Series) -> tuple[float, float, int]:
    """(相对偏差中位数, 偏差>25% 占比, 覆盖数)。"""
    cur = pd.to_numeric(cur, errors="coerce")
    calc = pd.to_numeric(calc, errors="coerce")
    ok = cur.notna() & calc.notna() & (calc.abs() > 1e7)
    if int(ok.sum()) == 0:
        return float("nan"), float("nan"), 0
    rel = (cur[ok] - calc[ok]).abs() / calc[ok].abs()
    return float(rel.median()), float((rel > _CELL_REL).mean()), int(ok.sum())


def _scan_symbols(parts: list[tuple[str, Path]]) -> list[str]:
    """扫描窗内首尾分区的 symbol 并集（覆盖新股）。"""
    syms: set[str] = set()
    for _dt, f in (parts[0], parts[-1]):
        d = pd.read_parquet(f, columns=["symbol"])
        syms.update(d["symbol"].astype(str))
    return sorted(syms)


def survey(start: str, end: str | None = None) -> list[dict]:
    """只读体检：逐日打印 np/rev 的偏差中位数与 >25% 占比。"""
    parts = [(dt, f) for dt, f in _partitions(start) if end is None or dt <= end]
    if not parts:
        return []
    syms = _scan_symbols(parts)
    inc = _load_sources(syms)
    rows = []
    for dt, f in parts:
        part = pd.read_parquet(f, columns=["symbol", "net_profit_ttm", "revenue_ttm"])
        ttm = _ttm_tables(pd.Timestamp(dt), syms, inc)
        d = part.merge(ttm, on="symbol", how="left")
        med_np, bad_np, n_np = _rel_stats(d["net_profit_ttm"], d["np_ttm"])
        med_rv, bad_rv, n_rv = _rel_stats(d["revenue_ttm"], d["rev_ttm"])
        rows.append(
            {
                "dt": dt,
                "np_med": med_np,
                "np_bad": bad_np,
                "n_np": n_np,
                "rv_med": med_rv,
                "rv_bad": bad_rv,
                "n_rv": n_rv,
            }
        )
        log.info(
            "%s np 中位=%.4f 坏格=%.1f%%（n=%d）· rev 中位=%.4f 坏格=%.1f%%（n=%d）",
            dt,
            med_np,
            bad_np * 100,
            n_np,
            med_rv,
            bad_rv * 100,
            n_rv,
        )
    return rows


def _validate(dt: str) -> int:
    f = _valuation_dir() / f"dt={dt}" / "data.parquet"
    part = pd.read_parquet(f)
    ttm = _ttm_tables(pd.Timestamp(dt), part["symbol"].astype(str).tolist())
    m = part.merge(ttm, on="symbol", how="left")
    med_np, bad_np, n_np = _rel_stats(m["net_profit_ttm"], m["np_ttm"])
    med_rv, bad_rv, n_rv = _rel_stats(m["revenue_ttm"], m["rev_ttm"])
    log.info(
        "validate %s: np 中位=%.4f 坏格=%.1f%%（n=%d）· rev 中位=%.4f 坏格=%.1f%%（n=%d）",
        dt,
        med_np,
        bad_np * 100,
        n_np,
        med_rv,
        bad_rv * 100,
        n_rv,
    )
    if (
        not np.isfinite(med_np)
        or not np.isfinite(med_rv)
        or med_np > 0.05
        or med_rv > 0.05
    ):
        log.error(
            "校验未通过（中位偏差 >5%% 或覆盖不足）——公式与源口径不一致，禁止修复"
        )
        return 2
    return 0


def apply_from(start: str, end: str | None = None) -> int:
    """逐日逐格修复（备份 + 原子写）。"""
    parts = [(dt, f) for dt, f in _partitions(start) if end is None or dt <= end]
    if not parts:
        log.warning("无匹配分区")
        return 0
    syms = _scan_symbols(parts)
    inc = _load_sources(syms)
    total: dict[str, int] = {}
    for dt, f in parts:
        part = pd.read_parquet(f)
        ttm = _ttm_tables(pd.Timestamp(dt), syms, inc)
        fixed, counts = _cell_fix(part, ttm)
        if sum(counts.values()) == 0:
            continue
        bak = f.with_name(f.name + ".bak-ttmrepair")
        if not bak.exists():
            bak.write_bytes(f.read_bytes())
        tmp = f.with_name(f.name + ".tmp")
        fixed.to_parquet(tmp, index=False)
        tmp.replace(f)
        for k, v in counts.items():
            total[k] = total.get(k, 0) + v
        log.info(
            "%s: np %d · rev %d · pe %d · ps %d",
            dt,
            counts["net_profit_ttm"],
            counts["revenue_ttm"],
            counts["pe_ttm"],
            counts["ps_ttm"],
        )
    log.info("完成：%s", total)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="修复 valuation TTM 字段（逐格）")
    ap.add_argument(
        "--survey", type=str, default="", help="体检起点（只读，如 20260401）"
    )
    ap.add_argument(
        "--apply-from", type=str, default="", help="修复起点（如 20260401）"
    )
    ap.add_argument("--end", type=str, default="", help="终点（含），缺省为最新")
    ap.add_argument("--validate", type=str, default="", help="在健康日上校验公式")
    args = ap.parse_args()

    if args.validate:
        return _validate(args.validate)
    if args.survey:
        survey(args.survey, args.end or None)
        return 0
    if args.apply_from:
        return apply_from(args.apply_from, args.end or None)
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
