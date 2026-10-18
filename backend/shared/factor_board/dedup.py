"""因子概率板：入选内二次去重（P2-e，|ρ|≤0.9 席位置换）。

算法复用 ``alpha_agent/statistical/dedup.greedy_select``（值级相关性去重的
唯一实现，Tier 3 / T-S3-03）——本模块只做板侧编排：

1. :func:`build_pool` 族头池 = 入选（|ic60| 降序，**前排**）∪ 各族
   ``gated−sel`` 的 |ic60| 头 ``FAMILY_CAP`` 名替补（后排）；
2. :func:`build_panel` 池内因子评估窗 ``(trade_date, symbol)`` 长序列
   （greedy 的 provider 层，按需取列；内存 O(池 × 窗)）；
3. :func:`dedup_selection` greedy（``top_k=len(sel)``、reject 0.9）+
   **族 cap 后置修剪**（替补递补理论上可把某族顶过 CAP，修剪按 greedy
   序保留族内前 CAP 名，失去的席位不回收——「不占席递补」同款语义）。

**恒等退化**（单测钉住）：无相关对时 kept 前 ``top_k`` = 入选原样——
池序保证任何一个替补的次序都在全部入选之后。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from backend.services.engine.alpha_agent.statistical.dedup import greedy_select
from backend.shared.factor_board import composite as CP
from backend.shared.factor_board import config as C

log = logging.getLogger("factor_board.dedup")


def key_of(lib: str, name: str) -> str:
    """去重键（greedy 的 order/provider 字面量；因子名不含 ``::``）。"""
    return f"{lib}::{name}"


def split_key(key: str) -> tuple[str, str]:
    lib, _, name = key.partition("::")
    return lib, name


def _cap_of(lib: str) -> int:
    return C.RD_FAMILY_CAP if lib == "rd_mined" else C.FAMILY_CAP


def build_pool(gated: pd.DataFrame, sel: pd.DataFrame) -> pd.DataFrame:
    """族头池：sel（|ic60| 降序）在前，各族 gated−sel |ic60| 头 CAP 名替补在后。

    ``gated``：过闸全表（select_factors 的 ``info["gated_table"]``，含降级
    分支镜像）；``sel`` 须已带 ``sgn/w``。返回列：lib/name/group/ic60/sgn/w/
    key/is_sel（行序 = greedy 的 order）。
    """
    g = gated.copy()
    # gated 表无 sgn/w（select_factors 才补）→ 替补行按同式补齐：
    # sgn=sign(ic60)、w=|ic60| clip——与 sel 侧恒等（同一公式，两处必须同源同值）。
    g["sgn"] = np.sign(g["ic60"]).astype(int)
    g["w"] = g["ic60"].abs().clip(C.W_CLIP_LO, C.W_CLIP_HI)
    g["key"] = [key_of(lib, n) for lib, n in zip(g["lib"], g["name"], strict=True)]
    s = sel.copy()
    s["key"] = [key_of(lib, n) for lib, n in zip(s["lib"], s["name"], strict=True)]
    sel_keys = set(s["key"])

    sel_sorted = s.sort_values("ic60", key=lambda v: v.abs(), ascending=False)
    res = g[~g["key"].isin(sel_keys)]
    heads: list[pd.DataFrame] = []
    for (lib, _grp), fam in res.groupby(["lib", "group"]):
        heads.append(
            fam.sort_values("ic60", key=lambda v: v.abs(), ascending=False).head(
                _cap_of(lib)
            )
        )
    cols = ["lib", "name", "group", "ic60", "sgn", "w", "key"]
    top = sel_sorted.reindex(columns=cols).assign(is_sel=True)
    if not heads:
        return top.reset_index(drop=True)
    reserve = pd.concat(heads).sort_values(
        "ic60", key=lambda v: v.abs(), ascending=False
    )
    reserve = reserve.reindex(columns=cols).assign(is_sel=False)
    return pd.concat([top, reserve], ignore_index=True)


def build_panel(
    quantdb_root: Path, pool: pd.DataFrame, dates: list[str]
) -> dict[str, pd.Series]:
    """池内因子在 ``dates`` 上的 ``(trade_date, symbol)`` 长序列（NaN 先丢）。

    逐日逐库一次读取（``read_selected_frames`` 只取池内列）。读不到的
    库/日在池键上留空序列——``daily_rank_corr`` 对「无重叠日」按无证据
    处理（保留并记 NaN），不造假 0 相关。
    """
    by_lib = pool.groupby("lib")["name"].apply(list).to_dict()
    acc: dict[str, list[pd.Series]] = {k: [] for k in pool["key"]}
    for dt in dates:
        frames = CP.read_selected_frames(quantdb_root, pool[["lib", "name"]], dt)
        for lib, d in frames.items():
            for n in by_lib.get(lib, []):
                v = d[n].dropna()
                if v.empty:
                    continue
                idx = pd.MultiIndex.from_arrays(
                    [
                        np.full(len(v), dt, dtype=object),
                        v.index.to_numpy(),
                    ],
                    names=["trade_date", "symbol"],
                )
                acc[key_of(lib, n)].append(
                    pd.Series(v.to_numpy(dtype=float), index=idx)
                )
    return {
        k: (pd.concat(vs) if vs else pd.Series(dtype=float)) for k, vs in acc.items()
    }


@dataclass(frozen=True)
class DedupOutcome:
    """一次板侧去重的全量记录（key 序 = greedy 步行序，即最终席位序）。"""

    kept_keys: tuple[str, ...]
    pruned_keys: tuple[str, ...]
    #: (key, matched_key, ρ)——被 |ρ|≥reject 拒掉的候选及其命中对象
    dropped: tuple[tuple[str, str, float], ...]
    #: (key_a, key_b, ρ)——保留但对内 ρ ∈ [warn, reject)
    warnings: tuple[tuple[str, str, float], ...]
    pool: pd.DataFrame
    n_reserves: int
    max_corr: dict[str, float]
    reject: float = C.DEDUP_REJECT
    warn: float = C.DEDUP_WARN


def dedup_selection(
    quantdb_root: Path,
    gated: pd.DataFrame,
    sel: pd.DataFrame,
    dates: list[str],
) -> DedupOutcome:
    """编排：建池 → 建面板 → greedy（top_k=len(sel)）→ 族 cap 后置修剪。

    ``dates`` = 评估窗（由调用方给定；dedup 只需评估窗的因子值，无需前向）。
    池无替补（gated−sel 为空）时走恒等退化路径，不建面板。
    """
    pool = build_pool(gated, sel)
    n_reserves = int((~pool["is_sel"]).sum())
    top_k = int(len(sel))
    if n_reserves == 0:
        return DedupOutcome(
            kept_keys=tuple(pool["key"]),
            pruned_keys=(),
            dropped=(),
            warnings=(),
            pool=pool,
            n_reserves=0,
            max_corr={},
        )
    panel = build_panel(quantdb_root, pool, dates)
    report = greedy_select(
        pool["key"].tolist(),
        provider=panel.__getitem__,
        top_k=top_k,
        reject=C.DEDUP_REJECT,
        warn=C.DEDUP_WARN,
        on_drop=panel.pop,  # 被拒候选即刻释放缓存（峰值 O(走过候选数) → O(K+1)）
    )
    meta = pool.set_index("key")
    kept: list[str] = []
    pruned: list[str] = []
    counts: dict[str, int] = {}
    for key in report.kept:
        lib = str(meta.loc[key, "lib"])
        grp = str(meta.loc[key, "group"])
        if counts.get(grp, 0) >= _cap_of(lib):
            pruned.append(key)  # 族 cap 后置修剪：失去的席位不回收
            continue
        counts[grp] = counts.get(grp, 0) + 1
        kept.append(key)
    return DedupOutcome(
        kept_keys=tuple(kept),
        pruned_keys=tuple(pruned),
        dropped=report.dropped,
        warnings=report.warnings,
        pool=pool,
        n_reserves=n_reserves,
        max_corr=dict(report.max_corr),
        reject=report.reject,
        warn=report.warn,
    )


def dedup_block(outcome: DedupOutcome) -> dict:
    """产物顶层 ``dedup`` 块（JSON 安全；rho 供 UI tooltip）。"""

    def _clean(x: float):
        v = float(x)
        return None if not np.isfinite(v) else round(v, 4)

    return {
        "n_pool": int(len(outcome.pool)),
        "n_reserves": int(outcome.n_reserves),
        "n_kept": int(len(outcome.kept_keys)),
        "reject": float(outcome.reject),
        "warn": float(outcome.warn),
        "dropped": [
            {"key": k, "matched_key": m, "rho": _clean(r)}
            for k, m, r in outcome.dropped
        ],
        "pruned": list(outcome.pruned_keys),
        "warnings": [
            {"key": a, "matched_key": b, "rho": _clean(r)}
            for a, b, r in outcome.warnings
        ],
    }
