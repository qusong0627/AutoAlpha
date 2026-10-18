#!/usr/bin/env python3
"""因子注册中心报告（T-FE-07）：命名违约 + code_fp 失配与下游闭包 + 标签覆盖。

来源：``docs/因子挖掘_T-FE-06_07_生命周期与六维标签_细案_20261011.md``
（§三 D3/D4、§四 #6）。本报告是**可见性**工具，不是门禁：

- 命名规范 3 条（细案 D3）：① factor_name 非空且 ≤64 字符；② ``fm:`` 前缀
  与 ``metadata.source`` 声明一致；③ 物化列名 = ``feature_column_name``
  经物化器 ``_disambiguate_column`` 消歧的结果（复演对拍，算法单一来源）。
  违约只列清单、不阻塞、不大扫除。
- code_fp 失配（细案 D4）：当前 ``code_fingerprint(factor_code)`` vs 物化
  manifest 条目 ``code_fp``（同一函数）→ 活跃物化列的失配附**下游闭包**
  （引用该列的 catalog mapping 版本）。重物化仍由物化器自理（其已有
  code_changed 逻辑），本报告只做可见性。
- 六维标签覆盖统计：读时派生（``shared/factor_tags``），缺失记 ``unknown``
  不计覆盖；maturity 由生命周期状态机派生（``shared/factor_lifecycle``）。

用法:
    # 全量（容器内跑，需 DB）：
    python backend/scripts/factor_registry_report.py

    # 限市场/限量：
    python backend/scripts/factor_registry_report.py --market a_share --limit 100

    # 指定输出目录（默认 <data>/refs/factor_registry/）：
    python backend/scripts/factor_registry_report.py --out-dir /tmp/reg

产物：``registry_report_YYYYMMDD.{md,json}``（冻结报告区，审计留档）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.shared.factor_identity import (  # noqa: E402
    code_fingerprint,
    feature_column_name,
)
from backend.shared.factor_lifecycle import (  # noqa: E402
    blind_until_of,
    has_completed_backtest,
    maturity_of,
    resolve_state,
)
from backend.shared.factor_tags import (  # noqa: E402
    TAG_DIMENSIONS,
    derive_tags,
    extract_expression_deps,
    read_metadata,
)
from backend.shared.utc_datetime import to_utc_iso  # noqa: E402

logger = logging.getLogger("factor_registry_report")

#: 命名规范：factor_name 长度上限（细案 D3）。
NAME_MAX_CHARS = 64

#: FM-DSL 表达式前缀（与 ``factor_tags._FM_PREFIX``/``dsl.FM_CODE_PREFIX`` 同值）。
FM_PREFIX = "fm:"

_UNKNOWN = "unknown"

REPORT_SCHEMA_VERSION = 1


# ------------------------------------------------------------------ 纯函数


def analyze_factor(
    row: Mapping[str, Any],
    *,
    now: datetime,
    first_completed_run_at: datetime | None = None,
    manifest_entry: Mapping[str, Any] | None = None,
    expected_column: str | None = None,
) -> dict[str, Any]:
    """因子行 → 报告记录（纯函数；全部输入含时间，无 IO 无时钟）。

    ``expected_column`` = 调用方按物化器算法复演的预期列名（None 时跳过
    列名对拍——不猜）。
    """
    fid = str(row.get("factor_id") or "")
    name = str(row.get("factor_name") or "")
    code = str(row.get("factor_code") or "")
    meta = read_metadata(row)
    life = meta.get("lifecycle")
    life = life if isinstance(life, Mapping) else {}

    blind_until = blind_until_of(
        created_at=row.get("created_at"),
        first_completed_run_at=first_completed_run_at,
    )
    state = resolve_state(
        explicit_state=life.get("state"),
        has_completed_run=has_completed_backtest(
            row, first_completed_run_at=first_completed_run_at
        ),
        blind_until=blind_until,
        now=now,
    )
    maturity = maturity_of(state)

    violations: list[str] = []
    if not name.strip():
        violations.append("name_empty")
    elif len(name.strip()) > NAME_MAX_CHARS:
        violations.append("name_too_long")

    claimed_source = str(meta.get("source") or meta.get("algorithm") or "")
    if claimed_source == "factorminer" and not code.startswith(FM_PREFIX):
        violations.append("fm_prefix_missing")
    elif (
        code.startswith(FM_PREFIX)
        and claimed_source
        and claimed_source != "factorminer"
    ):
        violations.append("fm_prefix_conflict")

    materialization: dict[str, Any] | None = None
    if manifest_entry is not None:
        column = str(manifest_entry.get("column") or "")
        stored_fp = str(manifest_entry.get("code_fp") or "") or None
        current_fp = code_fingerprint(code)
        if column and expected_column is not None and column != expected_column:
            violations.append("column_mismatch")
        materialization = {
            "column": column or None,
            "status": str(manifest_entry.get("status") or ""),
            "code_fp": stored_fp,
            "current_code_fp": current_fp,
            # 存储指纹存在且与当前不一致（含代码被清空 → current None）
            "code_mismatch": bool(stored_fp and current_fp != stored_fp),
        }

    return {
        "factor_id": fid,
        "factor_name": name,
        "market": str(row.get("market") or ""),
        "status": str(row.get("status") or ""),
        "state": state,
        "maturity": maturity,
        "blind_until": to_utc_iso(blind_until),
        "tags": derive_tags(row, maturity=maturity),
        "deps": extract_expression_deps(code),
        "materialization": materialization,
        "naming_violations": violations,
    }


def build_report(
    records: Sequence[Mapping[str, Any]],
    *,
    catalog_rows: Sequence[Mapping[str, Any]],
    generated_at: datetime,
) -> dict[str, Any]:
    """记录集 + catalog 引用行 → 报告 dict（纯函数）。"""
    by_state = Counter(str(r.get("state") or "") for r in records)
    naming_violations = [
        {
            "factor_id": r["factor_id"],
            "factor_name": r["factor_name"],
            "codes": list(r["naming_violations"]),
        }
        for r in records
        if r.get("naming_violations")
    ]

    downstream_by_column: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in catalog_rows:
        column = str(row.get("source_column") or "")
        if column:
            downstream_by_column[column].append(
                {
                    "version_id": row.get("version_id"),
                    "version_name": row.get("version_name"),
                    "version_status": row.get("version_status"),
                    "enabled": bool(row.get("enabled")),
                }
            )

    materialized = [
        r
        for r in records
        if r.get("materialization")
        and r["materialization"].get("status") == "materialized"
    ]
    code_drift = [
        {
            "factor_id": r["factor_id"],
            "factor_name": r["factor_name"],
            "column": r["materialization"]["column"],
            "stored_fp": r["materialization"]["code_fp"],
            "current_fp": r["materialization"]["current_code_fp"],
            "downstream": downstream_by_column.get(
                str(r["materialization"]["column"] or ""), []
            ),
        }
        for r in materialized
        if r["materialization"].get("code_mismatch")
    ]
    drift_inactive_count = sum(
        1
        for r in records
        if r.get("materialization")
        and r["materialization"].get("code_mismatch")
        and r["materialization"].get("status") != "materialized"
    )

    total = len(records)
    tag_coverage: dict[str, dict[str, Any]] = {}
    for dim in TAG_DIMENSIONS:
        values = Counter(
            str((r.get("tags") or {}).get(dim) or _UNKNOWN) for r in records
        )
        covered = sum(v for k, v in values.items() if k not in (_UNKNOWN, ""))
        tag_coverage[dim] = {
            "covered": covered,
            "total": total,
            "ratio": round(covered / total, 4) if total else 0.0,
            "top": dict(values.most_common(8)),
        }

    field_counter: Counter[str] = Counter()
    operator_counter: Counter[str] = Counter()
    window_counter: Counter[str] = Counter()
    for r in records:
        deps = r.get("deps") or {}
        field_counter.update(str(x) for x in deps.get("fields") or [])
        operator_counter.update(str(x) for x in deps.get("operators") or [])
        window_counter.update(str(x) for x in deps.get("windows") or [])

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": to_utc_iso(generated_at),
        "summary": {
            "total": total,
            "by_state": dict(by_state),
            "materialized_count": len(materialized),
            "naming_violation_count": len(naming_violations),
            "code_drift_count": len(code_drift),
            "code_drift_inactive_count": drift_inactive_count,
        },
        "naming_violations": naming_violations,
        "code_drift": code_drift,
        "tag_coverage": tag_coverage,
        "deps_top": {
            "fields": dict(field_counter.most_common(10)),
            "operators": dict(operator_counter.most_common(10)),
            "windows": dict(window_counter.most_common(10)),
        },
    }


def _md_cell(value: Any) -> str:
    """表格单元格：转字符串并中和裸竖线/换行（防撕列）。"""
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ")


def render_markdown(report: Mapping[str, Any]) -> str:
    """报告 dict → Markdown（纯函数）。"""
    summary = report.get("summary") or {}
    by_state = summary.get("by_state") or {}
    lines: list[str] = [
        "# 因子注册中心报告（T-FE-07）",
        "",
        f"生成时间：{report.get('generated_at')} ；因子总数：{summary.get('total', 0)}",
        "",
        "## 概览",
        "",
        "| 指标 | 值 |",
        "|------|----|",
        f"| 状态分布 | 在用 {by_state.get('active', 0)} / 观察 {by_state.get('observing', 0)}"
        f" / 实验 {by_state.get('experiment', 0)} / 退役 {by_state.get('downweighted', 0)} |",
        f"| 已物化（manifest materialized） | {summary.get('materialized_count', 0)} |",
        f"| 命名违约 | {summary.get('naming_violation_count', 0)} |",
        f"| code_fp 失配（活跃物化列） | {summary.get('code_drift_count', 0)}"
        f"（另有非活跃 {summary.get('code_drift_inactive_count', 0)}） |",
        "",
        "## 命名违约（D3 三条）",
        "",
    ]
    violations = report.get("naming_violations") or []
    if violations:
        lines += ["| factor_id | 因子名 | 违约码 |", "|-----------|--------|--------|"]
        lines += [
            f"| {_md_cell(v.get('factor_id'))} | {_md_cell(v.get('factor_name'))}"
            f" | {_md_cell('、'.join(v.get('codes') or []))} |"
            for v in violations
        ]
    else:
        lines.append("无。")
    lines += ["", "## code_fp 失配与下游闭包（D4）", ""]
    drift = report.get("code_drift") or []
    if drift:
        lines += [
            "| factor_id | 列名 | 存储指纹 | 当前指纹 | 下游 catalog 版本 |",
            "|-----------|------|----------|----------|-------------------|",
        ]
        for d in drift:
            downstream = "、".join(
                f"{x.get('version_name')}({x.get('version_id')},{x.get('version_status')})"
                for x in d.get("downstream") or []
            )
            lines.append(
                f"| {_md_cell(d.get('factor_id'))} | {_md_cell(d.get('column'))}"
                f" | {_md_cell(d.get('stored_fp'))} | {_md_cell(d.get('current_fp'))}"
                f" | {_md_cell(downstream or '（无目录引用）')} |"
            )
    else:
        lines.append("无。")
    lines += [
        "",
        "## 标签覆盖（六维）",
        "",
        "| 维度 | 覆盖 | 占比 | 主要取值 |",
        "|------|------|------|----------|",
    ]
    for dim in TAG_DIMENSIONS:
        cov = (report.get("tag_coverage") or {}).get(dim) or {}
        top = "、".join(f"{k}×{v}" for k, v in (cov.get("top") or {}).items())
        lines.append(
            f"| {dim} | {cov.get('covered', 0)}/{cov.get('total', 0)}"
            f" | {cov.get('ratio', 0)} | {_md_cell(top)} |"
        )
    deps = report.get("deps_top") or {}
    lines += [
        "",
        "## 依赖要素 Top（表达式字符串级抽取）",
        "",
        f"- 字段：{'、'.join(f'{k}×{v}' for k, v in (deps.get('fields') or {}).items()) or '—'}",
        f"- 算子：{'、'.join(f'{k}×{v}' for k, v in (deps.get('operators') or {}).items()) or '—'}",
        f"- 窗口：{'、'.join(f'{k}×{v}' for k, v in (deps.get('windows') or {}).items()) or '—'}",
        "",
        "## 口径",
        "",
        "- 本报告只做**可见性**：违约与失配不阻塞写入；重物化由物化器自理"
        "（其已有 code_changed 逻辑），目录发布仍走人工闸门。",
        "- code_fp = ``code_fingerprint(factor_code)``（与物化同函数）；"
        "状态/成熟度由生命周期状态机读时派生（无回填）。",
        "- 缺失标签一律 unknown，不计覆盖。",
        "",
    ]
    return "\n".join(lines)


# ------------------------------------------------------------------ IO 面


def data_root() -> Path:
    """数据根：容器内由 QM_*_DATA_DIR 定位（/data），宿主回退仓库 data/。

    与 ``factor_lifecycle_eval`` 共用的唯一解析点（两脚本同进程/异进程
    皆可；refs 报告、评估台账都挂在这棵树下）。
    """
    env_root = os.getenv("QM_QUANTCUSTOM_DATA_DIR") or os.getenv("QM_QUANTDB_DATA_DIR")
    if env_root:
        return Path(env_root).resolve().parent
    return PROJECT_ROOT / "data"


def load_manifest() -> dict[str, Any]:
    """读物化 manifest（复用物化器 loader：缺失/坏文件按空清单）。

    报告面不因 manifest 读取失败而崩——降级空清单 + 告警。
    """
    try:
        from backend.scripts.rd_mined_materialize import _lib_root, _load_manifest

        return _load_manifest(_lib_root())
    except Exception as exc:  # noqa: BLE001 - 报告面可见性降级
        logger.warning("物化 manifest 读取失败，按空清单继续：%s", exc)
        return {}


def manifest_owners(manifest: Mapping[str, Any]) -> dict[str, str]:
    """列名→factor_id 占用表（复用物化器 ``_column_owners`` 单源）。"""
    try:
        from backend.scripts.rd_mined_materialize import _column_owners

        return _column_owners(manifest)
    except Exception as exc:  # noqa: BLE001
        logger.warning("占用表构建失败：%s", exc)
        return {}


def expected_column_of(row: Mapping[str, Any], owners: Mapping[str, str]) -> str | None:
    """复演物化器预期列名（消歧算法单一来源；失败返回 None=跳过对拍）。"""
    try:
        from backend.scripts.rd_mined_materialize import _disambiguate_column

        base = feature_column_name(str(row.get("factor_name") or ""))
        return _disambiguate_column(base, str(row.get("factor_id") or ""), owners)
    except Exception as exc:  # noqa: BLE001
        logger.warning("预期列名复演失败（跳过对拍）：%s", exc)
        return None


async def fetch_factors(
    *, market: str | None = None, limit: int | None = None
) -> list[dict[str, Any]]:
    """读因子行（只读）。"""
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    where = "WHERE market = :market" if market else ""
    limit_clause = "LIMIT :limit" if limit else ""
    params: dict[str, Any] = {}
    if market:
        params["market"] = market
    if limit:
        params["limit"] = int(limit)
    async with get_session(read_only=True) as session:
        rows = await session.execute(
            text(
                f"""
                SELECT factor_id, factor_name, factor_code, status, market,
                       metadata_json, created_at
                FROM rd_agent_factors
                {where}
                ORDER BY created_at DESC, factor_id
                {limit_clause}
                """
            ),
            params,
        )
        return [dict(r) for r in rows.mappings().all()]


async def fetch_catalog_rows(columns: Sequence[str]) -> list[dict[str, Any]]:
    """引用给定物化列的 catalog mapping（连同所属版本）。"""
    if not columns:
        return []
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    try:
        async with get_session(read_only=True) as session:
            rows = await session.execute(
                text(
                    """
                    SELECT m.source_column, m.version_id, m.enabled,
                           v.version_name, v.status AS version_status
                    FROM qm_training_factor_mapping m
                    JOIN qm_training_factor_catalog_version v
                      ON v.version_id = m.version_id
                    WHERE m.source_column = ANY(:columns)
                    """
                ),
                {"columns": list(columns)},
            )
            return [dict(r) for r in rows.mappings().all()]
    except Exception as exc:  # noqa: BLE001 - 目录表未建（新装）时装饰面降级
        logger.warning("catalog 引用查询失败（按空继续）：%s", exc)
        return []


def write_report(report: Mapping[str, Any], out_dir: Path) -> tuple[Path, Path]:
    """落盘 md + json（原子替换）。"""
    stamp = str(report.get("generated_at") or "")[:10].replace("-", "") or "unknown"
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"registry_report_{stamp}.md"
    json_path = out_dir / f"registry_report_{stamp}.json"
    md_text = render_markdown(report)
    for path, text in (
        (md_path, md_text),
        (json_path, json.dumps(report, ensure_ascii=False, indent=2)),
    ):
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    return md_path, json_path


async def main_async(args: argparse.Namespace) -> int:
    from backend.services.engine.factor_backtest import store as fb_store

    now = datetime.now(timezone.utc)
    rows = await fetch_factors(market=args.market, limit=args.limit)
    manifest = load_manifest()
    owners = manifest_owners(manifest)
    factor_ids = [str(r.get("factor_id") or "") for r in rows if r.get("factor_id")]
    first_map = await fb_store.first_completed_at(factor_ids)

    records: list[dict[str, Any]] = []
    for row in rows:
        fid = str(row.get("factor_id") or "")
        entry = manifest.get(fid)
        entry = entry if isinstance(entry, Mapping) else None
        expected = None
        if entry and entry.get("column"):
            expected = expected_column_of(row, owners)
        records.append(
            analyze_factor(
                row,
                now=now,
                first_completed_run_at=first_map.get(fid),
                manifest_entry=entry,
                expected_column=expected,
            )
        )

    columns = sorted(
        {
            str(r["materialization"]["column"])
            for r in records
            if r.get("materialization") and r["materialization"].get("column")
        }
    )
    catalog_rows = await fetch_catalog_rows(columns)
    report = build_report(records, catalog_rows=catalog_rows, generated_at=now)

    out_dir = (
        Path(args.out_dir) if args.out_dir else data_root() / "refs" / "factor_registry"
    )
    md_path, json_path = write_report(report, out_dir)
    summary = report["summary"]
    print(f"因子总数 {summary['total']}；状态 {summary['by_state']}")
    print(
        f"命名违约 {summary['naming_violation_count']}；"
        f"code_fp 失配 {summary['code_drift_count']}（非活跃 {summary['code_drift_inactive_count']}）"
    )
    print(f"报告：{md_path}")
    print(f"      {json_path}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="因子注册中心报告（T-FE-07，只读）")
    parser.add_argument("--market", default=None, help="限市场（如 a_share）")
    parser.add_argument("--limit", type=int, default=None, help="限量（调试用）")
    parser.add_argument(
        "--out-dir", default=None, help="输出目录（默认 <data>/refs/factor_registry/）"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
