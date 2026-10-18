#!/usr/bin/env python3
"""T-FE-08 PIT 核查三件套：财务发布日对齐 / 退市股保留 / 停牌 ffill 口径。

只读审计（不写任何数据集、不触 PG/Redis），产出 md（人读）+ json（机器读）报告。
细案：``docs/因子挖掘_T-FE-08_PIT核查三件套_细案_20261011.md``。

用法::

    python backend/scripts/audit/audit_pit_trio.py all
    python backend/scripts/audit/audit_pit_trio.py financial --dates 20260415,20260422
    python backend/scripts/audit/audit_pit_trio.py suspension --datasets features_daily,l1_factors
    python backend/scripts/audit/audit_pit_trio.py delisted

设计要点（细案 D2/D3）：

- 财务三视图全部走 ``repair_valuation_ttm._sum4_map`` 同一核（唯一实现不复制）：
  PIT₁(D)=announce≤上一交易日；PIT₀(D)=announce≤D；naive=全量最新版。
  逐格分类 match_pit1 / match_pit0_only / match_naive_only / diverged /
  stored_nan / missing，泄漏率只在**判别格**（naive≠PIT₁）内统计。
- 退市审计 = fixture 探针（退市整理期等公开交易无歧义日）× 六载体存在性 +
  锚日集合差 + 对照股；判定 ``survivorship_filter_confirmed``（存量缺陷，known_open）。
- 停牌审计 = 出货集分区符号 − 当日 raw 面板符号（anti-join），全库幻影行计数、
  最长跨度、冻结值抽验、复牌缝标签检查、qlib bin 对照。

默认退出码 0（审计员语义：报告即交付）；``--fail-on critical`` 供未来挂 CI。
"""

from __future__ import annotations

import argparse
import bisect
import json
import logging
import sys
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.scripts.repair_valuation_ttm import (  # noqa: E402
    _load_by_symbol,
    _sum4_map,
)
from backend.shared.quantdb_paths import resolve_quantdb_dir  # noqa: E402

logger = logging.getLogger("audit_pit_trio")

SCHEMA_VERSION = 1
RTOL = 1e-3
NAIVE_ASOF = pd.Timestamp("2099-01-01")

LABELS_ORDER = (
    "match_pit1",
    "match_pit0_only",
    "match_naive_only",
    "diverged",
    "stored_nan",
    "missing",
)

DEFAULT_FINANCIAL_DATES = (
    "20260415",
    "20260422",
    "20260430",
    "20260715",
    "20260722",
    "20261009",
    "20260515",  # 窗口外基线（对照锚）
    "20250930",
)

ANCHOR_DATES = ("20180102", "20190603", "20210104", "20240102", "20260930")

# 退市 fixture：探针日取退市整理期（连续竞价、公开交易记录无歧义）或停牌前
# 正常连续交易段；control=同期在册对照股（探针日必须在场，验证方法学）。
FIXTURES: tuple[dict[str, Any], ...] = (
    {
        "code": "600401.SH",
        "name": "退市海润",
        "delist": "2019-07-08",
        "probes": ("20190603", "20190701"),
        "evidence": "退市整理期 2019-05-27~2019-07-08，两探针日均为整理期连续交易日",
        "control": {"code": "600518.SH", "name": "康美药业"},
    },
    {
        "code": "300104.SZ",
        "name": "乐视网",
        "delist": "2020-07-20",
        "probes": ("20200615", "20200629"),
        "evidence": "退市整理期 2020-06-05~2020-07-20，两探针日均为整理期连续交易日",
        "control": {"code": "600518.SH", "name": "康美药业"},
    },
    {
        "code": "002450.SZ",
        "name": "康得新",
        "delist": "2021-05-31",
        "probes": ("20181101", "20190102"),
        "evidence": "停牌（2019-01-23 立案调查）前正常交易段，两探针日均连续竞价",
        "control": {"code": "600518.SH", "name": "康美药业"},
    },
)

_SUBDIR = {
    "valuation": ("5_technical_derived", "valuation"),
    "features_daily": ("6_ml_datasets", "features_daily"),
    "l1_factors": ("6_ml_datasets", "l1_factors"),
}

_FIN_FIELDS = ("net_profit_ttm", "revenue_ttm")  # 有三视图参照的字段
_PE_FIELDS = ("pe_ttm", "ps_ttm")  # 无三视图参照（由 total_mv/np 一致性单列核对）

# 标签列命名两代：≤20260918 分区用 return_*（旧命名），≥20260921 用 future_return_*
# （新编排重命名；两代单位均为 %）。仅按 future_return_* 找会把全部历史判成
# label_column_absent——复牌缝检查等于死代码（2026-10-11 评审 HIGH-1 实测）。
_LABEL_COL_CANDIDATES = (
    "future_return_1d",
    "return_1d",
    "future_ret_1d",
    "forward_return_1d",
    "label_1d",
)


# ── 基础工具 ────────────────────────────────────────────────────────


def _qdb_root() -> Path:
    return resolve_quantdb_dir()


def _root_guard(qdb: Path) -> str | None:
    """错根守卫：quantdb 根必须含核心数据集，否则审计会零样本静默出「净报告」。

    判据取 ``1_kline_data/daily_unadjusted``（三子审计的公共事实源——财务/退市/
    停牌的日历与交易性全从这里取）；不满足返回错误文案（CLI 以退 2 拒绝出报告），
    满足返回 None。曾状：错根下全部子审计输出空表 + exit 0，报告像「全绿」。
    """
    if not (qdb / "1_kline_data" / "daily_unadjusted").is_dir():
        return (
            f"quantdb 根不可用：{qdb} 下无 1_kline_data/daily_unadjusted"
            "（检查 QM_QUANTDB_DATA_DIR / data 符号链接后重试）"
        )
    return None


def _dataset_dir(qdb: Path, name: str) -> Path:
    return qdb.joinpath(*_SUBDIR.get(name, ("6_ml_datasets", name)))


def _partition_dates(raw_dir: Path) -> list[str]:
    """raw 面板分区名序列即交易日历（不引第二事实源）。"""
    return sorted(p.name.split("=", 1)[1] for p in raw_dir.glob("dt=*") if p.is_dir())


def _prev_trading_day(cal: Sequence[str], d: str) -> str | None:
    """严格早于 ``d`` 的最近交易日；无前驱返回 None。"""
    i = bisect.bisect_left(list(cal), d)
    return cal[i - 1] if i > 0 else None


def _schema_names(path: Path) -> list[str]:
    import pyarrow.parquet as pq

    return pq.ParquetFile(path).schema_arrow.names


def _parquet_nrows(path: Path) -> int:
    import pyarrow.parquet as pq

    return int(pq.ParquetFile(path).metadata.num_rows)


def _read_cols(path: Path, cols: Sequence[str]) -> pd.DataFrame:
    import pyarrow.parquet as pq

    names = set(_schema_names(path))
    use = [c for c in cols if c in names]
    return pq.read_table(path, columns=use).to_pandas()


def _sql_escape(s: str) -> str:
    return s.replace("'", "''")


def _symbol_diff(path_a: Path, path_b: Path, con: Any = None) -> set[str]:
    """path_a 有而 path_b 无的 symbol 集合（duckdb anti-join）。"""
    import duckdb

    own = con is None
    if own:
        con = duckdb.connect()
    try:
        q = (
            f"SELECT DISTINCT symbol FROM read_parquet('{_sql_escape(str(path_a))}') "
            f"WHERE symbol IS NOT NULL AND symbol NOT IN "
            f"(SELECT symbol FROM read_parquet('{_sql_escape(str(path_b))}') "
            f"WHERE symbol IS NOT NULL)"
        )
        return {r[0] for r in con.execute(q).fetchall()}
    finally:
        if own:
            con.close()


def _to_qlib_symbol(code: str) -> str:
    c = code.strip().upper()
    if "." in c:
        num, ex = c.split(".", 1)
        return f"{ex.lower()}{num.lower()}"
    for ex in ("SH", "SZ", "BJ"):
        if c.startswith(ex):
            return f"{ex.lower()}{c[2:]}"
    return c.lower()


def _load_instrument_ranges(all_txt: Path) -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    for line in all_txt.read_text(encoding="utf-8").splitlines():
        parts = line.strip().split()
        if len(parts) >= 2:
            out[parts[0]] = (parts[1], parts[2] if len(parts) > 2 else "")
    return out


def _instrument_covers(rng: tuple[str, str], probe: str) -> bool:
    """probe 为 ``YYYYMMDD``；空 end 视为不可判定 → 不覆盖（保守）。"""
    start, end = rng
    if not end:
        return False
    iso = f"{probe[:4]}-{probe[4:6]}-{probe[6:]}"
    return start <= iso <= end


# ── 财务分类器（D3）─────────────────────────────────────────────────


def _close_arr(a: Any, b: Any, rtol: float = RTOL) -> np.ndarray:
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    return np.isfinite(a) & np.isfinite(b) & np.isclose(a, b, rtol=rtol, atol=0.0)


def _classify_cells(
    stored: Any, pit1: Any, pit0: Any, naive: Any, rtol: float = RTOL
) -> np.ndarray:
    """逐格分类。优先级：missing > stored_nan > match_pit1 > pit0_only > naive_only > diverged。"""
    s = np.asarray(stored, dtype=float)
    p1 = np.asarray(pit1, dtype=float)
    have_ref = np.isfinite(p1)
    c1 = _close_arr(s, p1, rtol)
    c0 = _close_arr(s, pit0, rtol)
    cn = _close_arr(s, naive, rtol)
    out = np.full(p1.shape, "diverged", dtype=object)
    out[have_ref & ~np.isfinite(s)] = "stored_nan"
    out[~have_ref] = "missing"
    out[have_ref & ~c1 & c0] = "match_pit0_only"
    out[have_ref & ~c1 & ~c0 & cn] = "match_naive_only"
    out[c1] = "match_pit1"
    return out


def _disc_flags(
    pit1: Any, pit0: Any, naive: Any, rtol: float = RTOL
) -> tuple[np.ndarray, np.ndarray]:
    """(可判别 naive 泄漏, 可判别当日口径) 的布尔掩码。"""
    p1 = np.asarray(pit1, dtype=float)
    p0 = np.asarray(pit0, dtype=float)
    pn = np.asarray(naive, dtype=float)
    fin1 = np.isfinite(p1)
    d_naive = fin1 & np.isfinite(pn) & ~_close_arr(p1, pn, rtol)
    d_lag = fin1 & np.isfinite(p0) & ~_close_arr(p1, p0, rtol)
    return d_naive, d_lag


def _summarize_labels(
    labels: np.ndarray, disc_naive: np.ndarray, disc_lag: np.ndarray
) -> dict[str, Any]:
    labels = np.asarray(labels, dtype=object)
    dn_mask = np.asarray(disc_naive, dtype=bool)
    dl_mask = np.asarray(disc_lag, dtype=bool)
    counts = {lab: int(np.sum(labels == lab)) for lab in LABELS_ORDER}
    dn = int(dn_mask.sum())
    dl = int(dl_mask.sum())
    leak = int(np.sum((labels == "match_naive_only") & dn_mask))
    pit0_only = int(np.sum((labels == "match_pit0_only") & dl_mask))
    return {
        "n_total": int(labels.size),
        "counts": counts,
        "disc_naive_n": dn,
        "leak_naive_only_in_disc": leak,
        "leak_rate_in_disc": (leak / dn) if dn else None,
        "disc_lag_n": dl,
        "match_pit0_only_in_disc_lag": pit0_only,
        "pit0_only_rate_in_disc_lag": (pit0_only / dl) if dl else None,
    }


# ── 幻影跨度 / 退市判定 ─────────────────────────────────────────────


def _spans_from_pairs(
    pairs: Iterable[tuple[str, str]], cal: Sequence[str]
) -> list[dict[str, Any]]:
    """(dt, symbol) 幻影行对 → 连续交易日跨度，按 (-天数, symbol, start) 排序。"""
    idx = {d: i for i, d in enumerate(cal)}
    by_sym: dict[str, list[int]] = {}
    for dt, sym in pairs:
        i = idx.get(dt)
        if i is not None:
            by_sym.setdefault(sym, []).append(i)
    spans: list[dict[str, Any]] = []
    for sym, ix in by_sym.items():
        ix.sort()
        if not ix:
            continue
        run_start = prev = ix[0]
        for i in [*ix[1:], None]:
            if i is not None and i == prev + 1:
                prev = i
                continue
            spans.append(
                {
                    "symbol": sym,
                    "start": cal[run_start],
                    "end": cal[prev],
                    "n_days": prev - run_start + 1,
                }
            )
            if i is not None:
                run_start = prev = i
    spans.sort(key=lambda r: (-r["n_days"], r["symbol"], r["start"]))
    return spans


def _fixture_verdict(
    absent_flags: Sequence[bool],
    gone_to_latest: Sequence[int],
    control_flags: Sequence[bool] = (),
) -> str:
    """结构判据用**累计差**（各锚日 − 最新锚日）：相邻差含停牌（会回归），
    累计差若恒 0 = 全域从未失去成员（幸存者过滤器证据）；非 0 即否。"""
    if not absent_flags or not all(bool(x) for x in absent_flags):
        return "not_confirmed"
    if any(int(g) != 0 for g in gone_to_latest):
        return "not_confirmed"
    if control_flags and not all(bool(x) for x in control_flags):
        return "not_confirmed"
    return "survivorship_filter_confirmed"


def _delisted_status(verdict: str, control_flags: Sequence[bool]) -> str:
    """存量状态随判定走（不得硬编码 known_open——上游回补或对照缺席时报告
    会自相矛盾）：confirmed→known_open（披露锚点）；未复现但对照齐备→resolved；
    其余（对照缺席=方法瑕疵）→inconclusive。"""
    if verdict == "survivorship_filter_confirmed":
        return "known_open"
    if control_flags and all(bool(x) for x in control_flags):
        return "resolved"
    return "inconclusive"


# ── financial 子审计 ────────────────────────────────────────────────


def _field_view(
    df: pd.DataFrame,
    field: str,
    p1: pd.Series,
    p0: pd.Series,
    pn: pd.Series,
    rtol: float,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """单字段逐格分类 + 样例。返回 (summary, examples)。"""
    if field not in df.columns:
        return {"status": "column_absent"}, []
    stored = df[field].to_numpy(dtype=float)
    r1 = df["symbol"].map(p1).to_numpy(dtype=float)
    r0 = df["symbol"].map(p0).to_numpy(dtype=float)
    rn = df["symbol"].map(pn).to_numpy(dtype=float)
    labels = _classify_cells(stored, r1, r0, rn, rtol)
    d_naive, d_lag = _disc_flags(r1, r0, rn, rtol)
    summary = _summarize_labels(labels, d_naive, d_lag)
    syms = df["symbol"].to_numpy()
    examples: list[dict[str, Any]] = []
    for label in ("match_naive_only", "diverged"):
        sel = np.where(labels == label)[0][:5]
        for i in sel:
            examples.append(
                {
                    "symbol": str(syms[i]),
                    "label": label,
                    "stored": _f(stored[i]),
                    "pit1": _f(r1[i]),
                    "pit0": _f(r0[i]),
                    "naive": _f(rn[i]),
                }
            )
    return summary, examples


def _f(x: Any) -> float | None:
    x = float(x)
    return None if not np.isfinite(x) else x


def _pe_consistency(df: pd.DataFrame, rtol: float) -> dict[str, Any]:
    """pe_ttm == total_mv / net_profit_ttm（同分区自洽）。"""
    need = ("pe_ttm", "total_mv", "net_profit_ttm")
    if any(c not in df.columns for c in need):
        return {"status": "column_absent"}
    pe = df["pe_ttm"].to_numpy(dtype=float)
    mv = df["total_mv"].to_numpy(dtype=float)
    npst = df["net_profit_ttm"].to_numpy(dtype=float)
    with np.errstate(divide="ignore", invalid="ignore"):
        exp = np.where(npst != 0, mv / npst, np.nan)
    checked = int((np.isfinite(pe) & np.isfinite(exp)).sum())
    match = int((_close_arr(pe, exp, rtol)).sum())
    syms = df["symbol"].to_numpy()
    bad = np.where(np.isfinite(pe) & np.isfinite(exp) & ~_close_arr(pe, exp, rtol))[0][
        :3
    ]
    return {
        "n_checked": checked,
        "n_match": match,
        "match_rate": (match / checked) if checked else None,
        "examples": [
            {"symbol": str(syms[i]), "pe_stored": _f(pe[i]), "pe_expected": _f(exp[i])}
            for i in bad
        ],
    }


def audit_financial(
    qdb: Path,
    dates: Sequence[str],
    datasets: Sequence[str] = ("valuation", "features_daily"),
    rtol: float = RTOL,
) -> dict[str, Any]:
    raw = qdb / "1_kline_data" / "daily_unadjusted"
    cal = _partition_dates(raw)
    inc_dir = qdb / "3_financial_data" / "income"
    symbols = sorted(p.stem for p in inc_dir.glob("*.parquet"))
    logger.info("financial: 载入 income 关键列（%d 只）…", len(symbols))
    inc = _load_by_symbol(
        symbols,
        ("3_financial_data", "income"),
        ("m_timetag", "m_anntime", "revenue", "net_profit_excl_min_int_inc"),
    )
    # 载入缺口可见化：核内单文件读取失败静默跳过，若不计数会静默降级成
    # no_discriminating_cells（评审 LOW③）。
    n_loaded = int(inc["symbol"].nunique()) if len(inc) else 0
    if n_loaded < len(symbols):
        logger.warning(
            "income 载入缺口：文件 %d 只、成功载入 %d 只（差额=读取失败静默跳过）",
            len(symbols),
            n_loaded,
        )
    np1_naive = _sum4_map(inc, NAIVE_ASOF, "net_profit_excl_min_int_inc")
    rv_naive = _sum4_map(inc, NAIVE_ASOF, "revenue")

    out: dict[str, Any] = {
        "dates": list(dates),
        "rtol": rtol,
        "n_financial_symbols": len(symbols),
        "n_loaded_symbols": n_loaded,
        "datasets": {},
        "examples": {},
        "summary": {},
    }
    for ds in datasets:
        out["datasets"][ds] = {}
        out["examples"][ds] = {}
        out["summary"][ds] = {}
        ddir = _dataset_dir(qdb, ds)
        for dt in dates:
            f = ddir / f"dt={dt}" / "data.parquet"
            if not f.exists():
                out["datasets"][ds][dt] = {"status": "partition_missing"}
                continue
            prev = _prev_trading_day(cal, dt)
            if prev is None:
                out["datasets"][ds][dt] = {"status": "no_prev_trading_day"}
                continue
            ts_prev, ts_dt = pd.Timestamp(prev), pd.Timestamp(dt)
            views = {
                "net_profit_ttm": (
                    _sum4_map(inc, ts_prev, "net_profit_excl_min_int_inc"),
                    _sum4_map(inc, ts_dt, "net_profit_excl_min_int_inc"),
                    np1_naive,
                ),
                "revenue_ttm": (
                    _sum4_map(inc, ts_prev, "revenue"),
                    _sum4_map(inc, ts_dt, "revenue"),
                    rv_naive,
                ),
            }
            df = _read_cols(
                f,
                ["symbol", *_FIN_FIELDS, *_PE_FIELDS, "total_mv"],
            )
            entry: dict[str, Any] = {
                "status": "ok",
                "n_rows": int(len(df)),
                "fields": {},
            }
            leak_total = disc_total = 0
            for field, (p1, p0, pn) in views.items():
                summary, examples = _field_view(df, field, p1, p0, pn, rtol)
                entry["fields"][field] = summary
                if examples:
                    out["examples"][ds].setdefault(dt, {})[field] = examples
                leak_total += int(summary.get("leak_naive_only_in_disc") or 0)
                disc_total += int(summary.get("disc_naive_n") or 0)
            for field in _PE_FIELDS:
                if field in df.columns:
                    entry["fields"][field] = {"status": "stored_only"}
            entry["pe_consistency"] = _pe_consistency(df, rtol)
            entry["verdict"] = (
                "leak_detected"
                if leak_total
                else ("consistent" if disc_total else "no_discriminating_cells")
            )
            out["datasets"][ds][dt] = entry
            out["summary"][ds][dt] = {
                "np_leak": int(
                    entry["fields"]["net_profit_ttm"].get("leak_naive_only_in_disc")
                    or 0
                ),
                "np_disc": int(
                    entry["fields"]["net_profit_ttm"].get("disc_naive_n") or 0
                ),
                "rev_leak": int(
                    entry["fields"]["revenue_ttm"].get("leak_naive_only_in_disc") or 0
                ),
                "rev_disc": int(
                    entry["fields"]["revenue_ttm"].get("disc_naive_n") or 0
                ),
                "verdict": entry["verdict"],
            }
            logger.info(
                "financial: %s %s -> %s（np 判别 %s/泄漏 %s，rev 判别 %s/泄漏 %s）",
                ds,
                dt,
                entry["verdict"],
                out["summary"][ds][dt]["np_disc"],
                out["summary"][ds][dt]["np_leak"],
                out["summary"][ds][dt]["rev_disc"],
                out["summary"][ds][dt]["rev_leak"],
            )
    return out


# ── suspension 子审计 ───────────────────────────────────────────────


def _qlib_close_at(
    qlib_dir: Path, cal_iso: dict[str, int], symbol: str, dt: str
) -> float | None:
    """读 qlib close.day.bin 在 dt 的值；缺 bin/缺日期返回 None，NaN 也返回 None。"""
    binp = qlib_dir / "features" / _to_qlib_symbol(symbol) / "close.day.bin"
    if not binp.exists():
        return None
    iso = f"{dt[:4]}-{dt[4:6]}-{dt[6:]}"
    i = cal_iso.get(iso)
    if i is None:
        return None
    arr = np.fromfile(binp, dtype="<f4")
    if arr.size < 2:
        return None
    start = int(arr[0])
    j = i - start
    vals = arr[1:]
    if j < 0 or j >= vals.size:
        return None
    v = float(vals[j])
    return None if not np.isfinite(v) else v


def _raw_symbols(raw: Path, dt: str) -> set[str] | None:
    p = raw / f"dt={dt}" / "data.parquet"
    if not p.exists():
        return None
    return set(_read_cols(p, ["symbol"])["symbol"])


def _enrich_span(
    qdb: Path,
    ds: str,
    span: dict[str, Any],
    cal: Sequence[str],
    raw: Path,
    qlib_dir: Path | None,
    cal_iso: dict[str, int],
    rtol: float = RTOL,
) -> dict[str, Any]:
    """Top 跨度三件实证：冻结值、复牌缝标签、qlib 对照。

    基一致原则（评审 HIGH-2）：出货集 close 是复权基（同日面板/raw 比值中位
    ~1.31、仅 ~6% 落在 rtol=1e-3 内），冻结与跳空一律在**面板自身基**内计算
    （前一交易日面板 close vs 幻影行，复牌日面板 close vs 冻结值）；raw 未复权
    close 只作旁证列，跨基比较会把正常冻结误判 not-frozen。
    """
    ddir = _dataset_dir(qdb, ds)
    sym = span["symbol"]
    enriched = dict(span)
    start_i = cal.index(span["start"])
    # ① 冻结值：面板停牌前一交易日 close vs 幻影行 close（同基）
    enriched["freeze"] = {"status": "calendar_start"}
    raw_present_before = None
    raw_prev_close = None
    if start_i > 0:
        prev_dt = cal[start_i - 1]
        ds_prev_p = ddir / f"dt={prev_dt}" / "data.parquet"
        ds_p = ddir / f"dt={span['start']}" / "data.parquet"
        if ds_prev_p.exists() and ds_p.exists():
            pdf = _read_cols(ds_prev_p, ["symbol", "close"])
            prow = pdf[pdf["symbol"] == sym]
            ddf = _read_cols(ds_p, ["symbol", "close"])
            drow = ddf[ddf["symbol"] == sym]
            if len(prow) and len(drow):
                pc = float(prow["close"].iloc[0])
                dc = float(drow["close"].iloc[0])
                # NaN 是「无值可比」不是「不冻结」：isclose(NaN,·)=False 会把
                # 缺失渲染成否定判词（评审 LOW）；有限值才给 True/False。
                frozen = (
                    bool(np.isclose(pc, dc, rtol=rtol, atol=0.0))
                    if (np.isfinite(pc) and np.isfinite(dc))
                    else None
                )
                enriched["freeze"] = {
                    "status": "ok",
                    "prev_panel_date": prev_dt,
                    "prev_panel_close": _f(pc),
                    "phantom_close": _f(dc),
                    "is_frozen": frozen,
                }
            else:
                enriched["freeze"] = {"status": "panel_row_absent"}
        else:
            # 上一交易日分区文件不在 ≠ 日历起点（评审 LOW：曾把缺文件错报成
            # calendar_start）
            enriched["freeze"] = {"status": "prev_partition_missing"}
        # raw 旁证（未复权基，仅记录，不作判据）
        raw_p = raw / f"dt={prev_dt}" / "data.parquet"
        if raw_p.exists():
            rdf = _read_cols(raw_p, ["symbol", "close"])
            rrow = rdf[rdf["symbol"] == sym]
            raw_present_before = bool(len(rrow))
            if len(rrow):
                raw_prev_close = _f(float(rrow["close"].iloc[0]))
    enriched["freeze"]["raw_prev_close_aux"] = raw_prev_close
    # raw 全域采样（跨度中点 + 最新日）：区分「跨源符号集不一致（BSE 代码迁移等）」
    # 与「停牌后不久即退市」两类伪典型跨度——两者 raw 全域皆无此 symbol。
    raw_after = None
    mid_dt = cal[(start_i + cal.index(span["end"])) // 2]
    for probe_dt in (mid_dt, cal[-1]):
        p = raw / f"dt={probe_dt}" / "data.parquet"
        if p.exists():
            hit = sym in set(_read_cols(p, ["symbol"])["symbol"])
            raw_after = bool(raw_after or hit)
    enriched["raw_evidence"] = {
        "present_before_span": raw_present_before,
        "present_mid_or_latest": raw_after,
        "absent_all_samples": raw_present_before is False and raw_after is not True,
    }
    # ② 复牌缝：跨停牌期的 1 日收益标签（标签两代命名：≤20260918 分区为
    # return_*，≥20260921 为 future_return_*；旧代实测为 % 单位）
    end_i = cal.index(span["end"])
    enriched["resume"] = {"status": "no_resume_found_in_window"}
    resume_dt = raw_resume_close = None
    for j in range(end_i + 1, min(end_i + 31, len(cal))):
        dt = cal[j]
        s = _raw_symbols(raw, dt)
        if s is None or sym not in s:
            continue
        resume_dt = dt
        rdf = _read_cols(raw / f"dt={dt}" / "data.parquet", ["symbol", "close"])
        m = rdf[rdf["symbol"] == sym]
        raw_resume_close = _f(float(m["close"].iloc[0])) if len(m) else None
        break
    if resume_dt is not None:
        prev_of_resume = cal[cal.index(resume_dt) - 1]
        ds_p = ddir / f"dt={prev_of_resume}" / "data.parquet"
        label_col = None
        stored_label = None
        if ds_p.exists():
            names = _schema_names(ds_p)
            label_col = next((c for c in _LABEL_COL_CANDIDATES if c in names), None)
            if label_col:
                ddf = _read_cols(ds_p, ["symbol", label_col])
                drow = ddf[ddf["symbol"] == sym]
                if len(drow):
                    stored_label = _f(drow[label_col].iloc[0])
        # 面板自身基的复牌跳空
        panel_resume_close = None
        ds_r_p = ddir / f"dt={resume_dt}" / "data.parquet"
        if ds_r_p.exists():
            rdf2 = _read_cols(ds_r_p, ["symbol", "close"])
            m2 = rdf2[rdf2["symbol"] == sym]
            if len(m2):
                panel_resume_close = float(m2["close"].iloc[0])
        frozen_close = None
        if enriched["freeze"].get("status") == "ok":
            frozen_close = enriched["freeze"].get("phantom_close")
        # NaN=无值可比（评审 LOW）：非有限值一律 None，不得让 NaN 短路成假判词
        jump = None
        if (
            panel_resume_close is not None
            and frozen_close is not None
            and np.isfinite(panel_resume_close)
            and np.isfinite(float(frozen_close))
        ):
            jump = panel_resume_close / float(frozen_close) - 1
        # 标签口径核对：标签（%）对应比值 label/100，与面板跳空同基时应一致；
        # 不一致 = 标签用别的基/源算的（复牌缝上常见），如实记录供人工裁决。
        label_vs_jump = None
        if stored_label is not None and jump is not None and np.isfinite(stored_label):
            label_vs_jump = bool(np.isclose(stored_label / 100.0, jump, rtol=rtol))
        enriched["resume"] = {
            "status": "ok" if label_col else "label_column_absent",
            "resume_date": resume_dt,
            "label_date": prev_of_resume,
            "seamless_with_span_end": prev_of_resume == span["end"],
            "panel_resume_close": _f(panel_resume_close)
            if panel_resume_close is not None
            else None,
            "panel_jump_vs_frozen": _f(jump) if jump is not None else None,
            "raw_resume_close_aux": raw_resume_close,
            "stored_label_col": label_col,
            "stored_label": stored_label,
            "stored_label_units": "percent" if label_col else None,
            "label_vs_panel_jump_consistent": label_vs_jump,
        }
    # ③ qlib 对照（日历不可用时不冒充「全 NaN」——探针会因缺日历恒 None，评审 LOW）
    enriched["qlib"] = {"status": "qlib_unavailable"}
    if qlib_dir is not None and cal_iso:
        mid_dt = cal[(start_i + end_i) // 2]
        probes = {
            "start": _qlib_close_at(qlib_dir, cal_iso, sym, span["start"]),
            "mid": _qlib_close_at(qlib_dir, cal_iso, sym, mid_dt),
            "end": _qlib_close_at(qlib_dir, cal_iso, sym, span["end"]),
        }
        bin_found = (
            qlib_dir / "features" / _to_qlib_symbol(sym) / "close.day.bin"
        ).exists()
        enriched["qlib"] = {
            "status": "ok" if bin_found else "bin_missing",
            "qlib_symbol": _to_qlib_symbol(sym),
            "probes": probes,
            "all_nan_in_span": bin_found and all(v is None for v in probes.values()),
        }
    elif qlib_dir is not None:
        enriched["qlib"] = {"status": "calendar_missing"}
    return enriched


def audit_suspension(
    qdb: Path,
    datasets: Sequence[str] = ("features_daily", "l1_factors"),
    top_k: int = 10,
    qlib_dir: Path | None = None,
    rtol: float = RTOL,
) -> dict[str, Any]:
    import duckdb

    raw = qdb / "1_kline_data" / "daily_unadjusted"
    cal = _partition_dates(raw)
    cal_iso: dict[str, int] = {}
    if qlib_dir is not None and (qlib_dir / "calendars" / "day.txt").exists():
        cal_iso = {
            s: i
            for i, s in enumerate(
                (qlib_dir / "calendars" / "day.txt").read_text().split()
            )
        }
    out: dict[str, Any] = {"datasets": {}}
    con = duckdb.connect()
    try:
        for ds in datasets:
            ds_dir = _dataset_dir(qdb, ds)
            parts = sorted(p for p in ds_dir.glob("dt=*") if p.is_dir())
            phantom_pairs: list[tuple[str, str]] = []
            missing_pairs: list[tuple[str, str]] = []
            n_scanned = skipped = 0
            logger.info("suspension: %s（%d 分区）…", ds, len(parts))
            for p in parts:
                dt = p.name.split("=", 1)[1]
                raw_p = raw / f"dt={dt}" / "data.parquet"
                ds_p = p / "data.parquet"
                if not raw_p.exists() or not ds_p.exists():
                    skipped += 1
                    continue
                ph = _symbol_diff(ds_p, raw_p, con=con)
                ms: set[str] = set()
                if ph or _parquet_nrows(ds_p) != _parquet_nrows(raw_p):
                    ms = _symbol_diff(raw_p, ds_p, con=con)
                n_scanned += 1
                phantom_pairs.extend((dt, s) for s in ph)
                missing_pairs.extend((dt, s) for s in ms)
            spans = _spans_from_pairs(phantom_pairs, cal)
            top = [
                _enrich_span(qdb, ds, s, cal, raw, qlib_dir, cal_iso, rtol=rtol)
                for s in spans[:top_k]
            ]
            phantom_by_part = Counter(dt for dt, _ in phantom_pairs)
            phantom_dates_sorted = sorted(phantom_by_part)
            last_missing_dt = None
            if missing_pairs:
                last_missing_dt = max(
                    (dt for dt, _ in missing_pairs), key=lambda d: cal.index(d)
                )
            bse_share = sum(
                1 for _, s in missing_pairs if s.endswith(".BJ") and s.startswith("92")
            )
            out["datasets"][ds] = {
                "n_partitions_total": len(parts),
                "n_partitions_scanned": n_scanned,
                "n_partitions_skipped": skipped,
                "phantom_rows": len(phantom_pairs),
                "phantom_symbols": len({s for _, s in phantom_pairs}),
                "phantom_first_date": (
                    phantom_dates_sorted[0] if phantom_dates_sorted else None
                ),
                "phantom_last_date": (
                    phantom_dates_sorted[-1] if phantom_dates_sorted else None
                ),
                "missing_rows": len(missing_pairs),
                "missing_symbols": len({s for _, s in missing_pairs}),
                "phantom_by_partition": dict(sorted(phantom_by_part.items())),
                "missing_last_partition": {
                    "date": last_missing_dt,
                    # sorted：键序不得随 set 迭代序（PYTHONHASHSEED）漂移——
                    # 否则「同参数复跑 JSON 一致」只在计数层面成立（评审 LOW）
                    "prefix_counts": dict(
                        sorted(
                            Counter(
                                s.split(".")[0][:2]
                                for dt, s in missing_pairs
                                if dt == last_missing_dt
                            ).items()
                        )
                    )
                    if last_missing_dt
                    else {},
                },
                "missing_rows_bse_92": bse_share,
                "top_spans": top,
            }
            logger.info(
                "suspension: %s phantom=%d（%d 只）missing=%d（%d 只）",
                ds,
                len(phantom_pairs),
                out["datasets"][ds]["phantom_symbols"],
                len(missing_pairs),
                out["datasets"][ds]["missing_symbols"],
            )
    finally:
        con.close()
    return out


# ── delisted 子审计 ─────────────────────────────────────────────────


def audit_delisted(qdb: Path, qlib_dir: Path | None = None) -> dict[str, Any]:
    raw = qdb / "1_kline_data" / "daily_unadjusted"
    fin_dir = qdb / "3_financial_data" / "income"
    ranges: dict[str, tuple[str, str]] = {}
    if qlib_dir is not None:
        all_txt = qlib_dir / "instruments" / "all.txt"
        if all_txt.exists():
            ranges = _load_instrument_ranges(all_txt)
    sym_cache: dict[str, set[str] | None] = {}

    def raw_syms(dt: str) -> set[str] | None:
        if dt not in sym_cache:
            sym_cache[dt] = _raw_symbols(raw, dt)
        return sym_cache[dt]

    fixtures_out: list[dict[str, Any]] = []
    absent_flags: list[bool] = []
    control_flags: list[bool] = []
    for fx in FIXTURES:
        code = fx["code"]
        probes_out = []
        for probe in fx["probes"]:
            s = raw_syms(probe)
            in_panel = bool(s is not None and code in s)
            val_p = qdb.joinpath(*_SUBDIR["valuation"]) / f"dt={probe}" / "data.parquet"
            in_val = False
            if val_p.exists():
                in_val = code in set(_read_cols(val_p, ["symbol"])["symbol"])
            # 六载体矩阵（细案 D4）：raw 三面板 + valuation + 财报文件 + qlib 区间
            in_fwd = in_bwd = False
            fwd_p = (
                qdb / "1_kline_data" / "daily_forward" / f"dt={probe}" / "data.parquet"
            )
            if fwd_p.exists():
                in_fwd = code in set(_read_cols(fwd_p, ["symbol"])["symbol"])
            bwd_p = (
                qdb / "1_kline_data" / "daily_backward" / f"dt={probe}" / "data.parquet"
            )
            if bwd_p.exists():
                in_bwd = code in set(_read_cols(bwd_p, ["symbol"])["symbol"])
            qs = _to_qlib_symbol(code)
            in_qlib = qs in ranges and _instrument_covers(ranges[qs], probe)
            probes_out.append(
                {
                    "probe": probe,
                    "absent_panel": not in_panel,
                    "presence": {
                        "daily_unadjusted": in_panel,
                        "daily_forward": in_fwd,
                        "daily_backward": in_bwd,
                        "valuation": in_val,
                        "financial_file": (fin_dir / f"{code}.parquet").exists(),
                        "qlib_instrument": in_qlib,
                    },
                }
            )
        absent_flags.append(all(p["absent_panel"] for p in probes_out))
        ctl = fx["control"]["code"]
        ctl_present_all = all(
            (s := raw_syms(p)) is not None and ctl in s for p in fx["probes"]
        )
        control_flags.append(ctl_present_all)
        qs = _to_qlib_symbol(code)
        fixtures_out.append(
            {
                "code": code,
                "name": fx["name"],
                "delist": fx["delist"],
                "evidence": fx["evidence"],
                "control": fx["control"],
                "control_present_all_probes": ctl_present_all,
                "qlib_range": ranges.get(qs),
                "probes": probes_out,
            }
        )
    # 锚日集合差
    anchor_sets: list[set[str] | None] = [raw_syms(dt) for dt in ANCHOR_DATES]
    existing = [s for s in anchor_sets if s is not None]
    latest = existing[-1] if existing else None
    gone_counts: list[dict[str, Any]] = []
    for i in range(len(anchor_sets) - 1):
        a, b = anchor_sets[i], anchor_sets[i + 1]
        if a is None or b is None:
            continue
        diff = a - b
        gone_counts.append(
            {
                "from": ANCHOR_DATES[i],
                "to": ANCHOR_DATES[i + 1],
                "gone": len(diff),
                # 全数回归 → 该相邻差系停牌（非退市）；出现不回归者=历史成员被剔除
                "all_returned_by_latest": (
                    all(s in latest for s in diff) if latest is not None else None
                ),
            }
        )
    gone_to_latest = (
        [len(s - latest) for s in anchor_sets if s is not None]
        if latest is not None
        else []
    )
    verdict = _fixture_verdict(absent_flags, gone_to_latest, control_flags)
    out = {
        "fixtures": fixtures_out,
        "anchors": {
            "dates": list(ANCHOR_DATES),
            "n_symbols": {
                dt: (len(s) if s is not None else None)
                for dt, s in zip(ANCHOR_DATES, anchor_sets, strict=True)
            },
            "gone_counts": gone_counts,
            "gone_to_latest": {
                dt: (
                    len(s - latest) if (s is not None and latest is not None) else None
                )
                for dt, s in zip(ANCHOR_DATES, anchor_sets, strict=True)
            },
            "gone_first_to_last": (
                len(existing[0] - existing[-1]) if len(existing) >= 2 else None
            ),
        },
        "n_financial_symbols": len(list(fin_dir.glob("*.parquet"))),
        "n_qlib_instruments": len(ranges),
        "survivorship_filter_absent_flags": absent_flags,
        "control_flags": control_flags,
        "verdict": verdict,
        "status": _delisted_status(verdict, control_flags),
    }
    logger.info(
        "delisted: verdict=%s（fixtures=%d，锚日=%d，对照=%s）",
        verdict,
        len(fixtures_out),
        len(gone_counts),
        control_flags,
    )
    return out


# ── 报告渲染与退出码 ────────────────────────────────────────────────


def _fmt_num(x: Any, nd: int = 4) -> str:
    if x is None:
        return "—"
    try:
        v = float(x)
    except (TypeError, ValueError):
        return str(x)
    if not np.isfinite(v):
        return "—"
    if abs(v - round(v)) < 1e-12 and abs(v) < 1e15:
        return str(int(round(v)))
    return f"{v:.{nd}g}"


def _fmt_freeze(fz: dict[str, Any]) -> str:
    """冻结抽验三态：True=冻结 / False=非冻结 / None=无值可比（NaN 不得渲染成
    否定判词，评审 LOW）；非 ok 状态照实显示。"""
    if fz.get("status") != "ok":
        return str(fz.get("status", "—"))
    arrow = (
        f"close {_fmt_num(fz.get('prev_panel_close'))}→"
        f"{_fmt_num(fz.get('phantom_close'))}"
    )
    if fz.get("is_frozen") is True:
        return f"{arrow} 冻结"
    if fz.get("is_frozen") is False:
        return f"{arrow} **非冻结**"
    return f"{arrow} 无值可比"


def _render_financial(fin: dict[str, Any]) -> list[str]:
    lines = ["## 1. 财务发布日对齐（financial）", ""]
    if not fin:
        lines += ["（缺 section）", ""]
        return lines
    lines.append(
        f"- 样本日 {len(fin.get('dates') or [])} 个；rtol={fin.get('rtol')}；"
        f"income 文件 {fin.get('n_financial_symbols')} 只、实际载入 "
        f"{fin.get('n_loaded_symbols', '—')} 只；三视图：PIT₁=公告≤上一交易日、"
        "PIT₀=公告≤当日、naive=全量最新版"
    )
    lines.append(
        "- 判词：leak_detected（判别格内出现仅 naive 可解释的格）/ consistent（有判别力且零泄漏）"
        " / no_discriminating_cells（当日三视图同值，防稀释）"
    )
    lines.append(
        "- 泄漏率脚注：`泄漏率` 分母 = **判别格数**（当日 naive≠PIT₁ 且参照可算的格），"
        "非全样本——三视图同值的格无判别力，单列计数防巨量稀释出假的 0.00%"
    )
    lines.append("")
    for ds, dates in (fin.get("datasets") or {}).items():
        lines.append(f"### {ds}")
        lines.append(
            "| 日期 | 状态 | 行数 | 字段 | 判别格 | 泄漏(naive_only) | 泄漏率 | pit0_only | diverged | stored_nan | missing |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for dt, entry in (dates or {}).items():
            if not isinstance(entry, dict) or entry.get("status") != "ok":
                lines.append(
                    f"| {dt} | {entry.get('status', '?') if isinstance(entry, dict) else '?'} | | | | | | | | | |"
                )
                continue
            fields = entry.get("fields") or {}
            for field in _FIN_FIELDS:
                s = fields.get(field) or {}
                if s.get("status") == "column_absent":
                    lines.append(
                        f"| {dt} | ok | {_fmt_num(entry.get('n_rows'))} | {field} | 列缺失 | | | | | | |"
                    )
                    continue
                c = s.get("counts") or {}
                lines.append(
                    f"| {dt} | ok | {_fmt_num(entry.get('n_rows'))} | {field} "
                    f"| {s.get('disc_naive_n', '—')} | **{s.get('leak_naive_only_in_disc', '—')}** "
                    f"| {_fmt_num(s.get('leak_rate_in_disc'))} | {s.get('match_pit0_only_in_disc_lag', '—')} "
                    f"| {c.get('diverged', '—')} | {c.get('stored_nan', '—')} | {c.get('missing', '—')} |"
                )
            pe = entry.get("pe_consistency") or {}
            if pe.get("status") != "column_absent":
                lines.append(
                    f"| {dt} | ok | | pe≡mv/np 自洽 | {pe.get('n_checked', '—')} 查 | "
                    f"{_fmt_num(pe.get('match_rate'))} 一致 | | | | | |"
                )
        lines.append("")
    examples = fin.get("examples") or {}
    if examples:
        lines.append("**样例（泄漏/发散格）**")
        lines.append("")
        for ds, bydt in examples.items():
            for dt, byfield in (bydt or {}).items():
                for field, rows in (byfield or {}).items():
                    for r in rows[:3]:
                        lines.append(
                            f"- {ds} {dt} {field} {r.get('label')}: {r.get('symbol')} "
                            f"stored={_fmt_num(r.get('stored'))} pit1={_fmt_num(r.get('pit1'))} "
                            f"pit0={_fmt_num(r.get('pit0'))} naive={_fmt_num(r.get('naive'))}"
                        )
        lines.append("")
    return lines


def _render_delisted(dl: dict[str, Any]) -> list[str]:
    lines = ["## 2. 退市股保留（delisted）", ""]
    if not dl:
        lines += ["（缺 section）", ""]
        return lines
    lines.append(
        f"- 判定：**{dl.get('verdict', '—')}**；status={dl.get('status', '—')}"
        "（survivorship_filter_confirmed=全库仅覆盖当前在册股票）"
    )
    lines.append("")
    lines.append(
        "| 代码 | 名称 | 退市 | 探针日 | raw 未复权 | 前复权 | 后复权 | valuation | 财报文件 | qlib 区间 | 对照在场 |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|")

    def _pres(v: Any, absent_word: str = "**缺席**", present_word: str = "在场") -> str:
        # 缺键=不可判定：旧形状 JSON 不得渲染成「缺席」这类肯定断言（评审 LOW）
        if v is True:
            return present_word
        if v is False:
            return absent_word
        return "?"

    for fx in dl.get("fixtures") or []:
        ctl = "✓" if fx.get("control_present_all_probes") else "✗"
        qr = fx.get("qlib_range")
        qr_s = f"{qr[0]}~{qr[1]}" if qr else "—"
        for p in fx.get("probes") or []:
            pres = p.get("presence") or {}
            lines.append(
                f"| {fx.get('code')} | {fx.get('name')} | {fx.get('delist')} | {p.get('probe')} "
                f"| {_pres(pres.get('daily_unadjusted'))} "
                f"| {_pres(pres.get('daily_forward'))} "
                f"| {_pres(pres.get('daily_backward'))} "
                f"| {_pres(pres.get('valuation'))} "
                f"| {_pres(pres.get('financial_file'), '**无**', '有')} "
                f"| {qr_s} | {ctl} |"
            )
    lines.append("")
    anchors = dl.get("anchors") or {}
    lines.append(
        "- 锚日符号数："
        + "；".join(f"{dt}={n}" for dt, n in (anchors.get("n_symbols") or {}).items())
    )
    for g in anchors.get("gone_counts") or []:
        back = g.get("all_returned_by_latest")
        n_gone = int(g.get("gone") or 0)
        if n_gone == 0:
            back_s = "（该区间无成员消失）"
        elif back is True:
            back_s = "（全数回归=停牌非退市）"
        elif back is False:
            back_s = "（**存在不回归成员=历史成员被剔除**）"
        else:
            back_s = ""
        lines.append(
            f"  - symbols({g.get('from')}) − symbols({g.get('to')}) = {g.get('gone')}{back_s}"
        )
    gtl = anchors.get("gone_to_latest") or {}
    if gtl:
        lines.append(
            "- 累计差（各锚日 − 最新锚日）："
            + "；".join(f"{dt}:{n}" for dt, n in gtl.items())
            + "（恒 0 = 全域从未失去成员；A股 2018–2026 真实退市 200+ 家，故为幸存者过滤器证据）"
        )
    lines.append("")
    return lines


def _render_suspension(sus: dict[str, Any]) -> list[str]:
    lines = ["## 3. 停牌 ffill 口径（suspension）", ""]
    if not sus:
        lines += ["（缺 section）", ""]
        return lines
    lines.append(
        "- 幻影行 = 出货集当日行，其 symbol 不在当日 raw 面板（停牌日被 ffill/凝固）"
    )
    lines.append(
        "- 判读口径：raw 面板与 qlib bin 停牌期无行/NaN（正确）；出货集幻影行 close=停牌前冻结值。"
        "跨度尾部 `raw证据=全域未见` 的行属**跨源符号集不一致**（如北交所 8xxxxx→92xxxx 代码迁移，"
        "例 920305.BJ）或停牌后直接退市——非典型停牌，需人工裁决"
    )
    lines.append(
        "- **基一致口径**：冻结与跳空一律在**出货集自身复权基**内计算（前一交易日面板 close → "
        "幻影行 close；复牌日面板 close ÷ 冻结值−1），raw 未复权 close 仅作旁证列（跨基比较"
        "会把正常冻结误判 not-frozen）。`标签一致`=标签（%）与面板跳空同基一致（rtol 内）"
    )
    lines.append("")
    for ds, entry in (sus.get("datasets") or {}).items():
        lines.append(f"### {ds}")
        lines.append(
            f"- 分区 {entry.get('n_partitions_scanned', '—')}/"
            f"{entry.get('n_partitions_total', '—')} 扫描（跳过 {entry.get('n_partitions_skipped', 0)}）；"
            f"**幻影行 {entry.get('phantom_rows', '—')}**（{entry.get('phantom_symbols', '—')} 只，"
            f"分布 {entry.get('phantom_first_date', '—')}~{entry.get('phantom_last_date', '—')}）；"
            f"反向覆盖差 {entry.get('missing_rows', '—')}（{entry.get('missing_symbols', '—')} 只，"
            f"含北交所 92xxxx {entry.get('missing_rows_bse_92', '—')} 行）"
        )
        top = entry.get("top_spans") or []
        if top:
            lines.append("")
            lines.append(
                "| symbol | 起 | 止 | 交易日数 | 冻结抽验（面板同基） | 复牌日 | 复牌前标签 | "
                "面板跳空 | 标签一致 | raw证据 | qlib 对照 |"
            )
            lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
            for s in top:
                fz = s.get("freeze") or {}
                rz = s.get("resume") or {}
                ql = s.get("qlib") or {}
                re_ = s.get("raw_evidence") or {}
                fz_s = _fmt_freeze(fz)
                rz_s = (
                    f"{rz.get('stored_label_col')}={_fmt_num(rz.get('stored_label'))}"
                    if rz.get("stored_label_col")
                    else rz.get("status", "—")
                )
                lc = rz.get("label_vs_panel_jump_consistent")
                lc_s = "✓" if lc is True else ("**✗不同基**" if lc is False else "—")
                ql_s = (
                    "全 NaN（抽样，正确）"
                    if ql.get("all_nan_in_span")
                    else (ql.get("status") or "—")
                )
                if re_.get("absent_all_samples"):
                    re_s = "**全域未见**"
                elif re_.get("present_before_span") or re_.get("present_mid_or_latest"):
                    re_s = "在场"
                else:
                    re_s = "—"  # 无采样证据不得渲染成肯定断言（评审 LOW）
                lines.append(
                    f"| {s.get('symbol')} | {s.get('start')} | {s.get('end')} | {s.get('n_days')} "
                    f"| {fz_s} | {rz.get('resume_date', '—')} | {rz_s} | "
                    f"{_fmt_num(rz.get('panel_jump_vs_frozen'))} | {lc_s} | {re_s} | {ql_s} |"
                )
        lines.append("")
    return lines


def _render_md(result: dict[str, Any]) -> str:
    lines = [
        "# T-FE-08 PIT 核查三件套报告（自动生成）",
        "",
        f"- 生成时间：{result.get('generated_at', '—')}",
        f"- QuantDB 根：{result.get('quantdb_root', '—')}",
        f"- schema_version：{result.get('schema_version', '—')}；只读审计，本报告不改任何数据",
        "",
        "> 判据与设计见 `docs/因子挖掘_T-FE-08_PIT核查三件套_细案_20261011.md`。"
        "已知存量：退市股全库缺失（幸存者偏差）与 features_daily/l1_factors 停牌幻影行为**未修复缺陷**，"
        "其披露锚点即本报告。",
        "",
    ]
    lines += _render_financial(result.get("financial") or {})
    lines += _render_delisted(result.get("delisted") or {})
    lines += _render_suspension(result.get("suspension") or {})
    crit = _critical_items(result)
    lines += ["## 4. 复核要点", ""]
    if crit:
        # 全量列出与头部计数保持一致（曾截断 20 条 → 头部 25、清单 20 自相矛盾）
        lines.append(f"- `--fail-on critical` 将命中 {len(crit)} 条：")
        lines += [f"  - {c}" for c in crit]
    else:
        lines.append("- 无 critical 命中")
    lines.append(
        "- 复跑一致性：同参数多次运行应给出**数据逐项一致**结果（含键序——字典键序"
        "已 `sorted` 钉住，不随 PYTHONHASHSEED 漂移；本版本实测跨种子字节级一致）。"
        "只读审计，无时间依赖除 generated_at；不承诺跨版本字节级相同"
    )
    lines.append("")
    return "\n".join(lines)


def _critical_items(result: dict[str, Any]) -> list[str]:
    items: list[str] = []
    fin = result.get("financial") or {}
    for ds, dates in (fin.get("datasets") or {}).items():
        for dt, entry in (dates or {}).items():
            if not isinstance(entry, dict):
                continue
            for field, summary in (entry.get("fields") or {}).items():
                if not isinstance(summary, dict):
                    continue
                leak = int(summary.get("leak_naive_only_in_disc") or 0)
                if leak > 0:
                    items.append(f"financial:{ds}:{dt}:{field}:leak={leak}")
    dl = result.get("delisted") or {}
    if dl.get("verdict") == "survivorship_filter_confirmed":
        items.append("delisted:survivorship_filter_confirmed")
    sus = result.get("suspension") or {}
    for ds, entry in (sus.get("datasets") or {}).items():
        if not isinstance(entry, dict):
            continue
        ph = int(entry.get("phantom_rows") or 0)
        if ph > 0:
            items.append(f"suspension:{ds}:phantom_rows={ph}")
    return items


# ── CLI ─────────────────────────────────────────────────────────────


def _write_reports(result: dict[str, Any], out_dir: Path) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = str(result.get("generated_at", ""))[:10].replace("-", "")
    base = f"T-FE-08_pit_audit_{stamp}"
    json_path = out_dir / f"{base}.json"
    md_path = out_dir / f"{base}.md"
    json_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    md_path.write_text(_render_md(result), encoding="utf-8")
    return md_path, json_path


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="T-FE-08 PIT 核查三件套（只读审计）")
    ap.add_argument(
        "command",
        nargs="?",
        default="all",
        choices=["all", "financial", "delisted", "suspension"],
    )
    ap.add_argument(
        "--out-dir", default=None, help="默认 <quantdb根>/../refs/pit_audit/"
    )
    ap.add_argument("--dates", default=",".join(DEFAULT_FINANCIAL_DATES))
    ap.add_argument(
        "--datasets",
        default=None,
        help=(
            "逗号分隔；未给时按子命令取缺省"
            "（financial=valuation,features_daily；suspension=features_daily,l1_factors），"
            "显式给出时对该子命令生效（all 则同时生效）；delisted 无数据集概念、忽略此参"
        ),
    )
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--rtol", type=float, default=RTOL)
    ap.add_argument("--qlib-dir", default=None, help="默认 <quantdb根>/../qlib/cn_data")
    ap.add_argument("--fail-on", choices=["none", "critical"], default="none")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    qdb = _qdb_root()
    guard_err = _root_guard(qdb)
    if guard_err:
        logger.error("%s", guard_err)
        return 2
    qlib_dir = Path(args.qlib_dir) if args.qlib_dir else qdb.parent / "qlib" / "cn_data"
    if not (qlib_dir / "instruments" / "all.txt").exists():
        logger.warning("qlib 目录不可用（%s），qlib 对照项将记 unavailable", qlib_dir)
        qlib_dir = None  # type: ignore[assignment]
    out_dir = Path(args.out_dir) if args.out_dir else qdb.parent / "refs" / "pit_audit"
    datasets = tuple(args.datasets.split(",")) if args.datasets else None

    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "quantdb_root": str(qdb),
    }
    if args.command in ("all", "financial"):
        result["financial"] = audit_financial(
            qdb,
            tuple(args.dates.split(",")),
            datasets=datasets or ("valuation", "features_daily"),
            rtol=args.rtol,
        )
    if args.command in ("all", "delisted"):
        result["delisted"] = audit_delisted(qdb, qlib_dir=qlib_dir)
    if args.command in ("all", "suspension"):
        result["suspension"] = audit_suspension(
            qdb,
            datasets=datasets or ("features_daily", "l1_factors"),
            top_k=args.top_k,
            qlib_dir=qlib_dir,
            rtol=args.rtol,
        )
    md_path, json_path = _write_reports(result, out_dir)
    logger.info("报告：%s / %s", md_path, json_path)
    crit = _critical_items(result)
    logger.info("critical 项：%d", len(crit))
    for c in crit[:10]:
        logger.info("  - %s", c)
    if args.fail_on == "critical" and crit:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
