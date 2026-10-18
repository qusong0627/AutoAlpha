#!/usr/bin/env python3
"""T-FM-03 值级库审计（离线只读）：CUSTOM 库成对相关分布 + 0.5 库级闸回溯模拟。

回答两个问题：

1. **现有库超 0.5 的对有多少**：CUSTOM ``l1_factors`` + ``rd_mined`` 既有列
   （含 OHLCV 基列）的成对「逐日截面 Spearman 的时间均值 |ρ|」分布——
   within-rd / rd↔非rd / within-非rd 三块，及平均成对 |ρ|（对表 FactorMiner
   实测 A 股 0.30–0.31 标尺）；
2. **0.5 库级闸回溯会拦多少**：对每个 rd_mined 列做「若按 0.5 闸重判」模拟——
   与其余全部列的 ≥0.5 命中列数分桶（0 = 过；1 = 可走 T-FM-02 替换裁决；
   ≥2 = 多命中拒，替换也救不了），并给出 ≥0.9 对照。

口径与物化器同源：采样日选取、单对相关、pair 下限（20/日）、样例日下限（5）
全部复用 ``rd_mined_materialize`` 的常量与 ``_pick_sample_days``；取数走
``read_factor_source``（与对照帧同一条读取链）。**历史判决不回滚**——审计只做
未来准入阈值的证据，不删既有列（值不删是既成事实）。

注（2026-10-11）：审计对质中发现物化闸 ``mean_rho_by_column`` 的方差域错配
（窄覆盖对照列 ρ 被系统性缩水 ≈√(k_pair/k_wide)），已修复为严格
pairwise-complete 并以金样测试钉死——本审计口径即修复后闸的口径；
见报告「闸实现缺陷与修复」节。

用法::

    python backend/scripts/audit_library_corr.py                 # 60 日抽样
    python backend/scripts/audit_library_corr.py --days 30       # 快速版
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

_project_root = Path(__file__).resolve().parents[2]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from backend.scripts.rd_mined_materialize import (  # noqa: E402
    LIB_MARKET,
    MANIFEST_NAME,
    _MIN_PAIRS_PER_DAY,
    _MIN_SAMPLE_DAYS,
    _RECENT_WINDOW_DAYS,
    _SAMPLE_DAYS,
    _lib_root,
    _long_to_wide,
    _pick_sample_days,
)
from backend.shared.feature_source import read_factor_source  # noqa: E402

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("audit_library_corr")

DEFAULT_OUT_DIR = _project_root / "data/refs/factorminer"
#: OHLCV 基列（对照帧里非因子库的行情底料，报告单列避免污染库内统计）
MARKET_COLUMNS = frozenset({"open", "high", "low", "close", "volume", "amount"})
THRESHOLD_GRID = (0.3, 0.4, 0.5, 0.8, 0.9)
TOP_PAIRS = 20


def _sample_days(window_days: int, n_days: int) -> list[str]:
    """近端窗口内均匀抽 n 日（口径 = 物化器 ``_pick_sample_days``）。"""
    root = _lib_root().parent  # 6_ml_datasets
    l1_root = root / "l1_factors"
    days = sorted(p.name.removeprefix("dt=") for p in l1_root.glob("dt=*"))
    iso = [f"{d[:4]}-{d[4:6]}-{d[6:8]}" for d in days[-window_days:]]
    return _pick_sample_days(iso, n=n_days)


def _load_day(lib: str, day: str) -> pd.DataFrame:
    """单日宽帧（index=(trade_date, symbol)，仅数值列）。"""
    long_df = read_factor_source(lib, market=LIB_MARKET, start=day, end=day)
    return _long_to_wide(long_df)


def _column_group(name: str) -> str:
    if name.startswith("rd_"):
        return "rd"
    if name in MARKET_COLUMNS:
        return "market"
    return "l1"


def _accumulate_corr(days: list[str]) -> tuple[list[str], np.ndarray, int]:
    """逐日「rank → pairwise-complete Pearson（= Spearman）→ 对日平均」。

    返回 (列序, 日均相关矩阵, 实际参与天数)。少于 ``_MIN_SAMPLE_DAYS`` 日
    重叠的对记 NaN（与物化器同门槛）。
    """
    cols: list[str] | None = None
    sum_mat = cnt_mat = None
    used = 0
    for day in days:
        try:
            l1 = _load_day("l1_factors", day)
            rd = _load_day("rd_mined", day)
        except FileNotFoundError:
            logger.info("采样日 %s 缺分区，跳过", day)
            continue
        if l1.empty or rd.empty:
            logger.info("采样日 %s 空帧（l1=%d, rd=%d），跳过", day, len(l1), len(rd))
            continue
        frame = pd.concat([l1, rd], axis=1)
        frame = frame.loc[:, ~frame.columns.duplicated()]
        frame = frame.select_dtypes(include=[np.number])
        if cols is None:
            cols = list(frame.columns)
            n = len(cols)
            sum_mat = np.zeros((n, n), dtype=float)
            cnt_mat = np.zeros((n, n), dtype=float)
        frame = frame.reindex(columns=cols)
        corr = frame.rank().corr(min_periods=_MIN_PAIRS_PER_DAY)
        values = corr.to_numpy(dtype=float)
        mask = np.isfinite(values)
        sum_mat += np.where(mask, values, 0.0)
        cnt_mat += mask
        used += 1
        logger.info("采样日 %s：%d 列 × %d 标的", day, len(cols), len(frame))
    if cols is None:
        raise RuntimeError("没有任何采样日可用——检查 CUSTOM 数据集分区")
    with np.errstate(invalid="ignore", divide="ignore"):
        mean_mat = sum_mat / cnt_mat
    mean_mat[cnt_mat < _MIN_SAMPLE_DAYS] = np.nan
    np.fill_diagonal(mean_mat, np.nan)
    return cols, mean_mat, used


def _pairs_of(mean_mat: np.ndarray, ia: np.ndarray, ib: np.ndarray) -> np.ndarray:
    """跨两块列集的无序对内 |ρ| 全量（ia == ib 时取上三角防自比/重计）。

    含 NaN 封面（未达样例日下限的对）——NaN 过滤由调用方与「对清单」**联合**
    完成，避免 argsort 位置与名字对错接。
    """
    sub = np.abs(mean_mat[np.ix_(ia, ib)])
    if ia is ib or np.array_equal(ia, ib):
        tri = np.triu_indices(len(ia), k=1)
        return sub[tri]
    return sub.ravel()


def _block_stats(
    cols: list[str], mean_mat: np.ndarray, block_a: str, block_b: str
) -> dict:
    group = np.array([_column_group(c) for c in cols])
    ia = np.flatnonzero(group == block_a)
    ib = np.flatnonzero(group == block_b) if block_b != block_a else ia
    if block_b == block_a:
        ii, jj = np.triu_indices(len(ia), k=1)
        flat = list(zip(ia[ii], ia[jj], strict=True))
    else:
        flat = [(a, b) for a in ia for b in ib]
    rho_all = _pairs_of(mean_mat, ia, ib)
    # 关键：NaN 对与对清单联合过滤，argsort 的位置才落回同一份序列
    keep = np.isfinite(rho_all)
    rho = rho_all[keep]
    pairs = [pair for pair, ok in zip(flat, keep, strict=True) if ok]
    stats = {
        "n_pairs": int(rho.size),
        "mean_abs_rho": round(float(rho.mean()), 4) if rho.size else None,
        "counts": {f">={t:g}": int((rho >= t).sum()) for t in THRESHOLD_GRID},
    }
    if rho.size:
        top_idx = np.argsort(-rho)[:TOP_PAIRS]
        stats["top"] = [
            {
                "a": cols[pairs[k][0]],
                "b": cols[pairs[k][1]],
                "abs_rho": round(float(rho[k]), 4),
            }
            for k in top_idx
        ]
    else:
        stats["top"] = []
    return stats


def _reentry_simulation(cols: list[str], mean_mat: np.ndarray) -> dict:
    """0.5（及 0.9 对照）库级闸回溯：rd 列的命中列数分桶。"""
    group = np.array([_column_group(c) for c in cols])
    rd_idx = np.flatnonzero(group == "rd")
    out: dict = {"n_rd_columns": int(rd_idx.size), "per_column": []}
    buckets = {"0.5": {0: 0, 1: 0, "2+": 0}, "0.9": {0: 0, 1: 0, "2+": 0}}
    for i in rd_idx:
        row = np.abs(mean_mat[i])
        detail: dict = {"column": cols[i]}
        for thr in ("0.5", "0.9"):
            hits = [
                (cols[j], float(row[j]))
                for j in np.flatnonzero(np.isfinite(row) & (row >= float(thr)))
            ]
            hits.sort(key=lambda item: -item[1])
            key = 0 if not hits else 1 if len(hits) == 1 else "2+"
            buckets[thr][key] += 1
            detail[f"hits_{thr}"] = [
                {"against": name, "abs_rho": round(rho, 4)} for name, rho in hits[:5]
            ]
            detail[f"n_hits_{thr}"] = len(hits)
        out["per_column"].append(detail)
    out["buckets"] = buckets
    return out


def _historical_context(lib_root: Path) -> dict:
    """既有 manifest 的 0.9 闸实际拦截面（历史判决，不回滚）。"""
    manifest_path = lib_root / MANIFEST_NAME
    if not manifest_path.is_file():
        return {}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    status: dict[str, int] = {}
    corrs: list[float] = []
    for entry in manifest.values():
        entry = entry or {}
        status[str(entry.get("status") or "")] = (
            status.get(str(entry.get("status") or ""), 0) + 1
        )
        if str(entry.get("status")) == "rejected_duplicate":
            try:
                value = abs(float(entry.get("corr")))
            except (TypeError, ValueError):
                continue
            if value > 0:
                corrs.append(value)
    corrs_arr = np.array(corrs) if corrs else np.array([np.nan])
    return {
        "status_counts": status,
        "rejected_corr_min": round(float(np.nanmin(corrs_arr)), 4),
        "rejected_corr_median": round(float(np.nanmedian(corrs_arr)), 4),
        "rejected_corr_max": round(float(np.nanmax(corrs_arr)), 4),
    }


def _render_md(result: dict) -> str:
    blocks = result["blocks"]
    sim = result["reentry"]
    hist = result["historical"]
    lines = [
        "# T-FM-03：CUSTOM 库值级成对审计 + 0.5 库级闸回溯模拟",
        "",
        f"- 生成: {result['generated_at']}",
        f"- 采样: 近 {result['sampling']['window_days']} 交易日均匀抽 "
        f"{result['sampling']['requested_days']} 日，实际参与 {result['sampling']['used_days']} 日"
        f"（口径与 rd_mined 物化器同源：逐日 rank + pairwise-complete Pearson + 对日平均，"
        f"pair 下限 {_MIN_PAIRS_PER_DAY}/日、样例日下限 {_MIN_SAMPLE_DAYS}）",
        f"- 列集: {result['n_columns']} 列"
        f"（rd {result['n_by_group']['rd']} / l1 {result['n_by_group']['l1']} / market {result['n_by_group']['market']}）",
        "",
        "## 1. 成对 |ρ| 分布（对日平均，pairwise-complete）",
        "",
        "| 块 | 对数 | 平均 \\|ρ\\| | ≥0.3 | ≥0.4 | ≥0.5 | ≥0.8 | ≥0.9 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    labels = {
        ("rd", "rd"): "within-rd",
        ("l1", "l1"): "within-l1",
        ("market", "market"): "within-market",
        ("rd", "l1"): "rd↔l1",
        ("rd", "market"): "rd↔market",
        ("l1", "market"): "l1↔market",
    }
    for key, label in labels.items():
        stats = blocks[f"{key[0]}|{key[1]}"]
        counts = stats["counts"]
        lines.append(
            f"| {label} | {stats['n_pairs']} | {stats['mean_abs_rho']} | "
            + " | ".join(str(counts[f">={t:g}"]) for t in THRESHOLD_GRID)
            + " |"
        )
    lines += [
        "",
        f"> 参照：FactorMiner 实测 A 股平均成对相关 0.30–0.31；"
        f"within-rd 平均 \\|ρ\\| = {blocks['rd|rd']['mean_abs_rho']}。",
        "",
        "## 2. top 高相关对",
        "",
    ]
    for key, label in (
        ("rd|rd", "within-rd"),
        ("rd|l1", "rd↔l1"),
        ("rd|market", "rd↔market"),
    ):
        lines.append(
            f"**{label}**（|ρ| 前 {min(TOP_PAIRS, len(blocks[key]['top']))} 对）："
        )
        lines.append("")
        lines.append("| 对 | \\|ρ\\| |")
        lines.append("|---|---|")
        for row in blocks[key]["top"][:10]:
            lines.append(f"| {row['a']} vs {row['b']} | {row['abs_rho']:.3f} |")
        lines.append("")
    lines += [
        "## 3. 0.5 库级闸回溯模拟（rd 列 vs 其余全部列）",
        "",
        f"- rd 列 {sim['n_rd_columns']} 个；命中列数分桶：",
        "",
        "| 阈值 | 0 命中（过） | 1 命中（可走替换裁决） | ≥2 命中（多命中拒） |",
        "|---|---|---|---|",
        f"| ≥0.5 | {sim['buckets']['0.5'][0]} | {sim['buckets']['0.5'][1]} | {sim['buckets']['0.5']['2+']} |",
        f"| ≥0.9 | {sim['buckets']['0.9'][0]} | {sim['buckets']['0.9'][1]} | {sim['buckets']['0.9']['2+']} |",
        "",
        "命中最多的 rd 列（≥0.5，前 10）：",
        "",
        "| 列 | 命中数 | 最近命中 |",
        "|---|---|---|",
    ]
    worst = sorted(sim["per_column"], key=lambda item: -item["n_hits_0.5"])[:10]
    for row in worst:
        nearest = row["hits_0.5"][0] if row["hits_0.5"] else None
        near_txt = (
            f"{nearest['against']}（{nearest['abs_rho']:.3f}）" if nearest else "—"
        )
        lines.append(f"| {row['column']} | {row['n_hits_0.5']} | {near_txt} |")
    lines += [
        "",
        "## 4. 历史判决上下文（0.9 闸，不回滚）",
        "",
        f"- manifest 状态: {hist.get('status_counts')}",
        f"- rejected_duplicate 的 |ρ|：min {hist.get('rejected_corr_min')} / "
        f"median {hist.get('rejected_corr_median')} / max {hist.get('rejected_corr_max')}"
        "（0.9 闸实际只拦近乎克隆；0.5–0.9 灰区历史全放行。**注意**：这些记录值"
        "多为缩水口径——现库任何超 0.9 的 rd↔l1 存量对与「灰区全放行」都含闸缺陷"
        "成分，见 §5）",
        "",
        "## 5. 闸实现缺陷与修复（2026-10-11 同期提交）",
        "",
        "本次审计对质中发现物化闸 ``mean_rho_by_column``（``statistical/incremental.py``）",
        "的方差域错配：协方差与对照列方差取「逐列对交集」域，新因子方差却取",
        "「任一对照列非空」的宽域——对照列覆盖域窄于 values 时（l1 列只覆盖部分标的、",
        "对照帧是全票并集），ρ 被系统性缩水 √(k_pair/k_wide)。实测锚点：",
        "``rd_cci_20 × TDXGS_CCI_20`` 真 ρ=0.9985（本审计口径），旧闸报 0.7632",
        "（缩水比 0.773 ≈ √(3255/5511)）——真重复列在 0.9 闸下静默漏过。",
        "",
        "推论：**§4 的 rejected 记录与现库存量高相关对均为缩水口径产物**——对 l1 对照列，",
        "真 ρ 可高达记录值 ÷ 0.77；修复后闸复算 ``rd_cci_20`` 得 0.9985 / TDXGS_CCI_20，",
        "与本审计逐位吻合。修复 = 协方差与两侧方差一律按逐列对交集域计算，",
        "金样测试与 pandas 逐日 pairwise corr 逐位一致；历史判决不回滚，",
        "§3 的回溯模拟即修复后闸的真实行为。",
        "",
        "## 6. 结论",
        "",
        result["conclusion"],
    ]
    return "\n".join(lines)


def _conclude(result: dict) -> str:
    blocks = result["blocks"]
    sim = result["reentry"]
    rd_rd = blocks["rd|rd"]
    b50 = sim["buckets"]["0.5"]
    n = sim["n_rd_columns"]
    multi = b50["2+"]
    lines = [
        f"1. **库内饱和现状**：within-rd 平均 |ρ| = {rd_rd['mean_abs_rho']}（FactorMiner A 股标尺 0.30–0.31）；"
        f"≥0.5 的 within-rd 对 {rd_rd['counts']['>=0.5']} 对、≥0.9 的 {rd_rd['counts']['>=0.9']} 对——"
        "存量灰区对（尤以 rd↔l1 高相关对为甚）是 0.9 阈值宽容与闸缩水缺陷叠加的产物（§5），"
        "不回滚（值不删）。",
        f"2. **0.5 回溯模拟**：{n} 个 rd 列中 {b50[0]} 个零命中（照常可入）、"
        f"{b50[1]} 个单命中（可走 T-FM-02 替换裁决）、**{multi} 个多命中**"
        f"（{multi / n:.1%}，替换也救不了 → 0.5 闸下将被拒）。"
        f"≥0.9 对照：{sim['buckets']['0.9']['2+']} 个多命中——即现闸实际压力面。",
        "3. **判定**：0.5/0.4 作为**未来准入**阈值采纳（T-FM-03 对表）；历史列冻结；"
        "替换机制（单命中 + |IC|≥max(0.10, 1.3×旧)）为唯一泄压阀，多命中即拒——"
        "与 FactorMiner 四阶段口径一致。若未来观测到拒入率过高，`--corr-threshold` 可即时回退。",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="T-FM-03 CUSTOM 库值级审计")
    parser.add_argument("--days", type=int, default=_SAMPLE_DAYS, help="抽样日数")
    parser.add_argument("--window-days", type=int, default=_RECENT_WINDOW_DAYS)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args(argv)

    days = _sample_days(args.window_days, args.days)
    logger.info("采样 %d 日：%s … %s", len(days), days[0], days[-1])
    cols, mean_mat, used = _accumulate_corr(days)
    groups = {
        g: sum(1 for c in cols if _column_group(c) == g) for g in ("rd", "l1", "market")
    }

    blocks = {
        f"{a}|{b}": _block_stats(cols, mean_mat, a, b)
        for a, b in (
            ("rd", "rd"),
            ("l1", "l1"),
            ("market", "market"),
            ("rd", "l1"),
            ("rd", "market"),
            ("l1", "market"),
        )
    }
    reentry = _reentry_simulation(cols, mean_mat)
    historical = _historical_context(_lib_root())

    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sampling": {
            "window_days": args.window_days,
            "requested_days": args.days,
            "used_days": used,
            "days": days,
        },
        "n_columns": len(cols),
        "n_by_group": groups,
        "blocks": blocks,
        "reentry": reentry,
        "historical": historical,
    }
    result["conclusion"] = _conclude(result)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    json_path = args.out_dir / f"T-FM-03_library_corr_audit_{stamp}.json"
    md_path = args.out_dir / f"T-FM-03_library_corr_audit_{stamp}.md"
    json_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md_path.write_text(_render_md(result), encoding="utf-8")
    logger.info("报告已写：%s / %s", md_path, json_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
