"""因子去重：按秩相关把「同一份信息的多种写法」聚成簇，每簇只留一个代表。

用途：429 个 Alpha 因子（L1/L2 同理）里有大量同源变体 —— 例如
`a158_ROC20` 与 `gtja_088` 相关性 −0.978、`a101_040` 与 `gtja_042` 完全同源。
这些一起进模型等于同一份信息数两遍：放大噪声、扭曲特征重要性。

方法：以快照里的秩相关矩阵建图（边 = |ρ| ≥ 阈值），取**连通分量**作为簇；
每簇按「保留口径」选代表（默认 |ICIR| 最大，其次 |IC|），其余标为重复项并给出与代表的相关性。

注意：连通分量允许链式（A~B、B~C 但 A 与 C 不强相关），所以簇内每个成员都额外给出
「与代表的相关性」——间接相连的成员会明显低于阈值，一眼可辨。
"""

from __future__ import annotations

from typing import Any

import numpy as np


def _find(parent: dict[int, int], i: int) -> int:
    while parent[i] != i:
        parent[i] = parent[parent[i]]
        i = parent[i]
    return i


def cluster_by_correlation(
    names: list[str],
    matrix: list[list[float]],
    metrics: dict[str, dict[str, Any]],
    threshold: float = 0.9,
    keep: str = "icir",
) -> list[dict[str, Any]]:
    """返回去重簇（仅含 size ≥ 2 的簇），按「代表因子的强度」降序。

    metrics: {factor_name: {"ic_mean": .., "icir": .., "turnover": .., "display_name": ..}}
    keep:    icir | abs_ic | ls（保留口径）
    """
    if not names or not matrix:
        return []
    n = len(names)
    corr = np.asarray(matrix, dtype=np.float64)
    if corr.shape[0] != n:
        return []
    thr = float(threshold)

    parent = {i: i for i in range(n)}
    # 上三角扫描：|ρ| ≥ 阈值即连边（i<j 避免重复；对角线恒为 1 已排除）
    iu, ju = np.triu_indices(n, k=1)
    vals = np.abs(corr[iu, ju])
    for i, j in zip(iu[vals >= thr], ju[vals >= thr], strict=True):
        ri, rj = _find(parent, int(i)), _find(parent, int(j))
        if ri != rj:
            parent[ri] = rj

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(_find(parent, i), []).append(i)

    def strength(idx: int) -> float:
        m = metrics.get(names[idx]) or {}
        if keep == "abs_ic":
            return abs(float(m.get("ic_mean") or 0.0))
        if keep == "ls":
            return abs(float(m.get("ls_mean") or 0.0))
        icir = abs(float(m.get("icir") or 0.0))
        ic = abs(float(m.get("ic_mean") or 0.0))
        return icir * 1000 + ic  # 主键 ICIR，次键 |IC|（避免 ICIR 相同时随机）

    clusters: list[dict[str, Any]] = []
    for members in groups.values():
        if len(members) < 2:
            continue
        members = sorted(members, key=strength, reverse=True)
        rep_idx = members[0]
        rep = names[rep_idx]
        rep_metric = metrics.get(rep) or {}
        rows = []
        for idx in members:
            nm = names[idx]
            m = metrics.get(nm) or {}
            rows.append({
                "name": nm,
                "display_name": m.get("display_name"),
                "library": m.get("library"),
                "ic_mean": m.get("ic_mean"),
                "icir": m.get("icir"),
                "turnover": m.get("turnover"),
                "corr_to_rep": round(float(corr[rep_idx, idx]), 3),
                "is_rep": idx == rep_idx,
            })
        clusters.append({
            "size": len(members),
            "representative": rep,
            "representative_display": rep_metric.get("display_name"),
            "representative_icir": rep_metric.get("icir"),
            "representative_ic_mean": rep_metric.get("ic_mean"),
            "members": rows,
        })

    clusters.sort(key=lambda c: abs(float(c.get("representative_icir") or 0.0)), reverse=True)
    return clusters


def summarize(n_total: int, clusters: list[dict[str, Any]]) -> dict[str, Any]:
    """去重收益速览：能省掉多少因子、留下多少。"""
    dup = sum(c["size"] - 1 for c in clusters)
    return {
        "n_total": n_total,
        "n_clusters": len(clusters),
        "n_duplicates": dup,
        "n_keep": max(n_total - dup, 0),
        "largest_cluster": max((c["size"] for c in clusters), default=0),
    }
