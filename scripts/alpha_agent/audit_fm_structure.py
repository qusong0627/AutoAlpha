#!/usr/bin/env python3
"""T-FM-06 结构审计（离线只读）：110 内部结构重复度 + 与 T-FM-01 值级去重交叉验证。

产出两问的答案：

1. **现有库内部结构重复度**：110 条 FM-DSL 表达式的成对结构画像——
   AST 精确克隆（预期 0）、字面量变体簇（同形状仅窗口/常数不同）、
   trigram 余弦 ≥ θ 的近形对（θ 网格扫描）；
2. **θ 校准证据**：T-FM-01 报告里被值级 0.5 口径挤出的对（真值级重复）
   与警示对（0.4–0.5）的结构余弦分布，对比全库对的基线分布——近形阈值
   应落在「能捞回多数真重复」且「全库误报占比可接受」处。

注意证据边界：值级 ρ 只在 T-FM-01 贪心评估过的 308 对上有记录，
其余对「无证据」≠「不相关」——本审计只做正例召回与全库旗标规模，不做精确率。

用法::

    python scripts/alpha_agent/audit_fm_structure.py            # 默认报告路径
    python scripts/alpha_agent/audit_fm_structure.py --out-dir /tmp/audit
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path

import numpy as np

_project_root = Path(__file__).resolve().parents[2]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from backend.services.engine.alpha_agent.factorminer import (  # noqa: E402
    LIBRARY,
    structure,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("audit_fm_structure")

DEFAULT_TFM01_REPORT = (
    _project_root
    / "data/refs/factorminer/T-FM-01_factor_test_csi300_2021-10-09_2025-10-09.json"
)
DEFAULT_OUT_DIR = _project_root / "data/refs/factorminer"
#: θ 网格（near 阈值候选）
THETA_GRID = (0.5, 0.6, 0.7, 0.8, 0.9)


def _load_corpus() -> dict[str, dict]:
    """key（``fmNNN``）→ {ast, expression, prof}。"""
    corpus: dict[str, dict] = {}
    for factor in LIBRARY:
        key = f"fm{factor.fid}"
        corpus[key] = {
            "ast": factor.ast,
            "expression": factor.expression,
            "prof": structure.profile(factor.ast),
        }
    return corpus


def _pair_census(corpus: dict[str, dict]) -> dict:
    """全对结构画像：克隆 / 变体簇 / θ 网格旗标。"""
    keys = sorted(corpus)
    pairs: list[tuple[str, str, float, str]] = []  # (a, b, cosine, verdict)
    clones: list[tuple[str, str]] = []
    for a, b in combinations(keys, 2):
        sim = structure.similarity(corpus[a]["ast"], corpus[b]["ast"])
        if sim.clone:
            clones.append((a, b))
        elif sim.shape_equal:
            pairs.append((a, b, 1.0, structure.VERDICT_LITERAL_VARIANT))
        elif sim.trigram_cosine > 0.0:
            pairs.append((a, b, sim.trigram_cosine, "cosine"))

    # 变体簇：同 shape_key 分组
    by_shape: dict[str, list[str]] = {}
    for key in keys:
        by_shape.setdefault(corpus[key]["prof"].shape_key, []).append(key)
    clusters = [
        {"shape_key": shape, "members": members}
        for shape, members in sorted(by_shape.items())
        if len(members) >= 2
    ]

    cosine_pairs = [(a, b, c) for a, b, c, v in pairs if v == "cosine"]
    near_grid: dict[str, dict] = {}
    for theta in THETA_GRID:
        flagged = [(a, b, c) for a, b, c in cosine_pairs if c >= theta]
        near_grid[f"{theta:g}"] = {
            "n_variant_pairs": sum(
                1 for _, _, _, v in pairs if v == structure.VERDICT_LITERAL_VARIANT
            ),
            "n_near_only": len(flagged),
            "top": [
                {"a": a, "b": b, "cosine": round(c, 4)}
                for a, b, c in sorted(flagged, key=lambda item: -item[2])[:10]
            ],
        }
    all_cos = np.array([c for _, _, c in cosine_pairs], dtype=float)
    return {
        "n_pairs_total": len(keys) * (len(keys) - 1) // 2,
        "clones": clones,
        "variant_clusters": clusters,
        "near_grid": near_grid,
        "cosine_distribution": {
            "n_nonzero": int(all_cos.size),
            "p50": round(float(np.percentile(all_cos, 50)), 4)
            if all_cos.size
            else None,
            "p90": round(float(np.percentile(all_cos, 90)), 4)
            if all_cos.size
            else None,
            "p95": round(float(np.percentile(all_cos, 95)), 4)
            if all_cos.size
            else None,
            "p99": round(float(np.percentile(all_cos, 99)), 4)
            if all_cos.size
            else None,
            "max": round(float(all_cos.max()), 4) if all_cos.size else None,
        },
        "_cosine_pairs": cosine_pairs,  # 供交叉验证（不落 JSON 直接字段以外）
    }


def _cross_validate(corpus: dict[str, dict], census: dict, report_path: Path) -> dict:
    """与 T-FM-01 值级去重结果交叉：真重复对的结构余弦 vs 全库基线。"""
    report = json.loads(report_path.read_text(encoding="utf-8"))
    dedup = report["paper"]["dedup"]

    def _annotate(triples: list) -> list[dict]:
        rows = []
        for key, matched, rho in triples:
            a, b = f"fm{str(key)[-3:]}", f"fm{str(matched)[-3:]}"
            sim = structure.similarity(corpus[a]["ast"], corpus[b]["ast"])
            rows.append(
                {
                    "a": a,
                    "b": b,
                    "rho": round(float(rho), 4),
                    "cosine": round(sim.trigram_cosine, 4),
                    "verdict": structure.classify(sim),
                    "shape_equal": sim.shape_equal,
                }
            )
        return rows

    dropped = _annotate(dedup.get("dropped", []))
    warnings = _annotate(dedup.get("warnings", []))

    cosine_pairs = census["_cosine_pairs"]
    recall_grid: dict[str, dict] = {}
    for theta in THETA_GRID:
        caught = sum(
            1 for row in dropped if row["shape_equal"] or row["cosine"] >= theta
        )
        flagged = sum(1 for _, _, c in cosine_pairs if c >= theta)
        recall_grid[f"{theta:g}"] = {
            "caught_of_dropped": f"{caught}/{len(dropped)}",
            "flagged_near_pairs_in_corpus": flagged,
            "flagged_share_of_pairs": round(flagged / census["n_pairs_total"], 4),
        }
    return {
        "report_path": str(report_path),
        "reject": dedup.get("reject"),
        "warn": dedup.get("warn"),
        "dropped": dropped,
        "warnings": warnings,
        "recall_grid": recall_grid,
    }


def _fmt_threshold(v: object) -> str:
    """阈值展示：非数值（旧报告缺键/None）→ 「—」，不得让 ``:g`` 对 None 抛错。"""
    return f"{v:g}" if isinstance(v, (int, float)) else "—"


def _render_md(result: dict) -> str:
    census = result["census"]
    cross = result["cross_tfm01"]
    lines = [
        "# T-FM-06：FM 结构审计（110 内部结构重复度 + 值级去重交叉验证）",
        "",
        f"- 生成: {result['generated_at']}",
        f"- 语料: factorminer.library 110 条（成对 {census['n_pairs_total']} 对）",
        f"- 交叉证据: {cross.get('report_path')}"
        f"（值级口径 拒 |ρ|≥{_fmt_threshold(cross.get('reject'))}"
        f" / 警 {_fmt_threshold(cross.get('warn'))}）",
        "",
        "## 1. 精确克隆 / 字面量变体簇",
        "",
        f"- AST 精确克隆对: **{len(census['clones'])}**"
        + (
            f" → {census['clones']}" if census["clones"] else "（无——110 条无同式重复）"
        ),
        f"- 字面量变体簇（同形状仅窗口/常数不同）: **{len(census['variant_clusters'])} 簇**",
        "",
    ]
    for cluster in census["variant_clusters"]:
        lines.append(f"  - {'/'.join(cluster['members'])}：`{cluster['shape_key']}`")
    lines += [
        "",
        "## 2. trigram 余弦分布与 θ 网格",
        "",
        f"- 全库非零余弦对 {census['cosine_distribution']['n_nonzero']} 对；"
        f"p50={census['cosine_distribution']['p50']} p90={census['cosine_distribution']['p90']} "
        f"p95={census['cosine_distribution']['p95']} p99={census['cosine_distribution']['p99']} "
        f"max={census['cosine_distribution']['max']}",
        "",
        "| θ | 变体对 | 近形对（cos≥θ） | 近形对占全库 |",
        "|---|---|---|---|",
    ]
    for theta, row in census["near_grid"].items():
        share = row["n_near_only"] / census["n_pairs_total"]
        lines.append(
            f"| {theta} | {row['n_variant_pairs']} | {row['n_near_only']} | {share:.2%} |"
        )
    lines += [
        "",
        "## 3. 与 T-FM-01 值级去重交叉验证",
        "",
        "被值级挤出（|ρ|≥0.5）的 12 对，其结构画像：",
        "",
        "| 对 | 值级 ρ | trigram 余弦 | 结构判决 |",
        "|---|---|---|---|",
    ]
    for row in cross["dropped"]:
        lines.append(
            f"| {row['a']} vs {row['b']} | {row['rho']:+.2f} | {row['cosine']:.3f} | {row['verdict']} |"
        )
    lines += [
        "",
        "警示对（0.4≤|ρ|<0.5）结构画像：",
        "",
        "| 对 | 值级 ρ | trigram 余弦 |",
        "|---|---|---|",
    ]
    for row in cross["warnings"]:
        lines.append(
            f"| {row['a']} vs {row['b']} | {row['rho']:+.2f} | {row['cosine']:.3f} |"
        )
    lines += [
        "",
        "**θ 召回表**（捞回多少真重复 vs 全库旗标规模）：",
        "",
        "| θ | 捞回真重复 | 全库近形对 | 占全库对 |",
        "|---|---|---|---|",
    ]
    for theta, row in cross["recall_grid"].items():
        lines.append(
            f"| {theta} | {row['caught_of_dropped']} | {row['flagged_near_pairs_in_corpus']} "
            f"| {row['flagged_share_of_pairs']:.2%} |"
        )
    lines += [
        "",
        "## 4. 结论",
        "",
        result["conclusion"],
        "",
        "> 证据边界：值级 ρ 只在 T-FM-01 贪心评估过的对上有记录，其余对「无证据」≠「不相关」；"
        "本审计只做正例召回与旗标规模，不做精确率。",
    ]
    return "\n".join(lines)


def _conclude(census: dict, cross: dict) -> dict:
    """审计结论：结构闸的定位证据 + θ 标注线选取。

    关键发现（本审计存在的理由）：值级真重复与结构相似度**大面积脱钩**——
    12 对值级挤出对里多数结构上就是 novel（「结构异但值同」，须值级闸兜底）。
    因此结构闸**不是值级重复的预测器**，其角色 = ① clone 档（同式）计算前
    硬拒；② literal_variant / near 只做「形状拥挤」标注（生成循环的饱和方向
    输入）。θ 选取 = 旗标规模落在可读区间（≥5 对、≤ 全库 1%）的最大 ζ。
    """
    n_dropped = len(cross["dropped"])
    explained = sum(
        1
        for row in cross["dropped"]
        if row["shape_equal"] or row["cosine"] >= structure.DEFAULT_NEAR_THRESHOLD
    )
    chosen: float | None = None
    chosen_n = 0
    for theta in sorted(THETA_GRID, reverse=True):
        n = census["near_grid"][f"{theta:g}"]["n_near_only"]
        if n >= 5 and n <= 0.01 * census["n_pairs_total"]:
            chosen, chosen_n = theta, n
            break
    if chosen is None:
        return {
            "near_threshold": None,
            "clones": len(census["clones"]),
            "variant_clusters": len(census["variant_clusters"]),
            "value_dups_structurally_novel": f"{n_dropped - explained}/{n_dropped}",
            "basis": "网格内无 θ 落入可读区间——需人工定夺（见两张表）",
        }
    return {
        "near_threshold": chosen,
        "clones": len(census["clones"]),
        "variant_clusters": len(census["variant_clusters"]),
        "value_dups_structurally_novel": f"{n_dropped - explained}/{n_dropped}",
        "basis": (
            f"结构闸 = clone 硬拒 + 形状拥挤标注，**不是值级重复预测器**："
            f"值级挤出对中 {n_dropped - explained}/{n_dropped} 结构上 novel"
            f"（结构异但值同 → 值级库级闸兜底，T-FM-03）；"
            f"near 标注线 θ={chosen:g}（全库旗标 {chosen_n} 对，"
            f"{chosen_n / census['n_pairs_total']:.2%}）；"
            f"clone 档 {len(census['clones'])} 对（现库无同式，防未来生成循环重提）；"
            f"literal_variant {len(census['variant_clusters'])} 簇（变体探索对象，"
            f"交 T-FE-09 台账，不预拒）"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="T-FM-06 FM 结构审计")
    parser.add_argument("--report-json", type=Path, default=DEFAULT_TFM01_REPORT)
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR)
    args = parser.parse_args(argv)

    corpus = _load_corpus()
    logger.info("语料 %d 条，成对画像开始", len(corpus))
    census = _pair_census(corpus)
    logger.info(
        "克隆 %d 对 / 变体簇 %d 个",
        len(census["clones"]),
        len(census["variant_clusters"]),
    )
    cross = _cross_validate(corpus, census, args.report_json)
    conclusion = _conclude(census, cross)

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "corpus": {"n": len(corpus), "source": "factorminer.library"},
        "census": {k: v for k, v in census.items() if not k.startswith("_")},
        "cross_tfm01": cross,
        "conclusion": conclusion,
    }

    args.out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    json_path = args.out_dir / f"T-FM-06_fm_structure_audit_{stamp}.json"
    md_path = args.out_dir / f"T-FM-06_fm_structure_audit_{stamp}.md"
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    md = _render_md({**payload, "conclusion": conclusion["basis"]})
    md_path.write_text(md, encoding="utf-8")
    logger.info("报告已写：%s / %s", md_path, json_path)
    print(json.dumps(conclusion, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
