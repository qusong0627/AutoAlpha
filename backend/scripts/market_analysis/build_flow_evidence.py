#!/usr/bin/env python3
"""资金流延续证据产物构建（市场分析 · 资金流全景 v2 细案 §3/§6/§8，P2）。

产出三件套（原子写、按日幂等，落 ``QM_FLOW_EVIDENCE_DIR``）：
- ``panel_sector.parquet`` 逐日×板块（net_main/net_ratio/breadth/mom3/ret/streak/members）
- ``evidence.json`` 信号档案：streak_2/3/5 × fwd/exec 双口径 × h=1/3/5 条件统计
  （全窗/日均超额、hit/基准、逐年、环境分层）+ 闸门判定 + 否决清单；
- ``latest.json`` 当日逐板块 streak + 命中榜（streak≥2，按「连流天数 → 净额」排序）。

口径（与在线展示面合流的三件套：单位归一 / 前向收益 / 占位过滤，须与
``backend/services/api/market_analysis`` 行为一致）：
- 单位：``flow_*`` 按分区混存万元/元 → ``normalize_l2_flow_money_to_yuan``
  归一为元；``amount`` 恒万元（×1e4 得元）；
- 价：``daily_forward``（qfq）；**禁用 daily_backward**（跨分区坏序列）。
  前向收益双口径：``fwd`` = close(t)→close(t+h)（预研探针对照口径），
  ``exec`` = close(t+1)→close(t+1+h)（可执行主口径，t+1 收盘建仓）；
- 板块：行业二级（标准 80 板块），**当前成分回溯聚合**（幸存者偏差由 UI 披露）；
- 信号：板块主力净额（超大单+大单，归一后逐日求和）> 0 的连续天数 ``streak``；
- 占位污染：与在线 ``quantdb_feed.placeholder_symbols_by_dt`` 同判据
  （同 dt 同值被 ≥25 只共享 = 假行）按日剔除。

闸门（``gate.pass``，细则随产物自披露）：
  n(h=3) ≥ ``min_cond_n`` ∧ exec h=3 全窗超额 > 0 ∧ fwd h=3 全窗超额 > 0
  ∧ 两口径分年至少各有一年为正（全表分年仍随产物展示，负年不藏）
  ∧ 环境双正（fwd h=3：市场 20 日环境上涨/下跌两档条件超额均 > 0）

用法（容器内）：
  docker exec -w /app quantmind python backend/scripts/market_analysis/build_flow_evidence.py
  ... --max-days 120 --out-dir /tmp/flow_ev_smoke   # 快速验证（窗口截断，勿上线）
退码：0 成功；1 构建失败/数据不可用（**不落盘**）；2 参数错误。
"""

from __future__ import annotations

import argparse
import gc
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.shared import flow_evidence  # noqa: E402
from backend.shared.quantdb_flow_units import (  # noqa: E402
    normalize_l2_flow_money_to_yuan,
)

log = logging.getLogger("build_flow_evidence")

L2_REL = "6_ml_datasets/l2_factors"
FW_REL = "1_kline_data/daily_forward"
MEMBERS_REL = "2_base_sector/sector_concept"
SECTOR_TYPE_NAME = "行业板块(二级)"  # 成员表的 SectorType 标签（证据体系固定二级）

HORIZONS = (1, 3, 5)
BADGE_HORIZON = flow_evidence.BADGE_HORIZON  # 3
MIN_COND_N = flow_evidence.MIN_COND_N  # 500

# 占位污染判据（与 quantdb_feed.placeholder_symbols_by_dt 相同阈值）
PLACEHOLDER_SHARE_THRESH = 25
# RankIC 最少当日配对数（个股 100 / 板块 30，同预研探针）
IC_MIN_PAIRS_STOCK = 100
IC_MIN_PAIRS_SECTOR = 30
# 纯动量对照分位与低调/追涨子分层切点（同探针 v3）
MOM_TOP_PCT = 0.7
SUBSplit_MID_PCT = 0.5

GATE_CRITERIA = (
    "n(h=3)≥min_cond_n ∧ 可执行主口径 exec h=3 全窗超额>0 ∧ 对照口径 fwd h=3 全窗超额>0 "
    "∧ 两口径分年至少各一年为正（全表分年随产物展示） ∧ 环境双正（fwd h=3 市场 20 日"
    "环境上涨/下跌两档条件超额均>0）"
)

CONVENTIONS = {
    "sector_set": flow_evidence.EVIDENCE_SECTOR_SET,
    "sector_set_label": SECTOR_TYPE_NAME,
    "price_source": "daily_forward(qfq)",
    "fwd_definition": "close(t)→close(t+h)（预研探针对照口径）",
    "exec_definition": "close(t+1)→close(t+1+h)（可执行主口径：t 日盘后信号 → T+1 收盘建仓）",
    "amount_unit": "yuan",
    "signal": "板块主力净额（超大单+大单，归一后求和）>0 的连续天数 streak",
    "sector_aggregation": "当前成分回溯聚合（存在幸存者偏差）",
    "strength_note": "bp 级强度；历史超额 ≠ 未来收益",
    "stock_layer_note": "个股层资金流裸信号经全史实测不具预测力（RankIC≤0.01），不参与展示与排序",
}


class BuildError(RuntimeError):
    """构建失败（不落盘）。"""


# ---------------------------------------------------------------------------
# 取数
# ---------------------------------------------------------------------------


def _list_partition_dates(data_dir: Path, rel: str) -> list[str]:
    """分区日期（YYYYMMDD 升序）；目录缺失返回空。"""
    d = data_dir / rel
    if not d.is_dir():
        return []
    out = [
        e.name[3:]
        for e in d.iterdir()
        if e.is_dir() and e.name.startswith("dt=") and e.name[3:].isdigit()
    ]
    return sorted(out)


def _load_flow(data_dir: Path, days: list[str]) -> pd.DataFrame:
    """L2 资金流明细（金额列归一为元；列与在线 _load_l2_flow 的消费子集一致）。"""
    files = [str(data_dir / L2_REL / f"dt={d}" / "data.parquet") for d in days]
    files = [f for f in files if Path(f).exists()]
    if not files:
        return pd.DataFrame()
    con = duckdb.connect()
    try:
        df = con.execute(
            f"""SELECT symbol, dt, flow_super_net, flow_large_net, flow_medium_net,
                flow_small_net, flow_net_amount, flow_net_ratio, amount
                FROM read_parquet({files!r})"""
        ).df()
    finally:
        con.close()
    if df.empty:
        return df
    df["dt"] = df["dt"].astype(str)
    df = normalize_l2_flow_money_to_yuan(df)
    num_cols = (
        "flow_super_net",
        "flow_large_net",
        "flow_medium_net",
        "flow_small_net",
        "flow_net_amount",
        "flow_net_ratio",
        "amount",
    )
    for c in num_cols:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    # amount 恒万元（归一模块口径）：成交额非正的行不参与信号（同预研探针）
    df["amount_y"] = df["amount"] * 1e4
    df = df[df["amount_y"] > 0].copy()
    df["main"] = df["flow_super_net"] + df["flow_large_net"]
    df["retail"] = df["flow_medium_net"] + df["flow_small_net"]
    df["net_ratio"] = df["main"] / df["amount_y"]
    df["divergence"] = (df["main"] - df["retail"]) / df["amount_y"]
    return df


def _load_close(data_dir: Path, days: list[str]) -> pd.DataFrame:
    """qfq 收盘宽表（index=symbol，columns=days）。"""
    fw_all = _list_partition_dates(data_dir, FW_REL)
    fw_days = [d for d in fw_all if days[0] <= d <= days[-1]]
    files = [str(data_dir / FW_REL / f"dt={d}" / "data.parquet") for d in fw_days]
    files = [f for f in files if Path(f).exists()]
    if not files:
        return pd.DataFrame()
    con = duckdb.connect()
    try:
        df = con.execute(f"SELECT symbol, dt, close FROM read_parquet({files!r})").df()
    finally:
        con.close()
    if df.empty:
        return df
    df["dt"] = df["dt"].astype(str)
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    wide = df.groupby(["symbol", "dt"], sort=True)["close"].mean().unstack("dt")
    return wide.reindex(columns=days)


def _load_members(data_dir: Path) -> pd.DataFrame:
    """行业二级成员 (symbol, sector_name) 去重对。

    列名兼容与两套文件名逻辑镜像 ``QuantDBDataHub.fetch_sector_members``
    （在线路径同源文件），保证离线证据与在线板块聚合用的是同一张成员表。
    """
    base = data_dir / MEMBERS_REL
    for fname in ("sector_members.parquet", "sector_member.parquet"):
        fp = base / fname
        if fp.exists():
            df = pd.read_parquet(fp)
            break
    else:
        raise BuildError(f"板块成员表缺失：{base}/sector_members.parquet")
    df = df.rename(
        columns={
            "SectorCode": "sector_code",
            "SectorName": "sector_name",
            "SectorType": "sector_type",
            "Symbol": "symbol",
        }
    )
    need = {"symbol", "sector_name", "sector_type"}
    if not need <= set(df.columns):
        raise BuildError(f"板块成员表列不齐，缺少 {need - set(df.columns)}")
    m = df[df["sector_type"].astype(str) == SECTOR_TYPE_NAME]
    m = m[["symbol", "sector_name"]].astype(str)
    m["symbol"] = m["symbol"].str.strip()
    m["sector_name"] = m["sector_name"].str.strip()
    m = m[(m["symbol"] != "") & (m["sector_name"] != "")]
    m = m.drop_duplicates()
    if m.empty:
        raise BuildError(f"成员表中无 {SECTOR_TYPE_NAME} 记录")
    return m


def _placeholder_symbols_by_day(flow: pd.DataFrame) -> dict[str, set[str]]:
    """逐日识别整行占位（同 dt 同值被 ≥N 只共享）的 symbol。

    判据与 ``quantdb_feed.placeholder_symbols_by_dt`` 完全一致；全史 950 万行下
    改为「逐日 value_counts 预筛 + 仅嫌疑值查 nunique」——原实现逐嫌疑对全表
    扫描，成本随历史线性放大；此处逐日预筛保证语义等价且线性。
    """
    bad: dict[str, set[str]] = {}
    cand = flow[flow["flow_net_amount"].notna() & (flow["flow_net_amount"] != 0)]
    for dt_val, grp in cand.groupby("dt", sort=False):
        vc = grp["flow_net_amount"].value_counts()
        suspects = vc.index[vc >= PLACEHOLDER_SHARE_THRESH]
        if len(suspects) == 0:
            continue
        for v in suspects:
            syms = grp.loc[grp["flow_net_amount"] == v, "symbol"]
            if syms.nunique() >= PLACEHOLDER_SHARE_THRESH:
                bad.setdefault(str(dt_val), set()).update(syms.tolist())
    return bad


# ---------------------------------------------------------------------------
# 矩阵与聚合
# ---------------------------------------------------------------------------


def _wide_flow(flow: pd.DataFrame, days: list[str]) -> dict[str, pd.DataFrame]:
    """symbol×day 宽表：main / net_ratio / divergence / amount_y。"""
    g = flow.groupby(["symbol", "dt"], sort=True)[
        ["main", "net_ratio", "divergence", "amount_y"]
    ].mean()
    wide = g.unstack("dt")
    return {
        k: wide[k].reindex(columns=days)
        for k in ("main", "net_ratio", "divergence", "amount_y")
    }


def _streak_from_flag(flag: np.ndarray) -> np.ndarray:
    """连续为真天数（逐列递推，列=日期序）。"""
    s = np.zeros_like(flag, dtype=np.int32)
    for j in range(1, flag.shape[1]):
        s[:, j] = np.where(flag[:, j], s[:, j - 1] + 1, 0)
    return s


def _sector_mean(
    mat: pd.DataFrame, sym2sec: pd.Series, sec_names: list[str]
) -> pd.DataFrame:
    """成分等权均值（day 列保持）。无板块归属的 symbol 剔除。"""
    common = mat.index[mat.index.isin(sym2sec.index)]
    codes = sym2sec.reindex(common)
    return mat.loc[common].groupby(codes).mean().reindex(sec_names)


def _day_aggs(
    mask: pd.DataFrame,
    fwd: pd.DataFrame,
    days_sel: pd.Series | None = None,
) -> dict[str, pd.Series]:
    """逐日聚合（向量化，等价预研探针的逐日 python 循环）。

    仅在「该日条件样本 n>0 且有基准样本」的日参与统计（探针同口径：
    条件为空的日子连基准也一并跳过）。返回按日为索引的聚合量。
    """
    valid = fwd.notna()
    if days_sel is not None:
        valid = valid & days_sel.reindex(fwd.columns).fillna(False).astype(bool)
    cm = mask & valid
    out = {
        "n_c": cm.sum(axis=0),
        "s_c": fwd.where(cm).sum(axis=0),
        "w_c": (fwd.where(cm) > 0).sum(axis=0),
        "n_b": valid.sum(axis=0),
        "s_b": fwd.where(valid).sum(axis=0),
        "w_b": (fwd.where(valid) > 0).sum(axis=0),
    }
    return out


def _stats_from_aggs(aggs: dict[str, pd.Series], days: list[str]) -> dict[str, Any]:
    """聚合量 → 条件统计（全窗/日均超额 bp、hit/基准、逐年）。"""
    n_c, s_c, w_c = aggs["n_c"], aggs["s_c"], aggs["w_c"]
    n_b, s_b, w_b = aggs["n_b"], aggs["s_b"], aggs["w_b"]
    ok = (n_c > 0) & (n_b > 0)
    if not bool(ok.any()):
        return {"n": 0}
    n_c, s_c, w_c = n_c[ok], s_c[ok], w_c[ok]
    n_b, s_b, w_b = n_b[ok], s_b[ok], w_b[ok]
    n_cond, n_base = int(n_c.sum()), int(n_b.sum())
    day_excess = s_c / n_c - s_b / n_b
    year_of = pd.Series([d[:4] for d in days], index=days)
    yearly: dict[str, dict[str, Any]] = {}
    for y, g in day_excess.groupby(year_of.reindex(day_excess.index)):
        idx = g.index
        n_y = int(n_c[idx].sum())
        yearly[str(y)] = {
            "n": n_y,
            "excess_bp": round(float(g.mean()) * 1e4, 1),
            "hit": round(float(w_c[idx].sum() / n_y), 3) if n_y else None,
            "base_hit": round(float((w_b[idx] / n_b[idx]).mean()), 3),
        }
    return {
        "n": n_cond,
        "excess_bp": round(float(s_c.sum() / n_cond - s_b.sum() / n_base) * 1e4, 1),
        "excess_daily_bp": round(float(day_excess.mean()) * 1e4, 2),
        "hit": round(float(w_c.sum() / n_cond), 3),
        "base_hit": round(float(w_b.sum() / n_base), 3),
        "yearly": yearly,
    }


def _env_split(
    mask: pd.DataFrame,
    fwd: pd.DataFrame,
    env20: pd.Series,
) -> dict[str, dict[str, Any]]:
    """环境分层（市场 20 日累计环境 >0 / ≤0 两档；环境不可得的日两档都排除）。"""
    up = env20 > 0
    down = env20.notna() & (env20 <= 0)
    out: dict[str, dict[str, Any]] = {}
    for key, sel in (("up", up), ("down", down)):
        st = _stats_from_aggs(_day_aggs(mask, fwd, days_sel=sel), list(fwd.columns))
        out[key] = {
            "n": st.get("n", 0),
            "excess_bp": st.get("excess_bp"),
            "hit": st.get("hit"),
        }
    return out


def _ic_series(sig: pd.DataFrame, fwd: pd.DataFrame, min_pairs: int) -> pd.Series:
    """逐日 Spearman RankIC（rank 后按日 Pearson，向量化）。配对数不足落 NaN。"""
    mask = sig.notna() & fwd.notna()
    n = mask.sum(axis=0)
    a = sig.where(mask)
    b = fwd.where(mask)
    ra = a.rank(axis=0)
    rb = b.rank(axis=0)
    ra = ra - ra.mean(axis=0)
    rb = rb - rb.mean(axis=0)
    with np.errstate(invalid="ignore", divide="ignore"):
        cov = (ra * rb).sum(axis=0) / (n - 1)
        sa = np.sqrt((ra**2).sum(axis=0) / (n - 1))
        sb = np.sqrt((rb**2).sum(axis=0) / (n - 1))
        ic = cov / (sa * sb)
    ic = ic.replace([np.inf, -np.inf], np.nan)
    ic[n < min_pairs] = np.nan
    return ic


def _ic_report(label: str, ic: pd.Series) -> dict[str, Any]:
    ic = ic.dropna()
    if ic.empty:
        return {"label": label, "n_days": 0}
    mean, std = float(ic.mean()), float(ic.std())
    yearly = {
        str(y): round(float(g.mean()), 4) for y, g in ic.groupby(ic.index.str[:4])
    }
    return {
        "label": label,
        "n_days": int(len(ic)),
        "ic_mean": round(mean, 4),
        "ic_std": round(std, 4),
        "icir": round(mean / std, 3) if std > 0 else None,
        "pct_pos": round(float((ic > 0).mean()), 3),
        "t_stat": round(mean / std * float(np.sqrt(len(ic))), 2) if std > 0 else None,
        "yearly": yearly,
    }


# ---------------------------------------------------------------------------
# 主构建
# ---------------------------------------------------------------------------


def build(data_dir: Path, out_dir: Path, max_days: int = 0) -> dict[str, Any]:
    t0 = time.time()
    l2_all = _list_partition_dates(data_dir, L2_REL)
    if not l2_all:
        raise BuildError(f"无 {L2_REL} 分区：{data_dir}")
    days = l2_all[-max_days:] if max_days and max_days > 0 else l2_all
    log.info("窗口 %s..%s（%d 个分区）", days[0], days[-1], len(days))

    flow = _load_flow(data_dir, days)
    if flow.empty:
        raise BuildError("L2 资金流装载为空")
    log.info("L2 装载 %d 行（%.0fs）", len(flow), time.time() - t0)

    bad_map = _placeholder_symbols_by_day(flow)
    if bad_map:
        mask = pd.Series(
            [
                s in bad_map.get(d, ())
                for d, s in zip(flow["dt"], flow["symbol"], strict=True)
            ],
            index=flow.index,
        )
        dropped = int(mask.sum())
        flow = flow[~mask]
        log.info("占位污染剔除 %d 行（%d 个交易日）", dropped, len(bad_map))

    members = _load_members(data_dir)
    sym2sec = members.drop_duplicates("symbol").set_index("symbol")["sector_name"]
    sec_names = sorted(members["sector_name"].unique())
    log.info("板块 %d 个，成员 %d 只", len(sec_names), len(sym2sec))

    wide = _wide_flow(flow, days)
    del flow
    gc.collect()
    sig_main = wide["main"]
    sig_ratio = wide["net_ratio"]
    sig_div = wide["divergence"]
    sig_amount = wide["amount_y"]
    log.info("信号宽表 %s（%.0fs）", sig_main.shape, time.time() - t0)

    px = _load_close(data_dir, days)
    if px.empty:
        raise BuildError("qfq 收盘价装载为空")
    r1 = (px / px.shift(1, axis=1) - 1.0).replace([np.inf, -np.inf], np.nan)
    fwd_stock = {
        h: (px.shift(-h, axis=1) / px - 1.0).replace([np.inf, -np.inf], np.nan)
        for h in HORIZONS
    }
    exec_stock = {
        h: (px.shift(-(h + 1), axis=1) / px.shift(-1, axis=1) - 1.0).replace(
            [np.inf, -np.inf], np.nan
        )
        for h in HORIZONS
    }

    # ── 板块层矩阵 ───────────────────────────────────────────────────
    common = sig_main.index[sig_main.index.isin(sym2sec.index)]
    codes = sym2sec.reindex(common)
    main_m = sig_main.loc[common]
    sec_main = main_m.groupby(codes).sum(min_count=1).reindex(sec_names)
    sec_amount = (
        sig_amount.loc[common].groupby(codes).sum(min_count=1).reindex(sec_names)
    )
    sec_n = main_m.groupby(codes).count().reindex(sec_names)
    sec_n_pos = main_m.gt(0).groupby(codes).sum().reindex(sec_names)
    sec_ratio = (sec_main / sec_amount).where(sec_amount > 0)
    sec_breadth = (sec_n_pos / sec_n).where(sec_n > 0)
    sec_r1 = _sector_mean(r1, sym2sec, sec_names)
    # rolling(axis=1) 已被 pandas 弃用 → 转置走列向滚动（语义相同：沿日轴）
    sec_mom3 = sec_r1.T.rolling(3, min_periods=2).sum().T
    mom_rank = sec_mom3.rank(axis=0, pct=True)
    sec_fwd = {h: _sector_mean(fwd_stock[h], sym2sec, sec_names) for h in HORIZONS}
    sec_exec = {h: _sector_mean(exec_stock[h], sym2sec, sec_names) for h in HORIZONS}
    del exec_stock, sig_amount, main_m
    gc.collect()

    # 市场环境（t 日及以前 20 个交易日全市场等权累计；截至 t，无前视）
    mkt_r1 = r1.mean(axis=0)
    env20 = mkt_r1.rolling(20, min_periods=15).sum()

    sec_flag = (sec_main > 0).fillna(False).to_numpy()
    sec_streak_df = pd.DataFrame(
        _streak_from_flag(sec_flag), index=sec_names, columns=days
    )
    log.info("板块矩阵就绪（%.0fs）", time.time() - t0)

    # ── 条件统计（P 级：streak_2/3/5 × 双口径 × h=1/3/5 + 环境分层） ──
    conditions: dict[str, dict[str, Any]] = {}
    for k in (2, 3, 5):
        mask_k = sec_streak_df >= k
        fwd_stats = {
            str(h): _stats_from_aggs(_day_aggs(mask_k, sec_fwd[h]), days)
            for h in HORIZONS
        }
        exec_stats = {
            str(h): _stats_from_aggs(_day_aggs(mask_k, sec_exec[h]), days)
            for h in HORIZONS
        }
        env_stats = {str(h): _env_split(mask_k, sec_fwd[h], env20) for h in HORIZONS}
        s3f, s3e = fwd_stats[str(BADGE_HORIZON)], exec_stats[str(BADGE_HORIZON)]
        env3 = env_stats[str(BADGE_HORIZON)]
        n3 = int(s3f.get("n", 0))
        checks = {
            "n_min": n3 >= MIN_COND_N,
            "exec_h3_positive": float(s3e.get("excess_bp") or 0.0) > 0,
            "fwd_h3_positive": float(s3f.get("excess_bp") or 0.0) > 0,
            "yearly_any_positive": (
                any(v["excess_bp"] > 0 for v in (s3e.get("yearly") or {}).values())
                and any(v["excess_bp"] > 0 for v in (s3f.get("yearly") or {}).values())
            ),
            "env_double_positive": (
                float(env3["up"].get("excess_bp") or 0.0) > 0
                and float(env3["down"].get("excess_bp") or 0.0) > 0
            ),
        }
        cond: dict[str, Any] = {
            "level": k,
            "label": f"板块·连续主力净流入≥{k}日 → 板块等权前向收益",
            "years": len(s3f.get("yearly") or {}),
            "gate": {
                "pass": all(checks.values()),
                "evaluated_horizon": BADGE_HORIZON,
                "checks": checks,
                "criteria": GATE_CRITERIA,
            },
            "fwd": fwd_stats,
            "exec": exec_stats,
            "env": env_stats,
        }
        if k == 3:
            # 低调吸筹 / 追涨子分层（薄样本存档，不上主榜——细案 §12）
            cond["sub_splits"] = {
                "low_mom": _stats_from_aggs(
                    _day_aggs(
                        mask_k & (mom_rank < SUBSplit_MID_PCT), sec_fwd[BADGE_HORIZON]
                    ),
                    days,
                ),
                "high_mom": _stats_from_aggs(
                    _day_aggs(
                        mask_k & (mom_rank >= SUBSplit_MID_PCT), sec_fwd[BADGE_HORIZON]
                    ),
                    days,
                ),
                "note": "低调吸筹/追涨子分层：样本薄且全窗/日均口径打架，仅存档不上主榜",
            }
        conditions[f"streak_{k}"] = cond
        log.info(
            "条件 streak_%d：fwd3 n=%s 超额 %sbp / exec3 %sbp → gate %s",
            k,
            s3f.get("n"),
            s3f.get("excess_bp"),
            s3e.get("excess_bp"),
            "PASS" if cond["gate"]["pass"] else "FAIL",
        )

    # ── 个股层（全部实测否决 / 背景列，存档防误用） ───────────────────
    rejected: list[dict[str, Any]] = []
    ic_ratio = _ic_series(sig_ratio, fwd_stock[BADGE_HORIZON], IC_MIN_PAIRS_STOCK)
    mu = sig_ratio.T.rolling(20, min_periods=15).mean().T
    sd = sig_ratio.T.rolling(20, min_periods=15).std().T
    with np.errstate(invalid="ignore", divide="ignore"):
        z20 = (sig_ratio - mu) / sd
    ic_z20 = _ic_series(z20, fwd_stock[BADGE_HORIZON], IC_MIN_PAIRS_STOCK)
    ic_div = _ic_series(sig_div, fwd_stock[BADGE_HORIZON], IC_MIN_PAIRS_STOCK)

    within = pd.DataFrame(np.nan, index=sig_ratio.index, columns=sig_ratio.columns)
    for _sec, grp in members.groupby("sector_name"):
        syms = list(dict.fromkeys(grp["symbol"]))
        sub = sig_ratio.reindex(syms)
        sub = sub[sub.notna().any(axis=1)]
        if sub.empty:
            continue
        within.loc[sub.index] = sub.rank(axis=0, pct=True).to_numpy()
    ic_within = _ic_series(within, fwd_stock[BADGE_HORIZON], IC_MIN_PAIRS_STOCK)

    rejected.append(
        {
            "key": "stock_net_ratio_ic",
            "scope": "stock",
            "label": "个股·主力净额比 → 前向3日 RankIC",
            "verdict": "reject",
            "note": "全史实测 IC 近零，不具预测力；不参与展示与排序",
            "metrics": _ic_report("stock_net_ratio_ic", ic_ratio),
        }
    )
    rejected.append(
        {
            "key": "stock_ratio_z20_ic",
            "scope": "stock",
            "label": "个股·净额比 z20 惊喜 → 前向3日 RankIC",
            "verdict": "reject",
            "note": "全史实测 IC 近零；否决",
            "metrics": _ic_report("stock_ratio_z20_ic", ic_z20),
        }
    )
    rejected.append(
        {
            "key": "stock_divergence_ic",
            "scope": "stock",
            "label": "个股·主力散户背离 → 前向3日 RankIC",
            "verdict": "reject",
            "note": "微弱且逐年衰减至无；否决",
            "metrics": _ic_report("stock_divergence_ic", ic_div),
        }
    )
    rejected.append(
        {
            "key": "stock_within_sector_rank_ic",
            "scope": "stock",
            "label": "个股·板块内净额比 rank → 前向3日 RankIC",
            "verdict": "background",
            "note": "IC≤0.01：只作板块内选股背景列，不作预测排序依据",
            "metrics": _ic_report("stock_within_sector_rank_ic", ic_within),
        }
    )

    stock_flag = (sig_main > 0).fillna(False).to_numpy()
    stock_streak_df = pd.DataFrame(
        _streak_from_flag(stock_flag), index=sig_main.index, columns=days
    )
    for k in (2, 3, 5):
        st = _stats_from_aggs(
            _day_aggs(stock_streak_df >= k, fwd_stock[BADGE_HORIZON]), days
        )
        rejected.append(
            {
                "key": f"stock_streak_{k}",
                "scope": "stock",
                "label": f"个股·连续净流入≥{k}日 → 前向3日条件超额",
                "verdict": "reject",
                "note": "个股层 streak 条件超额近零；否决（板块层才是有效层）",
                "metrics": st,
            }
        )
        log.info(
            "个股 streak_%d：n=%s 超额 %sbp（否决列）",
            k,
            st.get("n"),
            st.get("excess_bp"),
        )

    # 纯动量对照（证明 streak 非动量代理）：板块近 3 日涨幅居前 → fwd3
    mom_mask = mom_rank >= MOM_TOP_PCT
    mom_stats = _stats_from_aggs(_day_aggs(mom_mask, sec_fwd[BADGE_HORIZON]), days)
    rejected.append(
        {
            "key": "sector_momentum_control",
            "scope": "sector",
            "label": "板块·近3日涨幅居前（纯动量对照）→ 前向3日超额",
            "verdict": "control",
            "note": "正交检验对照：裸动量≈零，用于证明 streak 超额不是价格动量代理",
            "metrics": mom_stats,
        }
    )
    log.info(
        "纯动量对照：n=%s 超额 %sbp", mom_stats.get("n"), mom_stats.get("excess_bp")
    )

    del z20, mu, sd, within, sig_div, fwd_stock
    gc.collect()

    # ── 面板 / latest / 证据档案 ─────────────────────────────────────
    idx = pd.MultiIndex.from_product([sec_names, days], names=["sector", "dt"])

    def _flat(mat: pd.DataFrame) -> np.ndarray:
        return (
            mat.reindex(index=sec_names, columns=days)
            .to_numpy(dtype="float64", copy=True)
            .ravel()
        )

    panel = pd.DataFrame(
        {
            "net_main": _flat(sec_main),
            "net_ratio": _flat(sec_ratio),
            "breadth": _flat(sec_breadth),
            "mom3": _flat(sec_mom3),
            "ret": _flat(sec_r1),
            "streak": _flat(sec_streak_df).astype(np.int32),
            "members": _flat(sec_n),
        },
        index=idx,
    ).reset_index()
    panel = panel[panel["members"].fillna(0) > 0].reset_index(drop=True)
    panel["members"] = panel["members"].astype(int)

    built_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    trade_date = days[-1]
    last_rows = panel[panel["dt"] == trade_date]
    sectors_map: dict[str, dict[str, Any]] = {}
    for r in last_rows.itertuples(index=False):
        sectors_map[str(r.sector)] = {
            "streak": int(r.streak),
            "net_main": round(float(r.net_main), 2),
            "net_ratio": None if pd.isna(r.net_ratio) else round(float(r.net_ratio), 4),
            "breadth": None if pd.isna(r.breadth) else round(float(r.breadth), 4),
            "members": int(r.members),
        }
    hits = sorted(
        (
            {
                "name": name,
                "streak": row["streak"],
                "net_main": row["net_main"],
                "net_ratio": row["net_ratio"],
            }
            for name, row in sectors_map.items()
            if row["streak"] >= 2
        ),
        key=lambda x: (-x["streak"], -(x["net_main"] or 0.0)),
    )
    latest = {
        "schema": flow_evidence.LATEST_SCHEMA,
        "trade_date": trade_date,
        "built_at": built_at,
        "window": [days[0], days[-1]],
        "hits": hits,
        "sectors": sectors_map,
    }

    evidence = {
        "schema": flow_evidence.EVIDENCE_SCHEMA,
        "built_at": built_at,
        "trade_date": trade_date,
        "window": [days[0], days[-1]],
        "min_cond_n": MIN_COND_N,
        "badge_levels": list(flow_evidence.BADGE_LEVELS),
        "badge_horizon": BADGE_HORIZON,
        "conventions": CONVENTIONS,
        "gate_criteria": GATE_CRITERIA,
        "coverage": {
            "days": len(days),
            "sectors": len(sec_names),
            "panel_rows": int(len(panel)),
        },
        "conditions": conditions,
        "rejected": rejected,
    }

    _validate(evidence, latest, panel)

    flow_evidence.write_parquet_atomic(panel, out_dir / "panel_sector.parquet")
    flow_evidence.write_json_atomic(out_dir / "evidence.json", evidence)
    # latest.json 最后写：它是在线入口的新鲜度标记，未随本轮收尾前保持旧版一致性
    flow_evidence.write_json_atomic(out_dir / "latest.json", latest)

    passed = [k for k, c in conditions.items() if c["gate"]["pass"]]
    summary = {
        "status": "success",
        "trade_date": trade_date,
        "window": [days[0], days[-1]],
        "days": len(days),
        "sectors": len(sec_names),
        "panel_rows": int(len(panel)),
        "hits": len(hits),
        "gate_pass": passed,
        "elapsed_s": round(time.time() - t0, 1),
        "out_dir": str(out_dir),
    }
    log.info("完成：%s", json.dumps(summary, ensure_ascii=False))
    return summary


def _validate(
    evidence: dict[str, Any], latest: dict[str, Any], panel: pd.DataFrame
) -> None:
    """落盘前自检：结构齐备、样本存在、板块覆盖合理；不过即 BuildError（不落盘）。"""
    conds = evidence["conditions"]
    if set(conds) != {"streak_2", "streak_3", "streak_5"}:
        raise BuildError(f"conditions 键不齐：{sorted(conds)}")
    for key, cond in conds.items():
        for scope in ("fwd", "exec"):
            for h in HORIZONS:
                st = cond[scope].get(str(h))
                if not st or int(st.get("n", 0)) <= 0:
                    raise BuildError(f"{key}.{scope}.{h} 无样本")
            s3 = cond[scope]["3"]
            for field in ("excess_bp", "excess_daily_bp", "hit", "base_hit", "yearly"):
                if field not in s3:
                    raise BuildError(f"{key}.{scope}.3 缺字段 {field}")
        if not isinstance(cond["gate"].get("pass"), bool):
            raise BuildError(f"{key}.gate.pass 缺失")
    if len(panel) < 1000:
        raise BuildError(f"panel_sector 行数异常：{len(panel)}")
    if len(latest["sectors"]) < 20:
        raise BuildError(f"latest.sectors 覆盖异常：{len(latest['sectors'])}")
    if not latest["hits"]:
        log.warning("命中榜（streak≥2）为空——当天无延续状态板块")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="构建资金流延续证据产物（P2）")
    parser.add_argument(
        "--data-dir",
        default=None,
        help="QuantDB 数据根（默认 env QM_QUANTDB_DATA_DIR 或 /data/quantdb）",
    )
    parser.add_argument(
        "--out-dir",
        default=None,
        help="产物目录（默认 env QM_FLOW_EVIDENCE_DIR 或容器/仓库标准位）",
    )
    parser.add_argument(
        "--max-days",
        type=int,
        default=0,
        help="仅取最近 N 个 L2 分区（快速验证；默认 0=全史）",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    data_dir = Path(
        args.data_dir or os.getenv("QM_QUANTDB_DATA_DIR") or "/data/quantdb"
    )
    out_dir = (
        Path(args.out_dir) if args.out_dir else flow_evidence.resolve_evidence_dir()
    )
    if args.max_days and args.max_days < 30:
        log.error("--max-days 至少 30（窗口过短统计无意义）")
        return 2
    try:
        build(data_dir=data_dir, out_dir=out_dir, max_days=args.max_days)
        return 0
    except BuildError as exc:
        log.error("构建失败（未落盘）：%s", exc)
        return 1
    except Exception:  # noqa: BLE001 - CLI 边界：任何异常都如实失败退出
        log.exception("构建异常（未落盘）")
        return 1


if __name__ == "__main__":
    sys.exit(main())
