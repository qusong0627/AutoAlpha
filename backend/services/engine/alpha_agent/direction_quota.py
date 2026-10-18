"""生成侧代理模型 + 方向配额（T-FE-10）—— 三档预算的方向选择器。

**代理模型 v1 = 经验贝叶斯平滑成功率 + 饱和惩罚（刻意不引 LightGBM）**。
本平台现网挖掘 run 量级为「数十」条，GBM 在这个样本量上是过拟合剧场；
金融级验证求稳，v1 用可解释、可复现、零依赖的平滑估计。函数边界
:func:`build_score` / :func:`proxy_success_rate` 可整体替换为学得模型——
升级路径：run 数 ≥ 500 且标注质量过关后引入（届时评估脚本先行）。

公式（常量单源、测试钉数）：

- 成功率 ``p = (n_success + 0.5) / (n_tasks + 1.0)``（Jeffreys 先验；
  无史 → 0.5，中性不冒进）。``n_tasks`` 只计**终态**任务
  （completed/failed/cancelled）——进行中的任务尚无结局，不构成失败；
  ``n_success = completed ∧ factor_count ≥ 1``（与 T-FM-04 的可用语义同域）。
- 饱和惩罚 ``sat_penalty = min(0.5, dup_hits_90d / 4)``（T-FM-04 的
  ``SaturatedDirection.dup_hits_90d``；**缺 → 0，不臆造**）。
- ``score = p × (1 − sat_penalty)``。

**三档配额**（``QUOTA_EXPLORE/EXPLOIT/MUTATE``）：候选来源 = 传入方向词表
（与 selected/random 同源）；**硬饱和剔除**——``dup_hits_90d ≥ 2`` 的方向
从三档一律剔除（剔尽 → 回退**同档全集**并标 ``tier_fallback="saturated"``；
绝不空转、也绝不静默）。三档抽取：

- explore：空白度加权（:func:`direction_sampling.blankness_weights`，
  口径一字不改）；
- exploit：``score`` 加权 top-K（K=``EXPLOIT_TOPK``）内**线性归一**抽取
  （softmax 不留——简单、可复现、可解释）；
- mutate：最近成功任务方向集（最近 ``RECENT_SUCCESS_TASKS`` 个 success
  任务的 direction，去重；再与候选词表求交——用户没勾的方向不出自选）均匀
  抽；集空 → 退 exploit 并标 ``tier_fallback="mutate_empty"``（多因同时
  成立时按档位结构缺口优先标注，饱和事实看 ``scores_snapshot``）。

**可复现**（与 T-MV-03 同纪律）：逐条 meta 记录 ``{mode, tier, weighting,
seed, picked, candidates[{direction, attempts|score, weight}], scores_snapshot,
tier_fallback?}``——复算方按 ``Random(seed).choices(candidates, weights)``
重放必得同一命中；``candidates`` 的顺序**即抽样顺序**，不许重排。N ≥ 候选数
时直派全集 meta=None（没抽签就不伪造 seed，与 T-MV-04 同纪律）。

**N 方向并按配额比例切名额**（floor + 余数给 exploit；N 小于配额粒度时名额
集中在 exploit——单条派发即「代理引导利用」档）：每名额走对应档、跨槽不放回
（并行方向必须互不相同），逐条独立 meta。

**兜底纪律**（与 ``direction_sampling._read_attempts`` 同型）：读史/打分失败
**不拦任务创建**——退 random 空白度加权 + meta 标 ``quota_fallback: true``；
绝不假装配额成功。空候选显式 ValueError（静默返回空方向比报错更难查）。
"""

from __future__ import annotations

import logging
import random
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .direction_sampling import WEIGHTING_BLANKNESS, blankness_weights

logger = logging.getLogger(__name__)

# ── 常量（单源；测试钉数）────────────────────────────────────────────

#: 模式名（落任务 ``direction_mode`` 列与 meta.mode）。
MODE_QUOTA = "quota"

#: 三档名与槽位顺序（抽样顺序 = explore → exploit → mutate；重放依赖）。
TIER_EXPLORE = "explore"
TIER_EXPLOIT = "exploit"
TIER_MUTATE = "mutate"
TIER_ORDER = (TIER_EXPLORE, TIER_EXPLOIT, TIER_MUTATE)

#: 配额比例（D2 决策，常量文档化；名额切分 = floor + 余数给 exploit）。
QUOTA_EXPLORE = 0.3
QUOTA_EXPLOIT = 0.5
QUOTA_MUTATE = 0.2

#: 权重口径标签（meta.weighting）。blankness 复用 direction_sampling 单源；
#: proxy_score = 代理打分线性归一；uniform = 均匀。
WEIGHTING_BLANKNESS = WEIGHTING_BLANKNESS
WEIGHTING_PROXY = "proxy_score"
WEIGHTING_UNIFORM = "uniform"

#: 档位回退原因（meta.tier_fallback，仅回退时出现）。
FALLBACK_SATURATED = "saturated"
FALLBACK_MUTATE_EMPTY = "mutate_empty"

#: Jeffreys 先验（p = (s + 0.5) / (n + 1.0)）：无史 = 0.5 中性。
JEFFREYS_SUCCESS = 0.5
JEFFREYS_TOTAL = 1.0

#: 饱和惩罚 = min(上限, dup_hits_90d / 除数)。
SAT_PENALTY_CAP = 0.5
SAT_PENALTY_DIVISOR = 4.0

#: 硬饱和剔除阈值：dup_hits_90d ≥ 2 的方向从三档候选一律剔除。
SATURATED_MIN_DUP_HITS_90D = 2

#: 利用档 top-K 宽度；变异档「最近成功」任务数。
EXPLOIT_TOPK = 6
RECENT_SUCCESS_TASKS = 5

#: 预览默认名额数（D4）。
DEFAULT_PREVIEW_N = 3

#: seed 上限（2**31）：与 direction_sampling 同口径，落 JSON 人类可读。
_SEED_RANGE = 2**31

#: 终态状态集合（成功率分母：进行中的任务尚无结局）。
_TERMINAL_STATUSES = ("completed", "failed", "cancelled")


# ── 打分（纯函数）────────────────────────────────────────────────────


@dataclass(frozen=True)
class DirectionScore:
    """方向打分行（API 直出 + 配额内部共用同一份）。"""

    direction: str
    attempts: int  # 全状态任务数（空白度口径，与 direction_sampling 同源）
    n_tasks: int  # 终态任务数（成功率分母）
    n_success: int  # completed ∧ factor_count ≥ 1
    p: float
    dup_hits_90d: int
    sat_penalty: float
    score: float
    saturated: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "direction": self.direction,
            "attempts": self.attempts,
            "n_tasks": self.n_tasks,
            "n_success": self.n_success,
            "p": self.p,
            "dup_hits_90d": self.dup_hits_90d,
            "sat_penalty": self.sat_penalty,
            "score": self.score,
            "saturated": self.saturated,
        }


def proxy_success_rate(*, n_success: int, n_tasks: int) -> float:
    """Jeffreys 平滑成功率。脏数据（负数）按 0 收敛；上界 1.0（防御性）。"""
    ns = max(0, int(n_success))
    nt = max(0, int(n_tasks))
    p = (ns + JEFFREYS_SUCCESS) / (nt + JEFFREYS_TOTAL)
    return min(1.0, p)


def saturation_penalty(dup_hits_90d: int) -> float:
    """饱和惩罚 = min(0.5, dup_hits_90d / 4)；缺（0）→ 0，不臆造。"""
    return min(SAT_PENALTY_CAP, max(0, int(dup_hits_90d)) / SAT_PENALTY_DIVISOR)


def build_score(
    *,
    direction: str,
    attempts: int,
    n_tasks: int,
    n_success: int,
    dup_hits_90d: int,
) -> DirectionScore:
    """一行打分： ``score = p × (1 − sat_penalty)``；饱和标志按硬阈值。"""
    dup = max(0, int(dup_hits_90d))
    p = proxy_success_rate(n_success=n_success, n_tasks=n_tasks)
    sat = saturation_penalty(dup)
    return DirectionScore(
        direction=str(direction),
        attempts=max(0, int(attempts)),
        n_tasks=max(0, int(n_tasks)),
        n_success=max(0, int(n_success)),
        p=p,
        dup_hits_90d=dup,
        sat_penalty=sat,
        score=p * (1.0 - sat),
        saturated=dup >= SATURATED_MIN_DUP_HITS_90D,
    )


def split_slots(n: int) -> dict[str, int]:
    """N 名额按配额比例切分：floor + 余数给 exploit（总数恒等于 N）。"""
    total = max(0, int(n))
    raw = [int(total * q) for q in (QUOTA_EXPLORE, QUOTA_EXPLOIT, QUOTA_MUTATE)]
    raw[1] += total - sum(raw)
    return dict(zip(TIER_ORDER, raw, strict=True))


# ── 档位取池（纯函数；allocate 与 preview 共用同一实现）──────────────


@dataclass(frozen=True)
class TierPlan:
    """一档的预演池（API 直出；allocate 的槽位取池是它的逐步剩余版）。"""

    tier: str
    pool: tuple[str, ...]
    weighting: str
    fallback: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "pool": list(self.pool),
            "weighting": self.weighting,
            "fallback": self.fallback,
        }


def _topk(dirs: Sequence[str], by_dir: Mapping[str, DirectionScore]) -> list[str]:
    """按 (-score, direction) 降序取前 K——同分按方向名定序（可复现）。"""
    return sorted(dirs, key=lambda d: (-by_dir[d].score, d))[:EXPLOIT_TOPK]


def _blankness_pool(
    pool: list[str], by_dir: Mapping[str, DirectionScore]
) -> tuple[list[str], list[float], str, str]:
    """空白度加权（口径一字不改地复用 direction_sampling）。"""
    weights = blankness_weights([by_dir[d].attempts for d in pool])
    return pool, weights, WEIGHTING_BLANKNESS, "attempts"


def _exploit_pool(
    pool: list[str], by_dir: Mapping[str, DirectionScore]
) -> tuple[list[str], list[float], str, str]:
    """打分 top-K 线性归一权重（score 恒 > 0：p ≥ 0.25、惩罚 ≤ 0.5）。"""
    scores = [by_dir[d].score for d in pool]
    total = sum(scores)
    weights = [s / total for s in scores] if total > 0 else [1.0] * len(pool)
    return pool, weights, WEIGHTING_PROXY, "score"


def _uniform_pool(
    pool: list[str], by_dir: Mapping[str, DirectionScore]
) -> tuple[list[str], list[float], str, str]:
    return pool, [1.0] * len(pool), WEIGHTING_UNIFORM, "attempts"


def _resolve_pool(
    tier: str,
    remaining: Sequence[str],
    by_dir: Mapping[str, DirectionScore],
    recent_dirs: Sequence[str],
) -> tuple[list[str], list[float], str, str, str | None]:
    """一档在**当前剩余集合**上的取池与权重。

    返回 ``(pool, weights, weighting, value_key, fallback)``；``fallback``
    仅回退时非 None。回退链（多因同时成立时，按档位结构缺口优先标注）：

    - explore/exploit：非饱和剔尽 → 回退同档全集（``saturated``）；
    - mutate：近期成功∩候选 剔尽 → 回退该交集全集（``saturated``）；
      交集本身为空 → 退 exploit 池（``mutate_empty``）。
    """
    base = list(remaining)
    if not base:
        raise ValueError("方向配额取池需要至少一个候选")
    unsat = [d for d in base if not by_dir[d].saturated]

    if tier == TIER_EXPLORE:
        if unsat:
            pool, w, wt, vk = _blankness_pool(unsat, by_dir)
            return pool, w, wt, vk, None
        pool, w, wt, vk = _blankness_pool(base, by_dir)
        return pool, w, wt, vk, FALLBACK_SATURATED

    if tier == TIER_EXPLOIT:
        pool = _topk(unsat, by_dir)
        if pool:
            p, w, wt, vk = _exploit_pool(pool, by_dir)
            return p, w, wt, vk, None
        p, w, wt, vk = _exploit_pool(_topk(base, by_dir), by_dir)
        return p, w, wt, vk, FALLBACK_SATURATED

    # mutate
    recent = set(recent_dirs)
    mut = [d for d in base if d in recent]
    mut_unsat = [d for d in mut if not by_dir[d].saturated]
    if mut_unsat:
        p, w, wt, vk = _uniform_pool(mut_unsat, by_dir)
        return p, w, wt, vk, None
    if mut:
        p, w, wt, vk = _uniform_pool(mut, by_dir)
        return p, w, wt, vk, FALLBACK_SATURATED
    pool = _topk(unsat, by_dir)
    if pool:
        p, w, wt, vk = _exploit_pool(pool, by_dir)
        return p, w, wt, vk, FALLBACK_MUTATE_EMPTY
    p, w, wt, vk = _exploit_pool(_topk(base, by_dir), by_dir)
    return p, w, wt, vk, FALLBACK_MUTATE_EMPTY


def plan_tiers(
    scores: Sequence[DirectionScore], recent_success_dirs: Sequence[str]
) -> dict[str, TierPlan]:
    """三档预演（确定性）：各档在**全候选**上的取池与回退标注。

    与 :func:`allocate` 的槽位取池共用同一 :func:`_resolve_pool`——预览所见
    即首槽所用（后续槽位按逐步剩余收窄，见模块头注）。
    """
    by_dir = {s.direction: s for s in scores}
    remaining = [s.direction for s in scores]
    if not remaining:
        raise ValueError("plan_tiers 需要至少一个候选方向")
    plans: dict[str, TierPlan] = {}
    for tier in TIER_ORDER:
        pool, _w, weighting, _vk, fallback = _resolve_pool(
            tier, remaining, by_dir, recent_success_dirs
        )
        plans[tier] = TierPlan(
            tier=tier, pool=tuple(pool), weighting=weighting, fallback=fallback
        )
    return plans


# ── 分配（纯核心 + 读侧）──────────────────────────────────────────────


def _dedupe(directions: Sequence[str]) -> list[str]:
    """去重（保序）：两条同名方向派两个任务不是「并行方向」，是重复任务。"""
    clean: list[str] = []
    seen: set[str] = set()
    for d in directions:
        if isinstance(d, str) and d.strip() and d not in seen:
            seen.add(d)
            clean.append(d)
    return clean


def _allocate_pure(
    directions: Sequence[str],
    n: int,
    scores: Sequence[DirectionScore],
    *,
    recent_success_dirs: Sequence[str],
) -> list[tuple[str, dict[str, Any]]]:
    """逐步不放回配额分配（每步独立 seed，meta 可单条重放）。"""
    by_dir = {s.direction: s for s in scores}
    missing = [d for d in directions if d not in by_dir]
    if missing:
        raise ValueError(f"打分表缺候选方向：{missing[:3]}")
    snapshot = {s.direction: s.to_dict() for s in scores}
    slots = split_slots(n)
    remaining = list(directions)
    picks: list[tuple[str, dict[str, Any]]] = []
    for tier in TIER_ORDER:
        for _ in range(slots[tier]):
            pool, weights, weighting, value_key, fallback = _resolve_pool(
                tier, remaining, by_dir, recent_success_dirs
            )
            seed = random.SystemRandom().randrange(_SEED_RANGE)
            picked = random.Random(seed).choices(pool, weights=weights, k=1)[0]
            meta: dict[str, Any] = {
                "mode": MODE_QUOTA,
                "tier": tier,
                "weighting": weighting,
                "seed": seed,
                "picked": picked,
                "candidates": [
                    {
                        "direction": d,
                        "attempts" if value_key == "attempts" else "score": getattr(
                            by_dir[d], value_key
                        ),
                        "weight": w,
                    }
                    for d, w in zip(pool, weights, strict=True)
                ],
                "scores_snapshot": snapshot,
            }
            if fallback:
                meta["tier_fallback"] = fallback
            picks.append((picked, meta))
            remaining.remove(picked)
    return picks


async def _blankness_fallback(
    clean: Sequence[str], n: int, *, user_id: str, market: str
) -> list[tuple[str, dict[str, Any] | None]]:
    """配额输入不可用时的兜底：random 空白度加权 + ``quota_fallback`` 标注。"""
    from .direction_sampling import sample_weighted_directions_n

    pairs = await sample_weighted_directions_n(clean, n, user_id=user_id, market=market)
    return [
        (d, ({**meta, "quota_fallback": True} if meta else None)) for d, meta in pairs
    ]


@dataclass(frozen=True)
class QuotaInputs:
    """一次配额决策的全部输入快照（打分表 + 近期成功方向）。"""

    scores: tuple[DirectionScore, ...]
    recent_success_dirs: tuple[str, ...]


#: reader 注入点类型（默认实现打真库；单测注入桩）。
OutcomesReader = Callable[..., Awaitable[Mapping[str, tuple[int, int, int]]]]
RecentsReader = Callable[..., Awaitable[Sequence[str]]]
DupHitsReader = Callable[..., Awaitable[Mapping[str, int]]]


async def _load_inputs(
    directions: Sequence[str] | None,
    *,
    user_id: str,
    market: str,
    outcomes_reader: OutcomesReader | None = None,
    recents_reader: RecentsReader | None = None,
    dup_reader: DupHitsReader | None = None,
) -> QuotaInputs:
    """读侧组装：``directions=None`` = 全史（预览无候选词表时用）。"""
    outcomes = await (outcomes_reader or _read_direction_outcomes)(
        directions, user_id=user_id, market=market
    )
    recents = await (recents_reader or _read_recent_success_dirs)(
        user_id=user_id, market=market
    )
    dup = await (dup_reader or _read_dup_hits)(user_id=user_id, market=market)

    keys = sorted(outcomes) if directions is None else list(directions)
    scores: list[DirectionScore] = []
    for d in keys:
        attempts, n_tasks, n_success = outcomes.get(d, (0, 0, 0))
        scores.append(
            build_score(
                direction=d,
                attempts=attempts,
                n_tasks=n_tasks,
                n_success=n_success,
                dup_hits_90d=int(dup.get(d, 0)),
            )
        )
    return QuotaInputs(
        scores=tuple(scores), recent_success_dirs=tuple(str(d) for d in recents)
    )


async def allocate(
    directions: Sequence[str],
    n: int,
    *,
    user_id: str,
    market: str,
    outcomes_reader: OutcomesReader | None = None,
    recents_reader: RecentsReader | None = None,
    dup_reader: DupHitsReader | None = None,
) -> list[tuple[str, dict[str, Any] | None]]:
    """配额分配 N 条方向：``[(direction, meta|None)]``（顺序 = 槽位顺序）。

    - ``n ≥ 候选数``：全集直派、meta=None（与 T-MV-04 同纪律）；
    - 读史/打分失败：退 random 空白度加权 + meta 标 ``quota_fallback``
      ——fail-open，绝不拦任务创建、绝不假装配额成功。
    """
    clean = _dedupe(directions)
    if not clean:
        raise ValueError("allocate 需要至少一个非空方向")
    total = max(1, int(n))
    if total >= len(clean):
        return [(d, None) for d in clean]
    try:
        inputs = await _load_inputs(
            clean,
            user_id=user_id,
            market=market,
            outcomes_reader=outcomes_reader,
            recents_reader=recents_reader,
            dup_reader=dup_reader,
        )
        picks: list[tuple[str, dict[str, Any] | None]] = [
            (d, m)
            for d, m in _allocate_pure(
                clean,
                total,
                inputs.scores,
                recent_success_dirs=inputs.recent_success_dirs,
            )
        ]
        return picks
    except Exception as e:  # noqa: BLE001 - 配额输入失败不许拦任务创建
        logger.warning("[direction-quota] 配额输入不可用，退随机空白度加权: %s", e)
        return await _blankness_fallback(clean, total, user_id=user_id, market=market)


async def preview(
    *,
    user_id: str,
    market: str,
    directions: Sequence[str] = (),
    n: int = DEFAULT_PREVIEW_N,
    outcomes_reader: OutcomesReader | None = None,
    recents_reader: RecentsReader | None = None,
    dup_reader: DupHitsReader | None = None,
) -> dict[str, Any]:
    """只读预览（D4）：打分表 + 名额切分 + 三档预演（无候选词表则无档位）。

    读取失败 → ``available=false`` + 稳定文案（错误原文只进日志不上屏）。
    """
    clean = _dedupe(directions or ())
    total = max(1, int(n))
    base: dict[str, Any] = {
        "available": True,
        "market": market,
        "n": total,
        "quota": {
            "explore": QUOTA_EXPLORE,
            "exploit": QUOTA_EXPLOIT,
            "mutate": QUOTA_MUTATE,
        },
        "slots": split_slots(total),
        "saturated_threshold": SATURATED_MIN_DUP_HITS_90D,
        "recent_success_tasks": RECENT_SUCCESS_TASKS,
    }
    try:
        inputs = await _load_inputs(
            clean or None,
            user_id=user_id,
            market=market,
            outcomes_reader=outcomes_reader,
            recents_reader=recents_reader,
            dup_reader=dup_reader,
        )
        rows = (
            inputs.scores
            if clean
            else tuple(sorted(inputs.scores, key=lambda s: (-s.score, s.direction)))
        )
        tiers = (
            {
                t: p.to_dict()
                for t, p in plan_tiers(
                    inputs.scores, inputs.recent_success_dirs
                ).items()
            }
            if clean
            else None
        )
        return {
            **base,
            "directions": [s.to_dict() for s in rows],
            "tiers": tiers,
            "recent_success_dirs": list(inputs.recent_success_dirs),
            "error": None,
        }
    except Exception as e:  # noqa: BLE001 - 预览失败如实降级，不 5xx
        logger.warning("[direction-quota] 预览读取失败: %s", e)
        return {
            **base,
            "available": False,
            "directions": [],
            "tiers": None,
            "recent_success_dirs": [],
            "error": "方向史/饱和数据读取出错，配额预览暂不可用（详见服务端日志）",
        }


# ── 默认读侧（真库；注入点之外只有这里碰 DB）──────────────────────────


async def _read_direction_outcomes(
    directions: Sequence[str] | None, *, user_id: str, market: str
) -> dict[str, tuple[int, int, int]]:
    """本用户 × 本市场照方向聚合：``direction → (attempts, n_tasks, n_success)``。

    - attempts = 全状态计数（与 ``count_by_direction`` 空白度口径同源）；
    - n_tasks = 终态计数（completed/failed/cancelled——成功率分母）；
    - n_success = completed ∧ factor_count ≥ 1。
    ``directions=None`` = 全史（预览无词表时用）；空词表零 SQL 直返。
    """
    ids = None if directions is None else [d for d in directions if d]
    if ids is not None and not ids:
        return {}
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    sql = (
        "SELECT direction, count(*) AS attempts, "
        "count(*) FILTER (WHERE status = ANY(:terminal)) AS n_tasks, "
        "count(*) FILTER (WHERE status = 'completed' AND factor_count >= 1) "
        "AS n_success "
        "FROM rd_agent_mining_tasks "
        "WHERE user_id = :user_id AND market = :market AND direction <> ''"
    )
    params: dict[str, Any] = {
        "user_id": str(user_id),
        "market": str(market),
        "terminal": list(_TERMINAL_STATUSES),
    }
    if ids is not None:
        sql += " AND direction = ANY(:directions)"
        params["directions"] = ids
    sql += " GROUP BY direction"
    async with get_session(read_only=True) as session:
        rows = (await session.execute(text(sql), params)).mappings().all()
    return {
        str(r["direction"]): (
            int(r["attempts"]),
            int(r["n_tasks"]),
            int(r["n_success"]),
        )
        for r in rows
    }


async def _read_recent_success_dirs(
    *, user_id: str, market: str, limit: int = RECENT_SUCCESS_TASKS
) -> list[str]:
    """最近 ``limit`` 个 success 任务的 direction（去重保序，近期在前）。

    口径按任务数（LIMIT 是**任务行数**不是方向数）——最近 5 个任务恰好
    同方向时 mutate 集就是 1 条，这正是「最近哪里成了」的事实。
    """
    from sqlalchemy import text

    from backend.shared.database_manager_v2 import get_session

    async with get_session(read_only=True) as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT direction FROM rd_agent_mining_tasks "
                        "WHERE user_id = :user_id AND market = :market "
                        "AND status = 'completed' AND factor_count >= 1 "
                        "AND direction <> '' "
                        "ORDER BY COALESCE(completed_at, updated_at) DESC "
                        "LIMIT :lim"
                    ),
                    {
                        "user_id": str(user_id),
                        "market": str(market),
                        "lim": int(limit),
                    },
                )
            )
            .mappings()
            .all()
        )
    out: list[str] = []
    for r in rows:
        d = str(r["direction"] or "")
        if d and d not in out:
            out.append(d)
    return out


async def _read_dup_hits(*, user_id: str, market: str) -> dict[str, int]:
    """T-FM-04 饱和表 → ``{direction: dup_hits_90d}``；缺的方向语义 = 0（真零）。"""
    from backend.services.engine.alpha_agent import experience_memory

    memory = await experience_memory.load_memory(
        user_id=str(user_id), market=str(market)
    )
    return {s.direction: int(s.dup_hits_90d) for s in memory.saturated_directions}


__all__ = [
    "DEFAULT_PREVIEW_N",
    "EXPLOIT_TOPK",
    "FALLBACK_MUTATE_EMPTY",
    "FALLBACK_SATURATED",
    "MODE_QUOTA",
    "QUOTA_EXPLOIT",
    "QUOTA_EXPLORE",
    "QUOTA_MUTATE",
    "RECENT_SUCCESS_TASKS",
    "SATURATED_MIN_DUP_HITS_90D",
    "TIER_EXPLOIT",
    "TIER_EXPLORE",
    "TIER_MUTATE",
    "TIER_ORDER",
    "WEIGHTING_BLANKNESS",
    "WEIGHTING_PROXY",
    "WEIGHTING_UNIFORM",
    "DirectionScore",
    "QuotaInputs",
    "TierPlan",
    "allocate",
    "build_score",
    "plan_tiers",
    "preview",
    "proxy_success_rate",
    "saturation_penalty",
    "split_slots",
]
