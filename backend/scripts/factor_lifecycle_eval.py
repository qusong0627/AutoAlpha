#!/usr/bin/env python3
"""因子生命周期月度评估器（T-FE-06）：滚动 12m ICIR 判定律 + 降权执行。

来源：``docs/因子挖掘_T-FE-06_07_生命周期与六维标签_细案_20261011.md``
（§三 D7/D8/D9、§四 #5）。口径全部收在 ``shared/factor_lifecycle``
（判定律/状态机/台账 IO 唯一实现），本脚本只做 IO 编排：

- 读数源 = 每因子**家市场优先、否则最新**的 run cell（``store.latest_cells``
  ＋ ``store.get_series``）；cell 非 completed/无 series → 「不可算」桶
  （诚实降级，不冒充差读数）。
- 回溯自愈：每次对最新序列重算最近 K 个月末（默认 4）读数——首跑即能对
  存量弱因子给出连击判定，缺跑月份自动补齐；同序列重算幂等。
- 盲窗内连击达标 → ``observing_protected``（记录不动作，细案 D7）。
- ``--apply``：写 ``metadata_json.lifecycle``（含 last_eval 快照；迁移时
  置显式 ``state=downweighted`` + 证据）＋ 台账读数/事件；**发布留人工**：
  训练目录的禁用/发布走目录管理界面（细案 D8 交付口径：报告给出精确列
  清单，评估器不自动起草 catalog 草稿）。
- 无任何 flag = dry-run：零副作用，仅 stdout 报告。

用法:
    python backend/scripts/factor_lifecycle_eval.py                     # dry-run 全量
    python backend/scripts/factor_lifecycle_eval.py --market a_share --limit 50
    python backend/scripts/factor_lifecycle_eval.py --factor-id xxx     # 单因子（抽核）
    python backend/scripts/factor_lifecycle_eval.py --apply             # 写状态+台账+冻结报告
    python backend/scripts/factor_lifecycle_eval.py --apply --notify    # 另推 QQ 摘要（默认关）

产物（仅 --apply）：台账 ``<data>/factor_lifecycle/lifecycle_ledger.json``；
冻结报告 ``<data>/refs/factor_lifecycle/eval_YYYYMM.{md,json}``。
不改 celery；调度（cron/beat）是部署级决策，由用户拍板。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import sys
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backend.shared.factor_lifecycle import (  # noqa: E402
    DECISION_DOWNWEIGHT,
    STATE_DOWNWEIGHTED,
    MonthReading,
    append_event,
    blind_until_of,
    has_completed_backtest,
    load_ledger,
    month_ends_back,
    read_month,
    resolve_state,
    save_ledger,
    upsert_readings,
    verdict_of,
)
from backend.shared.factor_lifecycle_report import (  # noqa: E402
    build_plan,
    fmt_icir,
    render_markdown,
)
from backend.shared.factor_tags import read_metadata  # noqa: E402
from backend.shared.utc_datetime import as_utc, to_utc_iso  # noqa: E402

logger = logging.getLogger("factor_lifecycle_eval")

REPORT_SCHEMA_VERSION = 1

#: 降权原因码（写 metadata.lifecycle.reason 与台账事件；机器可读，勿改字面量）。
DOWNWEIGHT_REASON = "rolling_12m_icir_below_floor_3_months"

#: 默认回溯月末数（细案 D7：首跑即给出连击判定的自愈窗）。
DEFAULT_BACKFILL_MONTHS = 4

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


# ------------------------------------------------------------------ 纯函数


def pick_cell(
    cells: Sequence[Mapping[str, Any]], *, market: str | None
) -> Mapping[str, Any] | None:
    """每因子多市场 cell 择一：家市场优先；无匹配取最新（并列按 run_id 降序）。

    家市场缺测时诚实降级为其他市场证据（记录里带 cell.market 可溯）。
    """
    if not cells:
        return None
    home = [c for c in cells if str(c.get("market") or "") == str(market or "")]
    pool = home or list(cells)

    def _key(c: Mapping[str, Any]) -> tuple[datetime, str]:
        created = c.get("created_at")
        return (
            as_utc(created) if created is not None else _EPOCH,
            str(c.get("run_id") or ""),
        )

    return max(pool, key=_key)


def series_payload_of(
    fetched: Mapping[str, Any] | None,
) -> Mapping[str, Any] | None:
    """``store.get_series`` 行 → 曲线载荷（载荷挂在行内 ``series`` 键下）。"""
    if not isinstance(fetched, Mapping):
        return None
    payload = fetched.get("series")
    return payload if isinstance(payload, Mapping) else None


def evaluate_factor(
    row: Mapping[str, Any],
    *,
    cell: Mapping[str, Any] | None,
    series: Mapping[str, Any] | None,
    first_completed_run_at: datetime | None,
    asof: date,
    now: datetime,
    backfill_months: int = DEFAULT_BACKFILL_MONTHS,
) -> dict[str, Any]:
    """因子行 + cell + 序列 → 评估记录（纯函数；决策不写任何东西）。"""
    fid = str(row.get("factor_id") or "")
    meta = read_metadata(row)
    life = meta.get("lifecycle")
    life = life if isinstance(life, Mapping) else {}

    blind_until = blind_until_of(
        created_at=row.get("created_at"),
        first_completed_run_at=first_completed_run_at,
    )
    state_before = resolve_state(
        explicit_state=life.get("state"),
        has_completed_run=has_completed_backtest(
            row, first_completed_run_at=first_completed_run_at
        ),
        blind_until=blind_until,
        now=now,
    )

    readings: list[MonthReading] = []
    if cell is None:
        eval_status = "no_run"
    elif str(cell.get("status") or "") != "completed":
        eval_status = "no_completed_series"
    elif not series:
        eval_status = "no_series_payload"
    else:
        readings = [
            read_month(series, month_end=m)
            for m in month_ends_back(asof, backfill_months)
        ]
        eval_status = readings[-1].status if readings else "no_series_payload"

    verdict = verdict_of(readings, blind_until=blind_until)
    if verdict["decision"] != DECISION_DOWNWEIGHT:
        action = "none"
    elif state_before == STATE_DOWNWEIGHTED:
        action = "already"
    else:
        action = "downweight"
    state_after = STATE_DOWNWEIGHTED if action == "downweight" else state_before

    cell_view = None
    if cell is not None:
        cell_view = {
            "run_id": cell.get("run_id"),
            "market": cell.get("market"),
            "status": cell.get("status"),
            "created_at": to_utc_iso(cell.get("created_at"))
            if cell.get("created_at") is not None
            else None,
        }
    return {
        "factor_id": fid,
        "factor_name": str(row.get("factor_name") or ""),
        "market": str(row.get("market") or ""),
        "state_before": state_before,
        "state_after": state_after,
        "blind_until": to_utc_iso(blind_until) if blind_until is not None else None,
        "eval_status": eval_status,
        "cell": cell_view,
        "readings": [r.as_dict() for r in readings],
        "verdict": verdict,
        "action": action,
        "applied": False,
        "materialized_column": None,  # main 里经 manifest 补齐（人工执行提示）
    }


def lifecycle_write_payload(
    record: Mapping[str, Any],
    *,
    existing_lifecycle: Mapping[str, Any] | None,
    now: datetime,
) -> dict[str, Any]:
    """评估记录 → 新的 ``metadata_json.lifecycle``（完整对象，纯函数）。

    走 ``update_factor_metrics`` 的顶层浅合并，所以必须携带全量 lifecycle
    （含既有键）；``no_run`` 记录不改任何东西（返回既有对象内容）。
    """
    life = dict(existing_lifecycle or {})
    verdict = record.get("verdict") or {}
    latest = verdict.get("latest") or {}
    if str(record.get("eval_status") or "") != "no_run":
        life["last_eval"] = {
            "month_end": latest.get("month_end"),
            "n_days": latest.get("n_days"),
            "icir": latest.get("icir"),
            "status": str(record.get("eval_status") or ""),
            "streak": int(verdict.get("streak") or 0),
            "decision": verdict.get("decision"),
            "evaluated_at": to_utc_iso(now),
        }
    if record.get("action") == "downweight":
        life.update(
            {
                "state": STATE_DOWNWEIGHTED,
                "since": to_utc_iso(now),
                "reason": DOWNWEIGHT_REASON,
                "evidence": {
                    "month_end": latest.get("month_end"),
                    "n_days": latest.get("n_days"),
                    "icir": latest.get("icir"),
                    "streak": int(verdict.get("streak") or 0),
                },
                "blind_until": record.get("blind_until"),
            }
        )
    return life


def materially_same_lifecycle(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """除 ``last_eval.evaluated_at`` 外全等 → 实质相同（--apply 幂等判据）。

    评估戳变化（每次运行的 evaluated_at）不算实质变更——安静月份
    ``--apply`` 零写入；读数/状态/证据任一变化才落 PG。
    """

    def _strip(obj: Mapping[str, Any]) -> dict[str, Any]:
        out = dict(obj)
        le = out.get("last_eval")
        if isinstance(le, Mapping):
            out["last_eval"] = {k: v for k, v in le.items() if k != "evaluated_at"}
        return out

    return _strip(a) == _strip(b)


# ------------------------------------------------------- 拥挤度（§7.1 顺带记录）


def _parse_day(raw: Any) -> date | None:
    """artifact 日期（``YYYYMMDD`` / ISO）→ date；不可解析返回 None。"""
    s = str(raw).strip()
    if not s:
        return None
    try:
        if len(s) >= 10 and s[4] == "-":
            return date.fromisoformat(s[:10])
        return datetime.strptime(s[:8], "%Y%m%d").date()
    except (ValueError, TypeError):
        return None


def _finite_or_nan(raw: Any) -> float:
    """任意读数 → float；None/垃圾/非有限一律 NaN（下游自带 NaN 处理）。"""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return float("nan")
    return value if math.isfinite(value) else float("nan")


def crowding_from_series(
    dates: Sequence[Any],
    ics: Sequence[Any],
    turnovers: Sequence[Any],
    *,
    asof: date | None = None,
) -> dict[str, Any]:
    """逐日 ic/turnover → 既有 ``crowding_score``（纯函数；记录不裁决）。

    细案 §七.1：V1 顺带记录既有拥挤度代理（换手分位 + IC lag-1 自相关，
    ``factor_report/metrics_eval.py`` 单一实现——不在此重写公式）。只取
    ``date ≤ asof`` 的行；None/垃圾/非有限读数归 NaN 后交下游（``_finite``
    与 ``ic_autocorr`` 自带 NaN 处理）。输出做 JSON 安全化——冻结报告绝不
    带 NaN（非有限 → None，「无数据」不是「0 分」）。
    """
    from backend.services.engine.factor_report.metrics_eval import crowding_score

    ic_ok: list[float] = []
    t_ok: list[float] = []
    for raw_d, raw_ic, raw_t in zip(dates, ics, turnovers, strict=False):
        day = _parse_day(raw_d)
        if day is None or (asof is not None and day > asof):
            continue
        ic_ok.append(_finite_or_nan(raw_ic))
        t_ok.append(_finite_or_nan(raw_t))
    result = crowding_score(ic_ok, t_ok)
    return {
        key: (value if not isinstance(value, float) or math.isfinite(value) else None)
        for key, value in result.items()
    }


def crowding_for_records(
    records: Sequence[Mapping[str, Any]],
    crowding_by_column: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """记录行 → 附 ``crowding``（按物化列查表；未物化/未覆盖 → None）。

    不可变：返回新 dict 列表，原记录不被改动。
    """
    out: list[dict[str, Any]] = []
    for record in records:
        item = dict(record)
        column = str(item.get("materialized_column") or "")
        entry = crowding_by_column.get(column) if column else None
        item["crowding"] = dict(entry) if isinstance(entry, Mapping) else None
        out.append(item)
    return out


# ------------------------------------------------------------------ IO 面


def _data_root() -> Path:
    from backend.scripts.factor_registry_report import data_root

    return data_root()


def _factor_series_path() -> Path:
    """rd_mined 明细序列产物路径（factor_report 单一来源，不在本脚本拼路径）。"""
    from backend.services.engine.factor_report.service import series_path

    return series_path("rd_mined")


#: crowding 所需的四列（rd_mined factor_series.parquet 逐日×逐因子明细）。
_CROWDING_COLUMNS = ("factor", "date", "ic", "turnover")


def load_crowding_by_column(
    columns: Sequence[str], *, asof: date | None = None
) -> dict[str, dict[str, Any]]:
    """rd_mined ``factor_series.parquet`` → 每物化列 crowding（读不到 → {}）。

    细案 §七.1 探测结论（2026-10-11）：产物存在且含逐日 ic/turnover（102 列，
    2020-01→最新切日）→ V1 顺带记录。任何读取失败只告警降级——记录面
    「无数据」，不硬凑（报告里 crowding 全 None）。
    """
    wanted = {str(c) for c in columns if c}
    if not wanted:
        return {}
    path = _factor_series_path()
    if not path.exists():
        return {}
    try:
        import pyarrow.parquet as pq

        table = pq.read_table(path, columns=list(_CROWDING_COLUMNS)).to_pandas()
        table = table[table["factor"].isin(wanted)]
        out: dict[str, dict[str, Any]] = {}
        for column, group in table.groupby("factor", sort=False, observed=True):
            out[str(column)] = crowding_from_series(
                group["date"].tolist(),
                group["ic"].tolist(),
                group["turnover"].tolist(),
                asof=asof,
            )
        return out
    except Exception:  # noqa: BLE001 — 顺带记录面，读/算都降级不硬凑（评审 LOW-4）
        logger.warning(
            "crowding 读取/计算 factor_series 失败（报告记无数据）", exc_info=True
        )
        return {}


async def fetch_factor_rows(
    *, market: str | None = None, factor_id: str | None = None, limit: int | None = None
) -> list[dict[str, Any]]:
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    clauses: list[str] = []
    params: dict[str, Any] = {}
    if factor_id:
        clauses.append("factor_id = :factor_id")
        params["factor_id"] = factor_id
    if market:
        clauses.append("market = :market")
        params["market"] = market
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    limit_clause = "LIMIT :limit" if limit else ""
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


def report_paths_for(out_dir: Path, asof: date) -> tuple[Path, Path]:
    """冻结报告路径（写前可算，供 report 内自引）。"""
    stamp = asof.strftime("%Y%m")
    return out_dir / f"eval_{stamp}.md", out_dir / f"eval_{stamp}.json"


def write_report(report: Mapping[str, Any], md_path: Path, json_path: Path) -> None:
    """落盘 md + json（原子替换）。"""
    md_path.parent.mkdir(parents=True, exist_ok=True)
    for path, text in (
        (md_path, render_markdown(report)),
        (json_path, json.dumps(report, ensure_ascii=False, indent=2)),
    ):
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)


async def main_async(args: argparse.Namespace) -> int:
    from backend.services.engine.factor_backtest import store as fb_store
    from backend.services.engine.qlib_app.services.rd_agent_persistence import (
        RDAgentFactorPersistence,
    )

    from backend.scripts.factor_registry_report import load_manifest, manifest_owners

    now = datetime.now(timezone.utc)
    asof = date.fromisoformat(args.asof) if args.asof else now.date()

    rows = await fetch_factor_rows(
        market=args.market, factor_id=args.factor_id, limit=args.limit
    )
    factor_ids = [str(r.get("factor_id") or "") for r in rows if r.get("factor_id")]
    first_map = await fb_store.first_completed_at(factor_ids)
    cells = await fb_store.latest_cells(factor_ids)

    cells_by_factor: dict[str, list[dict[str, Any]]] = {}
    for cell in cells:
        cells_by_factor.setdefault(str(cell.get("factor_id") or ""), []).append(cell)

    manifest = load_manifest()

    records: list[dict[str, Any]] = []
    for row in rows:
        fid = str(row.get("factor_id") or "")
        cell = pick_cell(
            cells_by_factor.get(fid, []), market=str(row.get("market") or "")
        )
        series = None
        if cell is not None and str(cell.get("status") or "") == "completed":
            series = series_payload_of(
                await fb_store.get_series(str(cell.get("run_id") or ""))
            )
        record = evaluate_factor(
            row,
            cell=cell,
            series=series,
            first_completed_run_at=first_map.get(fid),
            asof=asof,
            now=now,
            backfill_months=args.backfill_months,
        )
        entry = manifest.get(fid)
        if isinstance(entry, Mapping) and entry.get("column"):
            record["materialized_column"] = str(entry["column"])
        records.append(record)

    # 拥挤度顺带记录（细案 §七.1）：rd_mined factor_series 可直接读取 → 逐物化
    # 列算既有 crowding_score。只记录不裁决（不进判定律）；读不到不硬凑。
    crowding_map = load_crowding_by_column(
        [str(r.get("materialized_column") or "") for r in records],
        asof=asof,
    )
    records = crowding_for_records(records, crowding_map)

    plan = build_plan(records)

    ledger_path = (
        Path(args.ledger)
        if args.ledger
        else _data_root() / "factor_lifecycle" / "lifecycle_ledger.json"
    )
    report_paths: tuple[Path, Path] | None = None
    applied_count = 0
    transitions: list[dict[str, Any]] = []

    if args.apply:
        ledger = load_ledger(ledger_path)
        persistence = RDAgentFactorPersistence()
        row_by_id = {str(r.get("factor_id") or ""): r for r in rows}
        for record in records:
            fid = record["factor_id"]
            if record["eval_status"] == "no_run" and record["action"] != "downweight":
                continue
            meta = read_metadata(row_by_id.get(fid) or {})
            existing = meta.get("lifecycle")
            existing = dict(existing) if isinstance(existing, Mapping) else {}
            payload = lifecycle_write_payload(
                record, existing_lifecycle=existing, now=now
            )
            if materially_same_lifecycle(payload, existing):
                continue  # 幂等：无实质变化不写（评估戳变化不算）
            await persistence.update_factor_metrics(
                fid, metadata={"lifecycle": payload}
            )
            record["applied"] = True
            applied_count += 1
            if record["action"] == "downweight":
                transitions.append(record)
                ledger = append_event(
                    ledger,
                    fid,
                    from_state=record["state_before"],
                    to_state=STATE_DOWNWEIGHTED,
                    reason=DOWNWEIGHT_REASON,
                    evidence=(payload.get("evidence") or None),
                    now=now,
                )
            cell = record.get("cell") or {}
            if record["readings"] and cell.get("run_id"):
                ledger = upsert_readings(
                    ledger,
                    fid,
                    record["readings"],
                    run_id=str(cell.get("run_id") or ""),
                    now=now,
                )
        save_ledger(ledger_path, ledger)

        out_dir = (
            Path(args.out_dir)
            if args.out_dir
            else _data_root() / "refs" / "factor_lifecycle"
        )
        md_path, json_path = report_paths_for(out_dir, asof)
        report: dict[str, Any] = {
            "schema_version": REPORT_SCHEMA_VERSION,
            "generated_at": to_utc_iso(now),
            "asof": asof.isoformat(),
            "mode": "apply",
            "params": {
                "market": args.market,
                "limit": args.limit,
                "factor_id": args.factor_id,
                "backfill_months": args.backfill_months,
            },
            "summary": {
                "total": len(records),
                "applied_count": applied_count,
                "ledger_path": str(ledger_path),
                "report_md": str(md_path),
                "report_json": str(json_path),
            },
            "records": records,
        }
        write_report(report, md_path, json_path)
        report_paths = (md_path, json_path)

        if args.notify and transitions:
            _notify_transitions(transitions)

    print(
        f"模式：{'apply' if args.apply else 'dry-run'} ；asof {asof} ；因子 {len(records)}"
    )
    print(f"读数状态：{plan['by_eval_status']}")
    print(
        f"降权 {len(plan['downweight_ids'])}（已降权维持 {len(plan['already_ids'])}）；"
        f"盲窗保护 {len(plan['protected_ids'])}；早警 {len(plan['weak_watch_ids'])}"
    )
    if args.apply:
        print(f"实际写入 {applied_count}；台账 {ledger_path}")
        if report_paths:
            print(f"冻结报告 {report_paths[0]}")
    pending = [r for r in records if r["action"] in ("downweight", "already")]
    if pending:
        print(
            "待人工：目录管理禁用下列物化列（发布留人工）："
            if args.apply
            else "预览：若 --apply 将降权下列因子（对应物化列需人工在目录管理禁用）："
        )
        for r in pending:
            print(
                f"  - {r['factor_id'][:12]} → {r['materialized_column'] or '（未物化）'}"
            )
    return 0


def _notify_transitions(transitions: Sequence[Mapping[str, Any]]) -> None:
    """降权迁移 → QQ 摘要（显式 --notify 才走；窗口即时冲刷）。永不因告警失败。"""
    try:
        from backend.shared import qq_digest

        for r in transitions:
            latest = (r.get("verdict") or {}).get("latest") or {}
            qq_digest.enqueue(
                digest_key="factor_lifecycle",
                header="因子生命周期降权",
                line=(
                    f"{r.get('factor_name')}（{str(r.get('factor_id') or '')[:8]}）"
                    f"滚动12m ICIR={fmt_icir(latest.get('icir'))}"
                    f"（连击{(r.get('verdict') or {}).get('streak')}月末）→ 已降权退役；"
                    f"训练供给处置见目录管理（列 {r.get('materialized_column') or '未物化'}）"
                ),
                footer="详情见因子生命周期评估报告。",
            )
        qq_digest.flush_all()
    except Exception as exc:  # noqa: BLE001 - 告警失败不影响评估结果
        logger.warning("QQ 摘要发送失败：%s", exc)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="因子生命周期月度评估器（T-FE-06；无 flag = dry-run 零副作用）"
    )
    parser.add_argument(
        "--asof", default=None, help="评估参考日 YYYY-MM-DD（默认今日）"
    )
    parser.add_argument("--market", default=None, help="限市场（如 a_share）")
    parser.add_argument("--factor-id", default=None, help="单因子（调试/抽核）")
    parser.add_argument("--limit", type=int, default=None, help="限量（调试用）")
    parser.add_argument(
        "--backfill-months",
        type=int,
        default=DEFAULT_BACKFILL_MONTHS,
        help=f"回溯月末数（默认 {DEFAULT_BACKFILL_MONTHS}）",
    )
    parser.add_argument("--apply", action="store_true", help="写状态/台账/冻结报告")
    parser.add_argument(
        "--notify", action="store_true", help="降权迁移时推 QQ 摘要（默认关）"
    )
    parser.add_argument(
        "--ledger", default=None, help="台账路径（默认 <data>/factor_lifecycle/）"
    )
    parser.add_argument(
        "--out-dir",
        default=None,
        help="冻结报告目录（默认 <data>/refs/factor_lifecycle/）",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(levelname)s %(name)s: %(message)s"
    )
    if args.notify and not args.apply:
        print("--notify 只在 --apply 时生效（dry-run 零副作用）", file=sys.stderr)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
