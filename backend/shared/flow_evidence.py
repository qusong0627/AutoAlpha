"""资金流延续证据产物 — 路径与契约的唯一出处（资金流全景 v2）。

产物目录（``QM_FLOW_EVIDENCE_DIR`` > 容器 ``/data/market-analysis/flow_evidence``
> 仓库 ``data/market-analysis/flow_evidence``）：

- ``latest.json``  当日逐板块 streak + 命中榜（跨日字段的唯一出处）
- ``evidence.json`` 信号档案：过闸条件统计（可执行 T+1 收盘建仓口径 +
  探针对照 t→t+3 口径）与「已实测不具预测力」否决清单
- ``panel_sector.parquet`` 逐日×板块面板
  （dt/sector/net_main/net_ratio/breadth/mom3/streak/members）

纪律（细案 v2 §3/§8）：
- 跨日字段（streak / 证据徽章）一律来自本产物；缺失或 ``trade_date`` 落后于
  行情最新分区时，调用方必须落 null 并在 meta 中披露原因——陈旧 streak 比没有更坏。
- 证据只在「行业板块(二级)」上测量（预研口径）；其他板块体系不适用，
  统计不可跨体系迁移。
- 构建由 ``backend/scripts/market_analysis/build_flow_evidence.py`` 夜间完成；
  本模块只读消费，另提供原子写工具供 builder 使用。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

LATEST_SCHEMA = "flow_evidence_latest_v1"
EVIDENCE_SCHEMA = "flow_evidence_v1"
EVIDENCE_SECTOR_SET = "industry2"  # 证据测量的板块体系（细案 §2.5）
MIN_COND_N = 500  # P 级门槛：条件样本 n 低于此值不上屏（细案 §3）
BADGE_LEVELS = (5, 3, 2)  # 徽章档位（由高到低，取第一个 streak 满足的档）
BADGE_HORIZON = 3  # 徽章默认前向窗口（交易日）

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _in_container() -> bool:
    """容器判定与 factor_board.config 同款（宿主开发用 QUANTMINT_HOST_MODE 覆盖）。"""
    return Path("/app").exists() and not os.environ.get("QUANTMINT_HOST_MODE")


def resolve_evidence_dir(explicit: str | Path | None = None) -> Path:
    """产物目录：显式参数 > QM_FLOW_EVIDENCE_DIR > 容器 /data/… > 仓库 data/…。"""
    if explicit:
        return Path(explicit)
    env = os.environ.get("QM_FLOW_EVIDENCE_DIR")
    if env:
        return Path(env)
    if _in_container():
        return Path("/data/market-analysis/flow_evidence")
    return _REPO_ROOT / "data" / "market-analysis" / "flow_evidence"


# ---------------------------------------------------------------------------
# 读取（按 mtime 失效；长驻进程在夜间产物重建后自动换新）
# ---------------------------------------------------------------------------

_json_cache: dict[str, tuple[float, dict[str, Any]]] = {}


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    key = str(path)
    hit = _json_cache.get(key)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("资金流证据产物读取失败 %s: %s", path, exc)
        return None
    if not isinstance(doc, dict):
        return None
    _json_cache[key] = (mtime, doc)
    return doc


def load_latest(evidence_dir: str | Path | None = None) -> dict[str, Any] | None:
    """当日命中榜与逐板块 streak；schema 不符返回 None。"""
    doc = _load_json(resolve_evidence_dir(evidence_dir) / "latest.json")
    if doc is None or doc.get("schema") != LATEST_SCHEMA:
        return None
    return doc


def load_evidence(evidence_dir: str | Path | None = None) -> dict[str, Any] | None:
    """信号档案（过闸条件统计 + 否决清单）；schema 不符返回 None。"""
    doc = _load_json(resolve_evidence_dir(evidence_dir) / "evidence.json")
    if doc is None or doc.get("schema") != EVIDENCE_SCHEMA:
        return None
    return doc


def load_sector_panel(evidence_dir: str | Path | None = None) -> pd.DataFrame | None:
    """逐日×板块面板（历史 streak 回溯）；缺失返回 None。"""
    path = resolve_evidence_dir(evidence_dir) / "panel_sector.parquet"
    if not path.exists():
        return None
    try:
        return pd.read_parquet(path)
    except (OSError, ValueError) as exc:
        logger.warning("资金流证据面板读取失败 %s: %s", path, exc)
        return None


def streak_map(latest: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """latest.json → {板块名: 行}；结构不符时返回空 dict。"""
    if not latest:
        return {}
    sectors = latest.get("sectors")
    if not isinstance(sectors, dict):
        return {}
    return {str(k): v for k, v in sectors.items() if isinstance(v, dict)}


def norm_trade_date(value: Any) -> str:
    """任意日期表示 → 去横线 8 位串（比较用）。"""
    return str(value or "").replace("-", "").strip()


# ---------------------------------------------------------------------------
# 证据徽章（P 级统一出口）
# ---------------------------------------------------------------------------


def pick_badge(
    streak: Any,
    evidence_doc: dict[str, Any] | None,
    horizon: int = BADGE_HORIZON,
) -> dict[str, Any] | None:
    """按 streak 选取最高满足档位的证据徽章；不满足任何档返回 None。

    门槛与细案 §3 一致：条件 n 不得低于 ``min_cond_n``；``gate.pass`` 为
    False 的条件不上屏。返回前端可直接渲染的结构（fwd=探针口径、exec=可执行口径）。
    """
    if isinstance(streak, bool) or not isinstance(streak, int):
        return None
    if streak < BADGE_LEVELS[-1] or not evidence_doc:
        return None
    conditions = evidence_doc.get("conditions") or {}
    min_n = int(evidence_doc.get("min_cond_n") or MIN_COND_N)
    for level in BADGE_LEVELS:
        if streak < level:
            continue
        cond = conditions.get(f"streak_{level}")
        if not isinstance(cond, dict):
            continue
        if (cond.get("gate") or {}).get("pass") is False:
            continue
        stats = (cond.get("fwd") or {}).get(str(horizon))
        if not isinstance(stats, dict) or not stats.get("n"):
            continue
        if int(stats["n"]) < min_n:
            continue
        return {
            "level": level,
            "horizon": horizon,
            "label": f"连续净流入 ≥{level} 日",
            "fwd": stats,
            "exec": (cond.get("exec") or {}).get(str(horizon)),
            "window": evidence_doc.get("window"),
            "min_n": min_n,
        }
    return None


# ---------------------------------------------------------------------------
# 原子写（builder 使用；读到一半的产物比没有更坏）
# ---------------------------------------------------------------------------


def _json_default(x: Any) -> Any:
    if isinstance(x, np.integer):
        return int(x)
    if isinstance(x, np.floating):
        return float(x)
    if isinstance(x, np.bool_):
        return bool(x)
    raise TypeError(f"不可序列化类型: {type(x)!r}")


def write_json_atomic(path: str | Path, doc: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(
            json.dumps(doc, ensure_ascii=False, indent=1, default=_json_default),
            encoding="utf-8",
        )
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def write_parquet_atomic(df: pd.DataFrame, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        df.to_parquet(tmp, index=False)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
