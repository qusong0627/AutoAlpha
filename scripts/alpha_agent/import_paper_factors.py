"""T-FM-01 论文因子落库：报告 JSON → 因子池 ``rd_agent_factors``。

把 FactorMiner 论文 110 因子（T-FM-01 实测报告）按 tier 落进因子池：

- **factor_code = ``fm:`` + FM 原式**——求值走 ``factorminer.runtime`` 的
  白名单求值器（``routers/alpha_agent._detect_factor_kind`` 识别，子进程
  隔离）；与统计派的裸 qlib 表达式同 kind 不同 dialect；
- **身份 = ``statistical.dedup.stable_factor_id``**（candidate_key
  ``paper_fmNNN`` + 固定 task_fingerprint）——同报告复跑幂等：同 fid 恒同
  factor_id，重复导入只会更新名称/代码/指标，不产生新行；
- **metadata 与统计派同形**（source/algorithm/market/universe/horizons/
  fdr_*/fold_*/reselect_* 等），另加 ``qlib_expression``（31 条 cs-free 的
  可携带翻译，审计/移植用；CsRank 类为 null 并附 ``qlib_export_gap``）；
  **刻意不写 ``formulation`` 键**——admin 管线 ``_extract_qlib_expression``
  会把它当 qlib 表达式直喂求值器，FM 原式不是 qlib 语法；
- **结构原创闸接线**（T-FM-06 落点 #7）：每条 FM 原式先过 ``dsl.parse``
  （报告损坏即响亮抛错，带 fid 上下文）——**批内 AST 逐位相同的克隆**
  拒（后到者跳过，记录到 ``build_rows(rejected=...)``），对已注册
  ``fm:`` 语料（代码内 LIBRARY，排除候选自身 key）的结构判决标注进
  ``metadata["structure_screen"]``（只标注不裁决——对库内既有因子的克隆
  属一次性历史导入，走值级闸豁免，见 T-FM-03 细案 §3.3）；
- **整单拒契约（刻意的 all-or-nothing）**：任一表达式解析失败
  （``_parse_expression`` 抛错）⇒ ``build_rows`` 整体中止、一行不落。
  报告是冻结的验证产物（110 条命名集合），解析错误只可能是报告损坏——
  静默跳过坏行会把「半份导入」伪装成成功；坏整单、响亮报错、重跑幂等，
  才是这份一次性历史导入的正确语义（T-FM-03 评审 MEDIUM-2 的取舍记录）；
- **存量刷新 ``--refresh-existing``**（2026-10-12，NW 口径刷新）：同窗口
  重跑（T-FE-04 后 ``rank_ic_t``=Newey-West）产出的新报告，把**既有行**的
  统计口径换新——选择面 = 全部 110 条（**跌出 FDR 的历史行也必须刷新**，
  绝不能把 naive-t 旧值永远留在库里），写出面 = 只命中既有 factor_id：
  库中缺失的行只报告、不落库（不静默新增），也不删除任何行；``tier`` 保留
  首次导入值；FDR 标志翻转的行写入 ``fdr_pass_prev`` 并在摘要列出；
- 默认 **dry-run**（打印逐条身份/翻译状态），``--apply`` 才真写。

用法（宿主或容器；容器里 qlib 数据路径固定 /data）：

    python scripts/alpha_agent/import_paper_factors.py            # dry-run, tier=fdr
    python scripts/alpha_agent/import_paper_factors.py --tier selected
    python scripts/alpha_agent/import_paper_factors.py --apply
    python scripts/alpha_agent/import_paper_factors.py --refresh-existing
    python scripts/alpha_agent/import_paper_factors.py --refresh-existing --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.services.engine.alpha_agent.factorminer import (  # noqa: E402
    qlib_export,
    structure,
)
from backend.services.engine.alpha_agent.factorminer.dsl import (  # noqa: E402
    FM_CODE_PREFIX,
    Node,
)
from backend.services.engine.alpha_agent.factorminer.dsl import (  # noqa: E402
    parse as fm_parse,
)
from backend.services.engine.alpha_agent.factorminer.library import (  # noqa: E402
    LIBRARY,
)
from backend.services.engine.alpha_agent.statistical import dedup as dedup_mod  # noqa: E402
from backend.services.engine.alpha_agent.statistical.serialize import (  # noqa: E402
    json_safe,
)

TASK_ID = "T-FM-01"
#: 固定指纹 ⇒ 同 fid 跨复跑恒同身份（报告重跑后重导只更新、不新增）
TASK_FINGERPRINT = "tfm01-paper-110"
DATA_SOURCE = "factorminer"
LITERATURE = "FactorMiner (arXiv:2602.14670) 附录 D"
DEFAULT_USER_ID = "10000001"
_REPORT_NAME = "T-FM-01_factor_test_csi300_2021-10-09_2025-10-09.json"
#: 报告路径候选：宿主 ROOT/data 是符号链接直达数据盘；容器里 /app/data 是
#: 空目录、真数据卷挂在 /data——按存在性择一（显式覆盖用 --report）。
_HOST_REPORT = ROOT / "data" / "refs" / "factorminer" / _REPORT_NAME
_CONTAINER_REPORT = Path("/data/refs/factorminer") / _REPORT_NAME
TIERS = ("fdr", "selected", "all")


def default_report() -> Path:
    """宿主 → 容器存在性择一；都不在时返回宿主候选（报错即见预期路径）。"""
    for candidate in (_HOST_REPORT, _CONTAINER_REPORT):
        if candidate.is_file():
            return candidate
    return _HOST_REPORT


def load_report(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def select_factors(report: dict, tier: str) -> list[dict]:
    """tier 过滤：``fdr``=过 BH-FDR 的 102 条、``selected``=去重入选 20 条、
    ``all``=全 110。"""
    factors = report["paper"]["factors"]
    if tier == "all":
        return list(factors)
    if tier == "selected":
        kept = set(report["paper"]["dedup"]["kept"])
        return [f for f in factors if f["key"] in kept]
    if tier == "fdr":
        return [f for f in factors if f.get("fdr_horizons")]
    raise ValueError(f"未知 tier：{tier}（可选 {TIERS}）")


def _parse_expression(key: str, expression: str) -> Node:
    """FM 原式 → AST；非法形态带 fid 上下文响亮抛（报告损坏不静默）。"""
    try:
        return fm_parse(expression)
    except ValueError as exc:
        raise ValueError(f"因子 {key} 表达式非法：{exc}") from exc


def _structure_screen(ast: Node, *, key: str) -> dict:
    """候选对已注册 ``fm:`` 语料（代码内 LIBRARY）的结构判决（只读标注）。

    排除候选自身 key——否则任何因子对自己必判 clone。语料 key 用
    ``fm{fid}``，与报告/因子池的 key 形态一致，``best_match`` 可直接回查。
    """
    corpus = {f"fm{f.fid}": f.ast for f in LIBRARY if f"fm{f.fid}" != key}
    result = structure.screen(ast, corpus)
    best = result.best
    return {
        "verdict": result.verdict,
        "best_match": best.key if best else None,
        "trigram_cosine": best.similarity.trigram_cosine if best else None,
        "n_matches": len(result.matches),
        "near_threshold": structure.DEFAULT_NEAR_THRESHOLD,
    }


def _build_metadata(
    factor: dict,
    report: dict,
    *,
    tier: str,
    scheme: str,
    report_path: Path,
    market: str,
    universe: str,
    structure_screen: dict,
) -> dict:
    horizons = {
        str(h["horizon"]): {k: v for k, v in h.items() if k != "horizon"}
        for h in factor["horizons"]
    }
    exported = qlib_export.export(factor["expression"])
    meta = {
        "source": DATA_SOURCE,
        "algorithm": "factorminer",
        "literature": LITERATURE,
        "market": market,
        "universe": universe,
        "task_id": TASK_ID,
        "task_fingerprint": TASK_FINGERPRINT,
        "identity_scheme": scheme,
        "candidate_key": factor["key"],
        "fid": factor["fid"],
        "tier": tier,
        "selected": bool(factor.get("selected")),
        "fm_expression": factor["expression"],
        "qlib_expression": exported.expression,
        "qlib_export_gap": exported.gap,
        "min_history": factor.get("min_history"),
        "complexity": factor.get("complexity"),
        "best_horizon": factor.get("best_horizon"),
        "horizons": horizons,
        "rank_ic_mean": factor.get("rank_ic_mean"),
        "rank_ic_win_rate": factor.get("rank_ic_win_rate"),
        "t_stat": factor.get("t_stat"),
        "p_value": factor.get("p_value"),
        # t 口径自证：T-FE-04 后的报告逐 horizon 带 rank_ic_t_naive /
        # rank_ic_t_lags 审计列 ⇒ 主 t = Newey-West；旧报告无从分辨，标 naive
        # （--refresh-existing 换新报告后此键随之转「newey_west」）。
        "t_correction": (
            "newey_west"
            if any(
                "rank_ic_t_naive" in h
                for h in (factor.get("horizons") or [])
                if isinstance(h, dict)
            )
            else "naive"
        ),
        "icir": factor.get("icir"),
        "rank_icir": factor.get("icir"),
        "fdr_pass": bool(factor.get("fdr_horizons")),
        "fdr_horizons": factor.get("fdr_horizons", []),
        "fdr_q": report.get("fdr_q"),
        "fold_ics": factor.get("fold_ics"),
        "fold_sign_consistency": factor.get("fold_sign_consistency"),
        "stability_eligible": factor.get("stability_eligible"),
        "stability_note": factor.get("stability_note"),
        "selection_score": factor.get("selection_score"),
        "reselect_rate": factor.get("reselect_rate"),
        "reselect_hits": factor.get("reselect_hits"),
        "reselect_checkpoints": factor.get("reselect_checkpoints"),
        "max_abs_corr": factor.get("max_abs_corr"),
        "corr_warn": factor.get("corr_warn"),
        "max_abs_corr_vs_house": factor.get("max_abs_corr_vs_house"),
        "closest_house": factor.get("closest_house"),
        "reject_reason": factor.get("reject_reason"),
        "structure_screen": structure_screen,
        "window": report.get("window"),
        "returns_convention": report.get("returns_convention"),
        "neutralize_mode": report.get("neutralize"),
        "tradability_filter": (report.get("tradability") or {}).get("filter"),
        "vwap_convention": report.get("vwap_convention"),
        "report": str(report_path),
    }
    return json_safe(meta)


def build_rows(
    report: dict,
    *,
    tier: str,
    report_path: Path,
    user_id: str = DEFAULT_USER_ID,
    market: str = "a_share",
    universe: str = "csi300",
    rejected: list[dict] | None = None,
) -> list[dict]:
    """报告 + tier → 待落库行（纯函数：不触库，dry-run 与 --apply 共用）。

    结构原创闸（T-FM-06 落点 #7）：**批内** AST 逐位相同的克隆拒——同一
    报告出现同式因子必是资产事故，后到者跳过（传入 ``rejected`` 列表时
    追加记录）。对已注册语料的判决只进 metadata 不裁决。
    """
    rows: list[dict] = []
    accepted: list[tuple[str, Node]] = []
    for factor in select_factors(report, tier):
        key = factor["key"]
        fm_expr = factor["expression"]
        ast = _parse_expression(key, fm_expr)
        clone_of = next((k for k, a in accepted if structure.ast_equal(ast, a)), None)
        if clone_of is not None:
            if rejected is not None:
                rejected.append(
                    {
                        "key": key,
                        "fid": factor["fid"],
                        "reason": structure.VERDICT_CLONE,
                        "clone_of": clone_of,
                    }
                )
            continue
        accepted.append((key, ast))
        factor_id, scheme = dedup_mod.stable_factor_id(
            user_id=user_id,
            market=market,
            universe=universe,
            candidate_key=f"paper_{key}",
            task_fingerprint=TASK_FINGERPRINT,
            task_id=TASK_ID,
        )
        meta = _build_metadata(
            factor,
            report,
            tier=tier,
            scheme=scheme,
            report_path=report_path,
            market=market,
            universe=universe,
            structure_screen=_structure_screen(ast, key=key),
        )
        horizons = meta["horizons"]
        h1 = horizons.get("1") or next(iter(horizons.values()), {})
        window = report.get("window") or {}
        rows.append(
            {
                "factor_id": factor_id,
                "scheme": scheme,
                "key": key,
                "factor_name": f"FactorMiner 论文因子（{key}）",
                "factor_code": FM_CODE_PREFIX + fm_expr,
                "factor_formulation": fm_expr,
                "metadata": meta,
                "qlib_expression": meta["qlib_expression"],
                "qlib_gap": meta["qlib_export_gap"],
                "ic_value": h1.get("ic_mean"),
                "rank_ic": factor.get("rank_ic_mean"),
                "icir": factor.get("icir"),
                "date_range": f"{window.get('start', '?')}~{window.get('end', '?')}",
                "user_id": user_id,
                "market": market,
                "universe": universe,
            }
        )
    return rows


async def apply_rows(rows: list[dict], *, persistence) -> dict:
    """真写：每行 save_factor（新行 status=pending）+ update_factor_metrics
    （status=completed 带指标）。失败向上抛，由调用方记账。"""
    await persistence.ensure_tables()
    for row in rows:
        await persistence.save_factor(
            factor_id=row["factor_id"],
            factor_name=row["factor_name"],
            factor_code=row["factor_code"],
            user_id=row["user_id"],
            metadata=row["metadata"],
            market=row["market"],
            universe=row["universe"],
            factor_formulation=row["factor_formulation"],
            data_source=DATA_SOURCE,
        )
        await persistence.update_factor_metrics(
            factor_id=row["factor_id"],
            status="completed",
            ic_value=row["ic_value"],
            rank_ic=row["rank_ic"],
            icir=row["icir"],
            rank_icir=row["icir"],
            universe=row["universe"],
            date_range=row["date_range"],
            metadata=row["metadata"],
        )
    return {"saved": len(rows)}


def _fmt_ic(value) -> str:
    if isinstance(value, (int, float)):
        return f"{value:+.4f}"
    return "  —  "


def _default_persistence():
    """真写库的 persistence（延迟导入：qlib_app.services 深链会拉 qlib，
    dry-run 与 venv 测试都不该为此买单）。"""
    from backend.services.engine.qlib_app.services.rd_agent_persistence import (  # noqa: PLC0415
        RDAgentFactorPersistence,
    )

    return RDAgentFactorPersistence()


async def _load_existing_rows(
    persistence,
    *,
    user_id: str,
    market: str,
    universe: str,
) -> dict[str, dict]:
    """本任务既有行（factor_id → 行；metadata 已解码）。"""
    rows = await persistence.list_factors(
        user_id=user_id,
        market=market,
        universe=universe,
        task_id=TASK_ID,
        limit=1000,
    )
    return {r["factor_id"]: r for r in rows}


def _run_refresh(args, report: dict, report_path: Path) -> int:
    """``--refresh-existing`` 同步入口：单事件循环完成整个刷新。"""
    return asyncio.run(_refresh_flow(args, report, report_path))


async def _refresh_flow(args, report: dict, report_path: Path) -> int:
    """NW 口径刷新既有行（不新增、不删除、记翻转）。

    选择面 = 全部 110 条——跌出 FDR 的历史行也必须换到新口径，否则库里
    永久残留 naive-t 旧值（这正是本次刷新的起因）。写出面只命中既有
    factor_id：库中缺失的行只报告、不落库（这是刷新，不是导入），也不
    删除任何行。``tier`` 保留首次导入值；FDR / 入选标志翻转的行分别写
    ``fdr_pass_prev`` 并进摘要。读库失败即放弃（读不到既有行时写库无意义）。

    **单循环约束**：读既有行与写回必须同一次 ``asyncio.run``——分两次会
    让 asyncpg 连接池跨事件循环（2026-10-12 真跑实测：写回时
    ``Future attached to a different loop``）。
    """
    persistence = _default_persistence()
    try:
        existing = await _load_existing_rows(
            persistence,
            user_id=args.user_id,
            market=args.market,
            universe=args.universe,
        )
    except Exception as exc:  # noqa: BLE001 —— 读不到既有行就绝不能写
        print(f"读取既有行失败，放弃刷新：{exc}")
        return 1
    rejected: list[dict] = []
    plan: list[dict] = []
    missing: list[str] = []
    fdr_flips: list[tuple[str, bool, bool]] = []
    sel_flips: list[tuple[str, bool, bool]] = []
    for row in build_rows(
        report,
        tier="all",
        report_path=report_path,
        user_id=args.user_id,
        market=args.market,
        universe=args.universe,
        rejected=rejected,
    ):
        old = existing.get(row["factor_id"])
        if old is None:
            missing.append(row["key"])
            continue
        meta = row["metadata"]
        old_meta = old.get("metadata") or {}
        meta["tier"] = old_meta.get("tier") or args.tier  # 保留首次导入语义
        prev_fdr = old_meta.get("fdr_pass")
        if prev_fdr is not None and bool(prev_fdr) != bool(meta.get("fdr_pass")):
            meta["fdr_pass_prev"] = bool(prev_fdr)
            fdr_flips.append((row["key"], bool(prev_fdr), bool(meta.get("fdr_pass"))))
        prev_sel = old_meta.get("selected")
        if prev_sel is not None and bool(prev_sel) != bool(meta.get("selected")):
            sel_flips.append((row["key"], bool(prev_sel), bool(meta.get("selected"))))
        plan.append(row)
    flip_keys = {k for k, _, _ in fdr_flips} | {k for k, _, _ in sel_flips}
    for row in plan:
        marker = " ★" if row["key"] in flip_keys else ""
        print(
            f"{row['key']:>6}  id={row['factor_id'][:12]}  "
            f"rank_ic={_fmt_ic(row['rank_ic'])}  "
            f"fdr_pass={row['metadata'].get('fdr_pass')}{marker}"
        )
    for key in missing:
        print(f"{key:>6}  跳过：库中无此身份行（刷新只报告，不落库）")
    for key, prev, new in fdr_flips:
        print(f"{key:>6}  ★ FDR 翻转：{prev} → {new}")
    for key, prev, new in sel_flips:
        print(f"{key:>6}  ★ 入选翻转：{prev} → {new}")
    print(
        f"共 {len(plan)} 条待刷新（既有任务行 {len(existing)}；"
        f"缺失跳过 {len(missing)}；FDR 翻转 {len(fdr_flips)}；"
        f"入选翻转 {len(sel_flips)}）；"
        + ("--apply 已给，开始写库" if args.apply else "dry-run（--apply 才写库）")
    )
    if not args.apply:
        return 0

    summary = await apply_rows(plan, persistence=persistence)
    print(f"刷新落库完成：{summary}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="T-FM-01 论文因子落库（默认 dry-run，--apply 才写库）"
    )
    parser.add_argument("--report", default=str(default_report()))
    parser.add_argument("--tier", choices=TIERS, default="fdr")
    parser.add_argument(
        "--apply", action="store_true", help="真写 rd_agent_factors（默认只打印）"
    )
    parser.add_argument(
        "--refresh-existing",
        action="store_true",
        help="NW 口径刷新既有行：选择面=全 110，只更新既有 factor_id"
        "（库中缺失只报告不落库、不删行；tier 保留首次导入值；默认 dry-run）",
    )
    parser.add_argument("--user-id", default=DEFAULT_USER_ID)
    parser.add_argument("--market", default="a_share")
    parser.add_argument("--universe", default="csi300")
    args = parser.parse_args(argv)

    report_path = Path(args.report)
    report = load_report(report_path)
    if args.refresh_existing:
        return _run_refresh(args, report, report_path)
    rejected: list[dict] = []
    rows = build_rows(
        report,
        tier=args.tier,
        report_path=report_path,
        user_id=args.user_id,
        market=args.market,
        universe=args.universe,
        rejected=rejected,
    )
    n_qlib = sum(1 for r in rows if r["qlib_expression"])
    for row in rows:
        state = "qlib✓" if row["qlib_expression"] else "FM-only"
        print(
            f"{row['key']:>6}  id={row['factor_id'][:12]}  {state:>7}  "
            f"rank_ic={_fmt_ic(row['rank_ic'])}  {row['factor_name']}"
        )
    for rec in rejected:
        print(f"{rec['key']:>6}  拒：与 {rec['clone_of']} AST 逐位相同（批内克隆）")
    clone_note = f"批内克隆拒 {len(rejected)} 条；" if rejected else ""
    print(
        f"共 {len(rows)} 条（tier={args.tier}；{clone_note}可翻译 qlib {n_qlib} / "
        f"FM-only {len(rows) - n_qlib}）；"
        + ("--apply 已给，开始写库" if args.apply else "dry-run（--apply 才写库）")
    )
    if not args.apply:
        return 0

    summary = asyncio.run(apply_rows(rows, persistence=_default_persistence()))
    print(f"落库完成：{summary}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
