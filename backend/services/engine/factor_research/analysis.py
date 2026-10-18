"""因子研究模块 —— 因子评价：IC / Top-N 回测 / KPI / 相关矩阵。

与 factor-lib-demo 口径对齐：
  - 截面打分已是 rank→正态分位；IC 用月频 Spearman（对 fwd 月收益）；
  - 回测 = 月末调仓、Top-N 等权、双边成本 0.2%（按换手比例计）；
  - 排行榜综合分 = 0.5 × 有效性分位(|IC|) + 0.5 × 业绩分位(Sharpe)。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

COST_RATE = 0.002  # 双边成本
DEFAULT_TOP_N = 30


def forward_returns(
    close: pd.DataFrame, dates: list[pd.Timestamp], universe: pd.DataFrame
) -> pd.DataFrame:
    """月末 → 次月末的持有期收益（复权价）；最后一个采样日为 NaN。"""
    px = close.reindex(dates).where(universe.reindex(dates).fillna(False))
    fwd = px.shift(-1) / px - 1
    return fwd


def ic_series(score: pd.DataFrame, fwd: pd.DataFrame) -> pd.Series:
    """逐期截面 Spearman IC（score vs 持有期收益）。"""
    common = score.index.intersection(fwd.index)
    s, f = score.loc[common], fwd.loc[common]
    rs = s.rank(axis=1)
    rf = f.rank(axis=1)
    ms, mf = rs.mean(axis=1), rf.mean(axis=1)
    cov = (rs.sub(ms, axis=0) * rf.sub(mf, axis=0)).sum(axis=1)
    ss = rs.sub(ms, axis=0).pow(2).sum(axis=1)
    ff = rf.sub(mf, axis=0).pow(2).sum(axis=1)
    denom = np.sqrt(ss * ff)
    out = cov / denom.replace(0, np.nan)
    # 样本太少的期数不可信
    n = (s.notna() & f.notna()).sum(axis=1)
    return out.where(n >= 30)


def backtest_topn(
    score: pd.DataFrame,
    fwd: pd.DataFrame,
    top_n: int = DEFAULT_TOP_N,
    weight: pd.Series | None = None,
    cost_rate: float = COST_RATE,
) -> dict:
    """月末调仓 Top-N 等权（或自定义单期权重）回测。

    返回 dict: dates / nav / ret（净） / gross_ret / turnover / holdings（每期 Top-N 名单）。
    """
    dates = list(score.index)
    nav, gross, turnover = [], [], []
    holdings: dict[pd.Timestamp, list[str]] = {}
    prev: set[str] = set()
    nav_v = 1.0
    for i, d in enumerate(dates):
        sc = score.loc[d].dropna()
        if sc.empty:
            continue
        picks = sc.sort_values(ascending=False).head(top_n).index.tolist()
        holdings[d] = picks
        if i + 1 >= len(dates):
            break
        f = fwd.loc[d] if d in fwd.index else pd.Series(dtype=float)
        r = f.reindex(picks).mean()
        to = len(set(picks) - prev) / max(len(picks), 1) if prev else 1.0
        net = (r - to * cost_rate) if pd.notna(r) else np.nan
        nav_v *= (1 + net) if pd.notna(net) else 1.0
        gross.append(r)
        turnover.append(to)
        nav.append(nav_v)
        prev = set(picks)
    idx = dates[: len(nav)]
    return {
        "dates": idx,
        "nav": pd.Series(nav, index=idx),
        "ret": pd.Series(
            [nav[i] / nav[i - 1] - 1 if i else nav[0] - 1 for i in range(len(nav))],
            index=idx,
        ),
        "gross_ret": pd.Series(gross, index=idx),
        "turnover": pd.Series(turnover, index=idx),
        "holdings": holdings,
    }


def kpi(ret: pd.Series, nav: pd.Series) -> dict:
    """年化 / 夏普 / 最大回撤 / 胜率 / Calmar。"""
    r = ret.dropna()
    n = len(r)
    if n == 0 or nav.dropna().empty:
        return {
            "annual_return": None,
            "sharpe": None,
            "max_drawdown": None,
            "win_rate": None,
            "calmar": None,
            "n_months": 0,
        }
    total = float(nav.dropna().iloc[-1])
    years = n / 12.0
    ann = total ** (1 / years) - 1 if total > 0 else -1.0
    sd = float(r.std())
    sharpe = float(r.mean() / sd * np.sqrt(12)) if sd > 0 else None
    navv = nav.dropna()
    mdd = float((1 - navv / navv.cummax()).max())
    win = float((r > 0).mean())
    calmar = float(ann / mdd) if mdd > 1e-9 else None
    return {
        "annual_return": round(ann, 4),
        "sharpe": round(sharpe, 3) if sharpe is not None else None,
        "max_drawdown": round(mdd, 4),
        "win_rate": round(win, 4),
        "calmar": round(calmar, 3) if calmar is not None else None,
        "n_months": n,
    }


def correlation_pairs(
    scores: dict[str, pd.DataFrame], min_common: int = 50
) -> pd.DataFrame:
    """因子两两相关（逐期截面 Spearman 的均值）。返回长表 [factor_a, factor_b, corr]。"""
    codes = list(scores)
    if len(codes) < 2:
        return pd.DataFrame(columns=["factor_a", "factor_b", "corr"])
    dates = None
    for df in scores.values():
        dates = df.index if dates is None else dates.intersection(df.index)
    acc = np.zeros((len(codes), len(codes)))
    cnt = np.zeros((len(codes), len(codes)))
    for d in dates:
        mat = pd.DataFrame({c: scores[c].loc[d] for c in codes})
        rk = mat.rank()
        cm = rk.corr(min_periods=min_common).to_numpy()
        ok = np.isfinite(cm)
        acc[ok] += cm[ok]
        cnt[ok] += 1
    with np.errstate(invalid="ignore"):
        mean_corr = np.where(cnt > 0, acc / np.maximum(cnt, 1), np.nan)
    rows = []
    for i in range(len(codes)):
        for j in range(i, len(codes)):
            rows.append(
                {
                    "factor_a": codes[i],
                    "factor_b": codes[j],
                    "corr": round(float(mean_corr[i, j]), 4),
                }
            )
    return pd.DataFrame(rows)


def leaderboard(metrics: dict[str, dict]) -> list[dict]:
    """综合分排名：0.5 × |IC| 分位 + 0.5 × Sharpe 分位。"""
    codes = [
        c
        for c, m in metrics.items()
        if m.get("sharpe") is not None and m.get("ic_mean") is not None
    ]
    if not codes:
        return []
    ic = pd.Series({c: abs(metrics[c]["ic_mean"]) for c in codes})
    sh = pd.Series({c: metrics[c]["sharpe"] for c in codes})
    ic_pct = ic.rank(pct=True)
    sh_pct = sh.rank(pct=True)
    rows = []
    for c in codes:
        score = 0.5 * float(ic_pct[c]) + 0.5 * float(sh_pct[c])
        rows.append({"code": c, "composite": round(score, 4), **metrics[c]})
    rows.sort(key=lambda x: -x["composite"])
    for i, r_ in enumerate(rows, 1):
        r_["rank"] = i
    return rows
