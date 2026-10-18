"""因子生命周期评估报告渲染（T-FE-06）。

从 ``backend/scripts/factor_lifecycle_eval.py`` 提取（评审 LOW-5，
2026-10-11：脚本超 800 行规则）——**纯函数**，无 IO 无时钟：动作计划桶
（``build_plan``）+ Markdown 渲染（``render_markdown``）+ 单元格/数字格
式化。脚本与测试从原路径继续导入（脚本 re-export），调用面不变。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from backend.shared.factor_lifecycle import (
    DECISION_HOLD,
    DECISION_OBSERVING_PROTECTED,
    READING_OK,
)


def build_plan(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """记录集 → 动作计划桶（纯函数；渲染与主流程共用）。"""
    verdicts = [r.get("verdict") or {} for r in records]
    return {
        "downweight_ids": [
            r["factor_id"] for r in records if r.get("action") == "downweight"
        ],
        "protected_ids": [
            r["factor_id"]
            for r, v in zip(records, verdicts, strict=True)
            if v.get("decision") == DECISION_OBSERVING_PROTECTED
        ],
        "already_ids": [
            r["factor_id"] for r in records if r.get("action") == "already"
        ],
        "weak_watch_ids": [
            r["factor_id"]
            for r, v in zip(records, verdicts, strict=True)
            if v.get("decision") == DECISION_HOLD
            and int(v.get("streak") or 0) >= 1
            and v.get("latest") is not None
            and r.get("eval_status") == READING_OK
        ],
        "by_eval_status": dict(
            Counter(str(r.get("eval_status") or "") for r in records)
        ),
    }


def md_cell(value: Any) -> str:
    """Markdown 表格单元格：``|``/换行转义（裸竖线会撕列）。"""
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def fmt_icir(value: Any) -> str:
    """ICIR 数字格：三位小数；不可计算 ``—``（缺失绝不显示成 0）。"""
    try:
        return f"{float(value):.3f}"
    except (TypeError, ValueError):
        return "—"


def render_markdown(report: Mapping[str, Any]) -> str:
    """评估报告 dict → Markdown（纯函数）。"""
    records = list(report.get("records") or [])
    plan = build_plan(records)
    summary = report.get("summary") or {}
    lines: list[str] = [
        "# 因子生命周期评估（T-FE-06）",
        "",
        f"模式：{report.get('mode')} ；asof：{report.get('asof')} ；"
        f"生成时间：{report.get('generated_at')}",
        "",
        "## 概览",
        "",
        "| 指标 | 值 |",
        "|------|----|",
        f"| 因子总数 | {summary.get('total', len(records))} |",
        f"| 读数状态分布 | {plan['by_eval_status']} |",
        f"| 降权动作 | {len(plan['downweight_ids'])}"
        f"（已降权维持 {len(plan['already_ids'])}） |",
        f"| 盲窗保护（连击达标但不动） | {len(plan['protected_ids'])} |",
        f"| 早警（连击 1-2 月） | {len(plan['weak_watch_ids'])} |",
        f"| 拥挤度（顺带记录，不裁决） | "
        f"{sum(1 for r in records if isinstance(r.get('crowding'), Mapping))} "
        f"个物化列有读数 |",
        "",
    ]
    if report.get("mode") == "apply":
        lines += [
            "## --apply 动作清单",
            "",
            f"- 实际写入因子数：{summary.get('applied_count', 0)}",
            f"- 台账：`{summary.get('ledger_path')}`",
            f"- 冻结报告：`{summary.get('report_md')}` / `{summary.get('report_json')}`",
            "",
            "- 训练口径（目录禁用/发布）走目录管理界面人工执行："
            "对下列物化列生成/编辑草稿后发布，本评估器不自动起草（细案 D8）。",
            "",
        ]

    def _table(title: str, subset: list[Mapping[str, Any]]) -> None:
        lines.append(f"## {title}")
        lines.append("")
        if not subset:
            lines.append("无。")
            lines.append("")
            return
        lines.extend(
            [
                "| factor_id | 因子名 | 状态(前→后) | 最新月末 | ICIR | 连击 | 物化列 | 动作 |",
                "|-----------|--------|-------------|----------|------|------|--------|------|",
            ]
        )
        for r in subset:
            latest = (r.get("verdict") or {}).get("latest") or {}
            lines.append(
                f"| {md_cell(str(r.get('factor_id') or '')[:12])} | {md_cell(r.get('factor_name'))}"
                f" | {md_cell(r.get('state_before'))}→{md_cell(r.get('state_after'))}"
                f" | {md_cell(latest.get('month_end'))} | {fmt_icir(latest.get('icir'))}"
                f" | {md_cell((r.get('verdict') or {}).get('streak'))}"
                f" | {md_cell(r.get('materialized_column') or '—')}"
                f" | {md_cell(r.get('action'))} |"
            )
        lines.append("")

    downweight = [r for r in records if r.get("action") in ("downweight", "already")]
    protected = [
        r
        for r in records
        if (r.get("verdict") or {}).get("decision") == DECISION_OBSERVING_PROTECTED
    ]
    weak_watch = [r for r in records if r["factor_id"] in set(plan["weak_watch_ids"])]
    _table("降权（弱读数连击 3 月末）", downweight)
    _table("盲窗保护（记录不动作）", protected)
    _table("早警（连击 1-2 月，未达裁决）", weak_watch)

    crowded = [r for r in records if isinstance(r.get("crowding"), Mapping)]
    lines += ["## 拥挤度（顺带记录，不裁决；细案 §7.1）", ""]
    if crowded:
        lines += [
            "| factor_id | 物化列 | score | 换手分位 | IC lag1 | n_days |",
            "|-----------|--------|-------|----------|---------|--------|",
        ]
        for r in crowded:
            c = r["crowding"]
            lines.append(
                f"| {md_cell(str(r.get('factor_id') or '')[:12])}"
                f" | {md_cell(r.get('materialized_column') or '—')}"
                f" | {fmt_icir(c.get('score'))}"
                f" | {fmt_icir(c.get('turnover_pct'))}"
                f" | {fmt_icir(c.get('ic_autocorr_lag1'))}"
                f" | {md_cell(c.get('n_days'))} |"
            )
    else:
        lines.append("无（无物化列或 factor_series 产物不可读）。")
    lines.append("")

    with_readings = [r for r in records if r.get("readings")]
    lines += ["## 读数明细（有序列因子）", ""]
    if with_readings:
        lines += [
            "| factor_id | 月末 → n_days / ICIR / 状态 |",
            "|-----------|---------------------------|",
        ]
        for r in with_readings:
            cells = "；".join(
                f"{rd.get('month_end')} → {rd.get('n_days')} / {fmt_icir(rd.get('icir'))}"
                f" / {rd.get('status')}"
                for rd in r.get("readings") or []
            )
            lines.append(
                f"| {md_cell(str(r.get('factor_id') or '')[:12])} | {md_cell(cells)} |"
            )
    else:
        lines.append("无。")
    lines += [
        "",
        "## 口径",
        "",
        "- 判定律：滚动 365 天窗口 ICIR=mean/std(ddof=1)，<0.2 记弱；"
        "连续 3 个自然月末弱 → 降权；任一回升/不可算读数清零连击（宁可漏判不可误判）。",
        "- 「不可算」桶（too_few_days / series_stale / no_completed_series / "
        "no_series_payload）永不冒充「差读数」，不参与连击。",
        "- 盲窗 = 首个 completed run（缺则行 created_at）+ 180 天；窗内达标不动作。",
        "- 拥挤度 = 既有 crowding_score（换手分位 + IC lag-1 自相关，factor_report "
        "口径）——顺带记录，不参与判定律、不触发任何动作；无读数记「无数据」。",
        "",
    ]
    return "\n".join(lines)
