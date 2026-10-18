"""任务级数据版本快照（移植 P3）——「这一单是在哪份数据上挖的」。

背景（参考实现把 provider 覆盖 + 逐数据集版本打进任务行，同型）：数据日更
之后回看历史任务，「当时的 IC 是在哪份特征上算的」必须可从任务行本身回答，
不能拿今天的覆盖去解释昨天的结果。

版本的判据 = **盘上事实**（不做自造版本号）：

- 覆盖区间 = ``dt=YYYYMMDD`` 分区目录名的 min/max——与
  :func:`rd_loop_wrapper.quantdb_coverage_from_dir` 同一口径（挖掘 h5
  实际读取的库，不用交易日历冒充数据覆盖）；
- 最新分区 + 其内 parquet 的最大 mtime = 该数据集的「版本戳」——与 h5
  共享缓存的新鲜度判定（``panel_cache.is_fresh``）读的是**同一份事实**：任务
  看到的版本与缓存判废用的版本不会各说各话；
- 分钟口径另带 provider 的可用连续块（日线覆盖 2016→2026 与 1min 断层
  不是一回事）。

**Fail-open**：快照是审计面不是主链——任何一项读不到都进 ``errors``（或
``note``）如实上屏，绝不拦任务创建；读不到就不写假的。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from backend.shared.utc_datetime import UTC, to_utc_iso, utc_now
from backend.services.engine.rd_agent.mining_datasets import DATASET_REL_PATHS

logger = logging.getLogger(__name__)

#: K 线主库 = ``1_kline_data/daily_forward``。
_KLINE_REL = "1_kline_data/daily_forward"
_PARTITION_PREFIX = "dt="


def partition_facts(base: Path) -> dict | None:
    """``dt=YYYYMMDD`` 分区目录 → 版本事实；目录缺失/无分区回 ``None``。

    返回 ``{start, end, latest_partition, latest_mtime}``：前两者是分区名
    归一出的覆盖区间（ISO），后两者是最新分区号与其内文件最大 mtime
    （UTC ISO）。读盘异常只告警回 None——调用方把它记为「该项目不可读」。
    """
    try:
        if not base.is_dir():
            return None
        names = sorted(
            d.name[len(_PARTITION_PREFIX) :]
            for d in base.iterdir()
            if d.is_dir() and d.name.startswith(_PARTITION_PREFIX)
        )
        names = [n for n in names if len(n) == 8 and n.isdigit()]
        if not names:
            return None
        latest_dir = base / f"{_PARTITION_PREFIX}{names[-1]}"
        try:
            mtimes = [f.stat().st_mtime for f in latest_dir.iterdir() if f.is_file()]
            mtime = max(mtimes) if mtimes else latest_dir.stat().st_mtime
        except OSError:
            mtime = latest_dir.stat().st_mtime
        return {
            "start": f"{names[0][:4]}-{names[0][4:6]}-{names[0][6:]}",
            "end": f"{names[-1][:4]}-{names[-1][4:6]}-{names[-1][6:]}",
            "latest_partition": names[-1],
            "latest_mtime": to_utc_iso(datetime.fromtimestamp(mtime, tz=UTC)),
        }
    except OSError as exc:
        logger.warning("数据分区事实读取失败 %s：%s", base, exc)
        return None


def flat_file_facts(base: Path) -> dict | None:
    """单文件/按标的分片数据的可追溯事实（财务表与分钟线）。"""
    try:
        files = [entry for entry in base.iterdir() if entry.is_file() and entry.suffix == ".parquet"]
        if not files:
            return None
        latest = max(files, key=lambda entry: entry.stat().st_mtime)
        return {
            "file_count": len(files),
            "latest_file": latest.name,
            "latest_mtime": to_utc_iso(datetime.fromtimestamp(latest.stat().st_mtime, tz=UTC)),
        }
    except OSError as exc:
        logger.warning("数据文件事实读取失败 %s：%s", base, exc)
        return None


def _resolve_quantdb_dir() -> str | None:
    """QuantDB 目录解析（复用挖掘数据面的同一解析器，不另立规则）。"""
    from backend.services.engine.rd_agent.rd_loop_wrapper import RDLoopWrapper

    return RDLoopWrapper._resolve_quantdb_dir()


def _minute_facts() -> dict | None:
    """分钟数据面事实（provider 全区间 / 可用连续块 / 断层）；未构建回 None。"""
    from backend.services.engine.alpha_agent.launcher import minute_scope

    scope = minute_scope()
    if scope is None:
        return None
    return {
        "coverage": {"start": scope.coverage.start.isoformat(), "end": scope.coverage.end.isoformat()},
        "usable": {"start": scope.usable.start.isoformat(), "end": scope.usable.end.isoformat()},
        "usable_days": scope.days,
        "gaps": [
            {"start": a.isoformat(), "end": b.isoformat()} for a, b in scope.gaps
        ],
    }


def snapshot_data_versions(
    *,
    market: str,
    freq: str | None,
    datasets: Sequence[str],
    quantdb_dir: str | None = None,
    captured_at: datetime | None = None,
) -> dict:
    """提交时刻的数据版本快照（调用方已 resolve 的 datasets 登记序元组）。

    ``datasets`` 只在 ``market='a_share'`` 有意义（其余市场数据面不走
    QuantDB，如实记 note 而不是编一份跨市场版本表）。
    """
    snapshot: dict = {
        # 快照时刻（UTC，ISO-8601 带 Z）：写入时间 ≠ 数据版本，两者都留
        "snapshot_at": to_utc_iso(captured_at or utc_now()),
        "market": market,
        "freq": freq or "day",
        "provider": None,
        "datasets": {},
        "minute": None,
        "note": None,
        "errors": [],
    }
    if market != "a_share":
        snapshot["note"] = f"{market} 数据面不走 QuantDB，暂无数据集版本事实"
        return snapshot

    try:
        resolved_dir = (
            quantdb_dir if quantdb_dir is not None else _resolve_quantdb_dir()
        )
    except Exception as exc:  # noqa: BLE001 - 快照失败不拦任务创建
        logger.warning("QuantDB 目录解析失败：%s", exc)
        resolved_dir = None
    if not resolved_dir:
        snapshot["errors"].append(
            "QuantDB 数据目录不可用（QM_QUANTDB_DATA_DIR / /data/quantdb 均不存在）"
        )
        return snapshot

    base = Path(resolved_dir)
    snapshot["provider"] = {
        "name": "quantdb",
        "dir": str(base),
        "kline": partition_facts(base / _KLINE_REL),
    }
    if snapshot["provider"]["kline"] is None:
        snapshot["errors"].append("K 线主库（1_kline_data/daily_forward）无可用分区")

    for key in datasets:
        rel_path = DATASET_REL_PATHS.get(key)
        facts = None
        if rel_path:
            dataset_dir = base / rel_path
            facts = partition_facts(dataset_dir) or flat_file_facts(dataset_dir)
        if facts is None:
            snapshot["errors"].append(f"数据集 {key} 无可用数据文件（目录缺失或为空）")
            continue
        snapshot["datasets"][key] = facts

    if (freq or "") == "1min":
        try:
            snapshot["minute"] = _minute_facts()
        except Exception as exc:  # noqa: BLE001 - 分钟事实读不到如实记一笔
            logger.warning("分钟数据面事实读取失败：%s", exc)
            snapshot["errors"].append(f"分钟数据面事实读取失败：{exc}")
        if snapshot["minute"] is None:
            snapshot["errors"].append("分钟数据面未构建（provider 不可读）")

    return snapshot
