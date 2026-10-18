"""因子研究模块 —— PIT 财报面板 + 财务/行为类因子。

口径（2026-09-13 对 QuantDB parquet 实测确认，务必遵守）：
  - income    ：**单季值**（非 YTD）。例：茅台 2024Q4 revenue=501 亿（全年 1741 亿），
                平安银行 2024 四季营收 388/384/345/351 亿 → TTM = 最近 4 个单季求和。
  - cashflow  ：**年内累计（YTD）**。例：茅台 2024Q4 OCF=924.6 亿（=公开年报值）、
                折旧附注仅半年/年报出现（H1 940M → FY 1893M）。
                → TTM = 上年年报 + 本年累计 − 上年同期累计。
  - balance   ：时点值。
  - PIT：一律以 m_anntime（公告日）做 as-of，某日只可见已公告报表；
    同比/多年增长用「该日再往前 1/2/3 年的可见快照」，不是拿今天的报表比历史日期。
  - 缺失（NaN/0）不做插补；除零 → NaN（由 rank_to_score 自然剔除）。
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

INCOME_COLS = (
    "revenue",
    "cost_of_goods_sold",
    "oper_profit",
    "net_profit_incl_min_int_inc",
    "net_profit_excl_min_int_inc",
    "deducted_net_profit",
    "less_impair_loss_assets",
    "interest_expense",
    "tot_profit",
)
# pershare_index 现成比率（**全部实测满填充**）。注意 income 里的 cost_of_goods_sold /
# net_profit_incl_min_int_inc / tot_profit / less_impair_loss_assets 在 QuantDB 中几乎全空
# （列存在但值为 None，2026-09-13 实测），依赖它们的因子一律改走本表或 cashflow 附注。
# 本表比率是「报告期（YTD）比率」而非 TTM——同一截面上所有公司同期可比，rank 打分不受影响。
PERSHARE_COLS = (
    "roa",  # 总资产收益率（YTD）
    "net_margin",  # 净利率（YTD）
    "gross_margin",  # 毛利率（YTD）
    "return_on_investment",  # 投入资本回报率
    "inventory_turnover",  # 存货周转率
    "interest_coverage",  # 利息保障倍数
)
BALANCE_COLS = (
    "tot_assets",
    "total_current_assets",
    "total_current_liability",
    "tot_liab",
    "total_equity",
    "tot_shrhldr_eqy_excl_min_int",
    "inventories",
    "account_receivable",
    "bill_receivable",
    "other_receivable",
    "cash_equivalents",
    "trading_financial_assets",
    "intang_assets",
    "goodwill",
    "fix_assets",
    "shortterm_loan",
    "long_term_loans",
    "non_current_liability_in_one_year",
    "bonds_payable",
)
CASHFLOW_COLS = (
    "net_cash_flows_oper_act",
    "fixed_asset_depreciation",
    "asset_impairment_provision",
)

_EPS = 1e-9


def _read_q(path: Path, cols: tuple[str, ...]) -> pd.DataFrame:
    """读单只股票的报表 → DataFrame(index=quarter末, ann_date + 各列)。"""
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_parquet(path)
    keep = [c for c in ("m_timetag", "m_anntime", *cols) if c in df.columns]
    df = df[keep].copy()
    df = df[df["m_timetag"].astype(str).str.len() == 8]
    df["m_timetag"] = pd.to_datetime(df["m_timetag"], format="%Y%m%d")
    df["m_anntime"] = pd.to_datetime(df["m_anntime"], format="%Y%m%d", errors="coerce")
    for c in cols:
        if c not in df.columns:
            df[c] = np.nan
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.set_index("m_timetag").sort_index()
    return df[~df.index.duplicated(keep="last")]


def _ttm_single_quarter(df: pd.DataFrame, cols: tuple[str, ...]) -> pd.DataFrame:
    """单季序列 → TTM（要求 4 个季度连续；否则 NaN）。"""
    ord_ = df.index.year * 4 + (df.index.month + 2) // 3
    ok = pd.Series(ord_, index=df.index)
    consecutive = (ok - ok.shift(3)) == 3
    out = df[list(cols)].rolling(4, min_periods=4).sum()
    out[~consecutive] = np.nan
    return out


def _ttm_ytd(df: pd.DataFrame, cols: tuple[str, ...]) -> pd.DataFrame:
    """YTD 累计序列 → TTM = 上年年报 + 本年累计 − 上年同期累计。"""
    out = pd.DataFrame(np.nan, index=df.index, columns=list(cols))
    idx = {(d.year, d.month): i for i, d in enumerate(df.index)}
    vals = df[list(cols)]
    for i, d in enumerate(df.index):
        if d.month == 12:
            out.iloc[i] = vals.iloc[i]  # 年报即 TTM
            continue
        j_fy = idx.get((d.year - 1, 12))
        j_sq = idx.get((d.year - 1, d.month))
        if j_fy is None or j_sq is None:
            continue
        out.iloc[i] = (
            vals.iloc[j_fy].values + vals.iloc[i].values - vals.iloc[j_sq].values
        )
    return out


def build_symbol_panel(symbol: str, dirs: dict[str, Path]) -> pd.DataFrame:
    """单只股票的 PIT 季度面板：TTM 流量项 + 时点存量项 + pershare 现成比率 + ann_date。"""
    inc = _read_q(dirs["income"] / f"{symbol}.parquet", INCOME_COLS)
    bal = _read_q(dirs["balance"] / f"{symbol}.parquet", BALANCE_COLS)
    cf = _read_q(dirs["cashflow"] / f"{symbol}.parquet", CASHFLOW_COLS)
    ps = _read_q(dirs["pershare"] / f"{symbol}.parquet", PERSHARE_COLS)
    if inc.empty and bal.empty:
        return pd.DataFrame()
    inc_ttm = (
        _ttm_single_quarter(inc, INCOME_COLS)
        if not inc.empty
        else pd.DataFrame(columns=INCOME_COLS)
    )
    cf_ttm = (
        _ttm_ytd(cf, CASHFLOW_COLS)
        if not cf.empty
        else pd.DataFrame(columns=CASHFLOW_COLS)
    )
    # 合并到同一季度网格（并集，ann_date 取参与计算的报表最大公告日）
    # 注意：pandas 对 tuple 键按「单个列标签」处理，必须显式 list() 做多列选择
    parts = [inc_ttm, cf_ttm, bal[list(BALANCE_COLS)]]
    ann_parts = [inc.get("m_anntime"), cf.get("m_anntime"), bal.get("m_anntime")]
    if not ps.empty:
        parts.append(ps[list(PERSHARE_COLS)])
        ann_parts.append(ps.get("m_anntime"))
    panel = pd.concat(parts, axis=1, join="outer")
    ann = pd.concat(ann_parts, axis=1).max(axis=1)
    panel = panel.join(ann.rename("ann_date"))
    panel = panel.sort_index()
    panel["ann_date"] = panel["ann_date"].ffill()  # 快照并集行沿用最近公告日
    return panel


def _asof_values(
    panel: pd.DataFrame, dates: list[pd.Timestamp], lag_days: int = 0
) -> pd.DataFrame:
    """按 PIT（ann_date ≤ 目标日）取每列值；返回 index=dates 的 DataFrame（不含 ann_date 列）。"""
    cols = panel.drop(columns=["ann_date"])
    if panel.empty:
        return pd.DataFrame(np.nan, index=dates, columns=cols.columns)
    ann = panel["ann_date"].to_numpy(dtype="datetime64[ns]")
    target = np.array(
        [np.datetime64(pd.Timestamp(d) - pd.Timedelta(days=lag_days)) for d in dates],
        dtype="datetime64[ns]",
    )
    j = np.searchsorted(ann, target, side="right") - 1
    vals = cols.to_numpy(dtype=float, na_value=np.nan)
    out = np.full((len(dates), cols.shape[1]), np.nan)
    ok = j >= 0
    out[ok] = vals[j[ok]]
    return pd.DataFrame(out, index=dates, columns=cols.columns)


def compute_financial_factors(
    symbols: list[str],
    dirs: dict[str, Path],
    dates: list[pd.Timestamp],
    total_mv: pd.DataFrame,
) -> dict[str, pd.DataFrame]:
    """财务类 33 个 + 依赖财报的估值 3 个（PE_DED/PCF/EV2EBITDA）。返回 {code: 宽表}。"""
    codes = _FIN_CODES + ("PE_DED", "PCF", "EV2EBITDA")
    mats = {c: pd.DataFrame(np.nan, index=dates, columns=symbols) for c in codes}
    n_done = 0
    for sym in symbols:
        try:
            panel = build_symbol_panel(sym, dirs)
            if panel.empty:
                continue
            date_objs = [pd.Timestamp(d) for d in dates]
            now = _asof_values(panel, date_objs, 0)
            y1 = _asof_values(panel, date_objs, 365)
            y2 = _asof_values(panel, date_objs, 730)
            y3 = _asof_values(panel, date_objs, 1095)
            year_start = [pd.Timestamp(year=d.year, month=1, day=1) for d in date_objs]
            y0 = _asof_values(panel, year_start, 0)
            y0.index = date_objs  # 年初快照按各采样日对齐（修 EQGROWTH 网格错位）
            vals = _financial_values(
                now, y1, y2, y3, y0, total_mv[sym] if sym in total_mv.columns else None
            )
            for c in codes:
                s = vals[c]
                if getattr(s, "index", None) is not None and not s.index.is_unique:
                    # 与外部索引（如市值面板）对齐时理论上不会重复；双保险（pandas ≥3 会直接抛错）
                    s = s[~s.index.duplicated(keep="last")]
                mats[c][sym] = s.reindex(mats[c].index)
            n_done += 1
        except Exception as e:  # 单只失败不影响整体（40 分钟全量构建不能被一例拖垮）
            logger.warning("财务因子失败 %s: %s: %s", sym, type(e).__name__, e)
            continue
    logger.info("财务面板完成 %d/%d 只", n_done, len(symbols))
    return mats


_FIN_CODES = (
    "ROE",
    "ROA",
    "ROIC",
    "GROSSMGN",
    "NETMGN",
    "OPMGN",
    "OCF2OR",
    "OCF2OI",
    "ACCRUAL",
    "NPGROWTH",
    "OPGROWTH",
    "CFOGROWTH",
    "GMGROWTH",
    "DEDNPGROWTH",
    "EQGROWTH",
    "ROEGROWTH",
    "ORGROWTH",
    "GRCAGR3Y",
    "LEVERAGE",
    "IMPAIRRISK",
    "CURRENT",
    "QUICK",
    "CASHRATIO",
    "INTCOVER",
    "DEBT2EQ",
    "EQMULT",
    "OCF2DEBT",
    "TANG2ASSET",
    "ASSETTURN",
    "INVTURN",
    "ARTURN",
    "CATURN",
    "FATURN",
)


def _safe_div(a: pd.Series, b: pd.Series) -> pd.Series:
    return a / b.where(b.abs() > _EPS)


def _financial_values(
    now: pd.DataFrame,
    y1: pd.DataFrame,
    y2: pd.DataFrame,
    y3: pd.DataFrame,
    y0: pd.DataFrame,
    mv: pd.Series | None,
) -> dict[str, pd.Series]:
    """由 PIT 快照（当期/1年前/2年前/3年前/年初）计算各因子原始值。"""
    g = lambda df, c: df[c] if c in df.columns else pd.Series(np.nan, index=df.index)  # noqa: E731
    ta, ta1 = g(now, "tot_assets"), g(y1, "tot_assets")
    te = g(now, "total_equity")
    tep, tep1, tep2 = (
        g(now, "tot_shrhldr_eqy_excl_min_int"),
        g(y1, "tot_shrhldr_eqy_excl_min_int"),
        g(y2, "tot_shrhldr_eqy_excl_min_int"),
    )
    tl = g(now, "tot_liab")
    tca, tca1 = g(now, "total_current_assets"), g(y1, "total_current_assets")
    tcl = g(now, "total_current_liability")
    ar = g(now, "account_receivable").fillna(0) + g(now, "bill_receivable").fillna(0)
    ar1 = g(y1, "account_receivable").fillna(0) + g(y1, "bill_receivable").fillna(0)
    fa, fa1 = g(now, "fix_assets"), g(y1, "fix_assets")
    inv = g(now, "inventories")  # QUICK 用（INVTURN 已改 pershare）
    rev, rev1, rev3 = g(now, "revenue"), g(y1, "revenue"), g(y3, "revenue")
    opr, opr1 = g(now, "oper_profit"), g(y1, "oper_profit")
    npe, npe1 = (
        g(now, "net_profit_excl_min_int_inc"),
        g(y1, "net_profit_excl_min_int_inc"),
    )
    ded, ded1 = g(now, "deducted_net_profit"), g(y1, "deducted_net_profit")
    # 减值用现金流表附注（income.less_impair_loss_assets 实测全空）
    impair = g(now, "asset_impairment_provision")
    ocf, ocf1 = (g(now, "net_cash_flows_oper_act"), g(y1, "net_cash_flows_oper_act"))
    dep = g(now, "fixed_asset_depreciation")
    cash = g(now, "cash_equivalents")
    tfa = g(now, "trading_financial_assets")
    orecv = g(now, "other_receivable")
    intang, gw = g(now, "intang_assets"), g(now, "goodwill")
    te_year0 = g(y0, "tot_shrhldr_eqy_excl_min_int")
    # pershare 现成比率（YTD 口径；对应 income 里全空的成本/净利(含少数)/利润总额列）
    ps_roa = g(now, "roa")
    ps_netmgn = g(now, "net_margin")
    ps_grossmgn, ps_grossmgn1 = g(now, "gross_margin"), g(y1, "gross_margin")
    ps_roic = g(now, "return_on_investment")
    ps_invturn = g(now, "inventory_turnover")
    ps_intcov = g(now, "interest_coverage")

    avg_ta, avg_te_p = (ta + ta1) / 2, (tep + tep1) / 2
    roe = _safe_div(npe, avg_te_p)
    roe_1y = _safe_div(npe1, (tep1 + tep2) / 2)

    out = {}
    out["ROE"] = roe
    out["ROA"] = ps_roa
    out["ROIC"] = ps_roic
    out["GROSSMGN"] = ps_grossmgn
    out["NETMGN"] = ps_netmgn
    out["OPMGN"] = _safe_div(opr, rev)
    out["OCF2OR"] = _safe_div(ocf, rev)
    out["OCF2OI"] = _safe_div(ocf, opr)
    out["ACCRUAL"] = _safe_div(
        (ocf - npe) * 2, avg_ta
    )  # 归母净利近似（含少数口径列全空）
    out["NPGROWTH"] = _safe_div(npe, npe1) - 1
    out["OPGROWTH"] = _safe_div(opr, opr1) - 1
    out["CFOGROWTH"] = _safe_div(ocf, ocf1) - 1
    out["GMGROWTH"] = ps_grossmgn - ps_grossmgn1
    out["DEDNPGROWTH"] = _safe_div(ded, ded1) - 1
    out["EQGROWTH"] = _safe_div(tep, te_year0) - 1
    out["ROEGROWTH"] = roe - roe_1y
    out["ORGROWTH"] = _safe_div(rev, rev1) - 1
    cagr = (rev / rev3).where(rev > 0, np.nan).where(rev3 > 0, np.nan)
    out["GRCAGR3Y"] = cagr ** (1 / 3.0) - 1
    out["LEVERAGE"] = _safe_div(tl, ta)
    out["IMPAIRRISK"] = _safe_div(impair, rev)
    out["CURRENT"] = _safe_div(tca, tcl)
    out["QUICK"] = _safe_div(tca - inv, tcl)
    out["CASHRATIO"] = _safe_div(
        cash.fillna(0) + tfa.fillna(0) + ar + orecv.fillna(0), tcl
    )
    out["INTCOVER"] = ps_intcov.clip(-100, 100)
    out["DEBT2EQ"] = _safe_div(tl, te)
    out["EQMULT"] = _safe_div(ta, te)
    out["OCF2DEBT"] = _safe_div(ocf, tl)
    out["TANG2ASSET"] = _safe_div(ta - intang.fillna(0) - gw.fillna(0), ta)
    out["ASSETTURN"] = _safe_div(rev, avg_ta)
    out["INVTURN"] = ps_invturn
    out["ARTURN"] = _safe_div(rev, (ar + ar1) / 2)
    out["CATURN"] = _safe_div(rev, (tca + tca1) / 2)
    out["FATURN"] = _safe_div(rev, (fa + fa1) / 2)
    if mv is not None:
        pe_ded = _safe_div(mv, ded)
        out["PE_DED"] = pe_ded.where(ded > 0)  # 扣非亏损 → NaN（负 PE 无意义）
        out["PCF"] = _safe_div(mv, ocf).where(ocf > 0)
        ebitda = opr + dep.fillna(0)
        out["EV2EBITDA"] = _safe_div(mv + tl - cash.fillna(0), ebitda).where(ebitda > 0)
    return out


# ---------------------------------------------------------------------------
# 行为类（筹码结构 3 个）：holder_num + 流通股本
# ---------------------------------------------------------------------------
def compute_behavior_factors(
    symbols: list[str],
    holder_dir: Path,
    dates: list[pd.Timestamp],
    circ_cap: pd.DataFrame,
) -> dict[str, pd.DataFrame]:
    """HOLDERCHG / HOLDERCONC / HOLDERCONCCHG（PIT：按公告日，逐期环比）。

    HOLDERCONC  = 流通股本(该期公告日时点) / 股东户数；
    HOLDERCHG   = 户数环比（最近两期已披露报表），负向（户数降=筹码集中）；
    HOLDERCONCCHG = 户均持股环比（含股本变动）。
    """
    codes = ("HOLDERCHG", "HOLDERCONC", "HOLDERCONCCHG")
    mats = {c: np.full((len(dates), len(symbols)), np.nan) for c in codes}
    target = np.array(
        [np.datetime64(pd.Timestamp(d)) for d in dates], dtype="datetime64[ns]"
    )
    for si, sym in enumerate(symbols):
        f = holder_dir / f"{sym}.parquet"
        if not f.exists():
            continue
        try:
            df = pd.read_parquet(f, columns=["m_anntime", "holder_num"])
        except Exception:
            continue
        df["m_anntime"] = pd.to_datetime(
            df["m_anntime"], format="%Y%m%d", errors="coerce"
        )
        df["holder_num"] = pd.to_numeric(df["holder_num"], errors="coerce")
        df = (
            df.dropna()
            .sort_values("m_anntime")
            .drop_duplicates("m_anntime", keep="last")
        )
        if len(df) < 2:
            continue
        ann = df["m_anntime"]
        hn = df["holder_num"].to_numpy(dtype=float)
        # 各期公告日时点的流通股本（ffill 对齐）
        circ_at = (
            circ_cap[sym].reindex(ann, method="ffill").to_numpy(dtype=float)
            if sym in circ_cap.columns
            else np.full(len(df), np.nan)
        )
        with np.errstate(divide="ignore", invalid="ignore"):
            conc = np.where(hn > 0, circ_at / hn, np.nan)
        j = (
            np.searchsorted(ann.to_numpy(dtype="datetime64[ns]"), target, side="right")
            - 1
        )
        for di in range(len(dates)):
            ji = j[di]
            if ji < 1:
                continue
            mats["HOLDERCHG"][di, si] = (
                hn[ji] / hn[ji - 1] - 1 if hn[ji - 1] else np.nan
            )
            mats["HOLDERCONC"][di, si] = conc[ji]
            if np.isfinite(conc[ji]) and np.isfinite(conc[ji - 1]) and conc[ji - 1] > 0:
                mats["HOLDERCONCCHG"][di, si] = conc[ji] / conc[ji - 1] - 1
    return {c: pd.DataFrame(mats[c], index=dates, columns=symbols) for c in codes}
