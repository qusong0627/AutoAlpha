"""因子概率板：滚动重选与 OOS 回填（P2-f）。

- **月频触发**：每月首个交易日 08:50（beat ``factor-board-reselect``；任务体
  判据 = 最近可用因子分区落在上一自然月，见 :func:`is_month_start`）。以该
  分区为 as_of **重跑选择管线**（同窗 85/65、同 h=5、同闸——细案 §七），
  快照落 ``{board_dir}/rolling/{YYYY-MM}.json``（YYYY-MM = 快照**应用月**）。
- **OOS 回填**：本月的运行把**上月台本**的 ``oos`` 补齐——该快照的应用月刚
  走完，按冻结 sgn/w 合成、后窗逐日对 fwd5 求 RankIC（**前窗选、后窗验**，
  无样本内自洽；对照 build_board._validate 的 in_sample=True）。
- **诚实注记**：回填时应用月最后约 h 日尚无前向收益（qfq 价表止于当时最新
  分区），这些日在 ``excluded_no_fwd`` 显形，不硬凑。
- **口径**：快照为 selection-only——dedup/regime 是每日产板的稳定化变换，
  不进月频证据面（差异见细案 §七）；月份运算全走 stdlib。
- **幂等**：同 as_of 重跑覆盖写（保留已回填 ``oos``）；已有更新的 as_of →
  跳过不倒退。分区迟到导致的重复触发按同 as_of 幂等收敛。
"""

from __future__ import annotations

import json
import logging
from datetime import date as _date
from pathlib import Path

import pandas as pd

from backend.shared.factor_board import board_io as IO
from backend.shared.factor_board import composite as CP
from backend.shared.factor_board import config as C
from backend.shared.factor_board.builder import run_selection
from backend.shared.utc_datetime import to_utc_iso, utc_now

log = logging.getLogger("factor_board.rolling")

LEDGER_SCHEMA = "factor_board_rolling/1.0"


def _iso_dt(dt: str) -> str:
    """'YYYYMMDD' → 'YYYY-MM-DD'。"""
    return f"{dt[:4]}-{dt[4:6]}-{dt[6:]}"


def latest_prior_date(dates_all: list[str], today: _date) -> str | None:
    """最近一个 < today 的因子分区（beat 08:50 时 = 上一交易日）。"""
    today_dt = today.strftime("%Y%m%d")
    prior = [d for d in dates_all if d < today_dt]
    return prior[-1] if prior else None


def is_month_start(dates_all: list[str], today: _date) -> tuple[bool, str | None]:
    """beat 任务判据：最近可用分区在上一自然月 → 今天是应用月的首个交易日。

    假日场景（如 10-08 首个交易日）最近分区仍是 09-30 → 判真；次日 10-08
    分区落地后判假。返回 ``(是否触发, as_of)``。
    """
    last = latest_prior_date(dates_all, today)
    if last is None:
        return False, None
    return last[:6] < today.strftime("%Y%m"), last


def application_month(as_of_dt: str) -> str:
    """应用月 = as_of 的下一个自然月（'YYYY-MM'；stdlib 运算，回避月末怪癖）。"""
    y, m = int(as_of_dt[:4]), int(as_of_dt[4:6])
    y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return f"{y:04d}-{m:02d}"


def _next_month_first_dt(month: str) -> str:
    """'YYYY-MM' → 次月首日紧凑口径（OOS 窗上界，开区间；与分区键同空间）。"""
    y, m = int(month[:4]), int(month[5:7])
    y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return f"{y:04d}{m:02d}01"


def ledger_path(board_dir: Path, month: str) -> Path:
    return Path(board_dir) / C.ROLLING_DIRNAME / f"{month}.json"


def load_ledger(board_dir: Path, month: str) -> dict | None:
    p = ledger_path(board_dir, month)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("读取滚动台账失败 %s: %s", p, exc)
        return None


def write_ledger(board_dir: Path, month: str, payload: dict) -> Path:
    p = ledger_path(board_dir, month)
    p.parent.mkdir(parents=True, exist_ok=True)
    IO._atomic_write(
        p, json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
    )
    return p


def _snapshot_factors(sel: pd.DataFrame) -> list[dict]:
    """选择快照（字段固定：lib/name/group/sgn/w/ic60/win60/n_days）。"""
    out = []
    for _, r in sel.sort_values(
        "ic60", key=lambda s: s.abs(), ascending=False
    ).iterrows():
        out.append(
            {
                "lib": str(r["lib"]),
                "name": str(r["name"]),
                "group": str(r["group"]),
                "sgn": int(r["sgn"]),
                "w": IO._clean(r["w"], 6),
                "ic60": IO._clean(r["ic60"], 6),
                "win60": IO._clean(r["win60"], 6),
                "n_days": int(r["n_days"]),
            }
        )
    return out


def _ic_agg(ics: list[float]) -> dict:
    s = pd.Series(ics, dtype=float).dropna()
    if s.empty:
        return {"rank_ic": None, "icir": None, "t": None, "win": None, "days": 0}
    mu, sd = float(s.mean()), float(s.std())
    return {
        "rank_ic": IO._clean(mu, 6),
        "icir": IO._clean(mu / sd, 6) if sd > 0 else None,
        "t": IO._clean(mu / sd * len(s) ** 0.5, 6) if sd > 0 else None,
        "win": IO._clean(float((s > 0).mean()), 6),
        "days": int(len(s)),
    }


def eval_oos(
    quantdb_root: Path,
    factors: list[dict],
    *,
    since_dt: str,
    until_dt_exclusive: str,
    horizon: int = 5,
) -> tuple[dict | None, list[str]]:
    """上一快照的后窗 OOS：窗口 = 因子分区 ∈ (since, until)，逐日合成↔fwd5 RankIC。

    返回 ``(oos_block | None, warnings)``。合成按快照冻结 sgn/w
    （``composite_day`` 同源）；单日标的 < ``MIN_COMPOSITE_IC_DAY`` 不计。
    边界接受 'YYYYMMDD' 或 ISO，内部归一到分区键空间再比较——曾因 ISO 与
    紧凑混比（位置 4 上 '0'(48) > '-'(45)）致任何紧凑日期都大于 ISO 上界、
    窗恒空（8 月回填落 null 的实证根因）。
    """
    warnings: list[str] = []
    if not factors:
        return None, ["OOS：快照无因子"]
    since_dt = str(since_dt).replace("-", "")
    until_dt_exclusive = str(until_dt_exclusive).replace("-", "")
    dates_all = CP.list_factor_dates(quantdb_root)
    win = [d for d in dates_all if since_dt < d < until_dt_exclusive]
    if not win:
        return None, ["OOS：窗内无因子分区"]
    idx_of = {d: i for i, d in enumerate(dates_all)}
    i0 = idx_of[win[0]]
    sub = dates_all[i0 : idx_of[win[-1]] + 1 + horizon]
    close = CP.load_close_matrix(quantdb_root, sub)
    if close.empty:
        return None, ["OOS：qfq 收盘矩阵为空"]

    sel_df = pd.DataFrame(factors)
    ics: list[float] = []
    excluded = 0
    for d in win:
        fr = CP.fwd_rank(close, idx_of[d] - i0, horizon)
        if fr is None:
            excluded += 1  # 窗尾无前向（回填时价表止于最新分区）——诚实排除
            continue
        frames = CP.read_selected_frames(quantdb_root, sel_df, d)
        comp = CP.composite_day(sel_df, frames).dropna()
        if comp.empty:
            continue
        f = fr.reindex(comp.index)
        m = comp.notna() & f.notna()
        if int(m.sum()) < C.MIN_COMPOSITE_IC_DAY:
            continue
        ics.append(float(comp[m].rank().corr(f[m])))
    block = {
        "since": _iso_dt(since_dt),
        "until": _iso_dt(win[-1]),
        "horizon": horizon,
        **_ic_agg(ics),
        "excluded_no_fwd": excluded,
        "note": (
            f"后窗 {len(win)} 日中 {excluded} 日无前向（回填时价表止于当时最新"
            "分区）已排除；合成按快照冻结 sgn/w（无 dedup/regime）"
        ),
    }
    if not ics:
        warnings.append("OOS：无任何有效 IC 日")
    return block, warnings


def run_rolling(
    quantdb_root: Path,
    board_dir: Path,
    *,
    as_of: str | None = None,
    today: _date | None = None,
) -> dict:
    """跑一次月频重选：写当月台本 + 回填上月 OOS。返回摘要 dict。

    ``as_of``：训练窗尾（'YYYYMMDD' 或 ISO；缺省=最近一个 < today 的分区，
    供 beat 首交易日场景）；显式给出用于历史回填。
    """
    today = today or _date.today()
    warnings: list[str] = []
    dates_all = CP.list_factor_dates(quantdb_root)
    if not dates_all:
        raise RuntimeError("因子库为空（无 dt 分区）")

    if as_of:
        as_of_dt = str(as_of).replace("-", "")
        le = [d for d in dates_all if d <= as_of_dt]
        if not le:
            raise RuntimeError(f"as_of {as_of} 早于最早分区 {dates_all[0]}")
        if le[-1] != as_of_dt:
            warnings.append(
                f"as_of {as_of_dt} 非因子分区日，取 ≤ 它的最近分区 {le[-1]}"
            )
        as_of_dt = le[-1]
    else:
        got = latest_prior_date(dates_all, today)
        if got is None:
            raise RuntimeError("无早于今日的因子分区")
        as_of_dt = got

    month = application_month(as_of_dt)

    # ── 前窗选择快照（与产板同一管线；selection-only） ──────────────
    run = run_selection(quantdb_root, as_of=as_of_dt, today=today)
    warnings += run.warnings
    factors = _snapshot_factors(run.sel)

    # ── 上月台本 OOS 回填（其应用月刚走完） ────────────────────────
    prev_key = f"{as_of_dt[:4]}-{as_of_dt[4:6]}"
    backfilled: str | None = None
    prev = load_ledger(board_dir, prev_key)
    if prev is None:
        warnings.append(f"上月台本 {prev_key} 不存在，跳过 OOS 回填")
    elif not prev.get("factors"):
        warnings.append(f"上月台本 {prev_key} 缺快照，跳过 OOS 回填")
    else:
        prev_asof = str(prev.get("as_of") or "").replace("-", "")
        if len(prev_asof) != 8 or prev_asof >= as_of_dt:
            warnings.append(f"上月台本 {prev_key} 的 as_of 非法或超前，跳过 OOS 回填")
        else:
            oos, ow = eval_oos(
                quantdb_root,
                prev["factors"],
                since_dt=prev_asof,
                until_dt_exclusive=_next_month_first_dt(prev_key),
            )
            warnings += [f"{prev_key}: {w}" for w in ow]
            if oos is None:
                warnings.append(f"上月台本 {prev_key} OOS 计算不出，不写回")
            else:
                prev["oos"] = {
                    **oos,
                    "filled_by_month": month,
                    "filled_at": to_utc_iso(utc_now()),
                }
                write_ledger(board_dir, prev_key, prev)
                backfilled = prev_key

    # ── 当月台本（同 as_of 重跑覆盖；已有更新的 as_of → 不倒退） ────
    existing = load_ledger(board_dir, month)
    existing_asof = str((existing or {}).get("as_of") or "").replace("-", "")
    if existing is not None and existing_asof > as_of_dt:
        warnings.append(
            f"台本 {month} 已有更新的 as_of {existing.get('as_of')}，跳过本次写入"
        )
        return _summary(month, as_of_dt, factors, backfilled, None, warnings, True)

    payload = {
        "schema": LEDGER_SCHEMA,
        "month": month,
        "as_of": _iso_dt(as_of_dt),
        "generated_at": to_utc_iso(utc_now()),
        "pipeline": "selection-only",
        "window": {
            "start": run.dates[0],
            "end": run.trade_date,
            "window_dates": C.WINDOW_DATES,
            "ic_window": C.IC_WINDOW,
            "eval_days": int(len(run.fwd5)),
            "horizon": 5,
        },
        "coverage": {
            "pool": int(len(run.cand)),
            "evaluated": run.gate_info["evaluated"],
            "gated": run.gate_info["gated"],
            "selected": int(len(run.sel)),
            "families": run.gate_info["families"],
            "degraded": bool(run.gate_info["degraded"]),
        },
        "factors": factors,
        # 同 as_of 重跑保留已回填的 oos（证据不丢）
        "oos": (existing or {}).get("oos"),
        "warnings": warnings,
    }
    path = write_ledger(board_dir, month, payload)
    return _summary(month, as_of_dt, factors, backfilled, path, warnings)


def _summary(
    month: str,
    as_of_dt: str,
    factors: list[dict],
    backfilled: str | None,
    path: Path | None,
    warnings: list[str],
    skipped: bool = False,
) -> dict:
    return {
        "month": month,
        "as_of": _iso_dt(as_of_dt),
        "n_factors": len(factors),
        "backfilled_oos": backfilled,
        "ledger": str(path) if path else None,
        "skipped": skipped,
        "warnings": warnings,
    }
