"""变体残差台账（T-FE-09）：编辑模因记忆的统计侧先行版。

台账（``docs/因子挖掘_方法论学习与后期优化_20261011.md`` §3）：「每个
『家族×窗口/变换』变体记录对家族基线的残差 IC（同窗同池），跨 run 累积；
置信门控（样本天数不足不给结论）+ 非对称否决（高置信度失败变体进冷却
名单，不再重算）」。

**编辑模因语义**（AlphaMemo SSPM 改造）：子变体对基线的编辑分四类——
``调窗``（窗口链相邻步进）/ ``换算子``（$volume→$amount）/ ``加门``
（adaptive 族门控于无门控母本）/ ``矩变换``（估计量变换）。父子映射
:data:`BASELINE_BY_KEY` 是**显式表**（不是机械规则），与 ``candidates``
目录的一致性由测试双向钉死。

纪律（与 incremental / factor_lifecycle 同源）：

- **缺失永不冒充通过**：不判失败的状态（``insufficient_days`` /
  ``unavailable``）只留观测量，不进冷却；残差 ICIR 已取绝对值，强负残差
  不是失败（方向由 signal_side 管）。
- **父本保护**：任何出现在映射**值**里的键（链中节点）永不被冷却——冷却
  父本会掐断其子的控制链（引擎侧另有 ``no_baseline_in_run`` 兜底）。
- **非对称否决**：只有「高置信失败」（``ok`` ∧ n_days ≥ 40 =
  ``ic_stats.DEFAULT_MIN_VALID_DAYS`` ∧ 残差 ICIR < 0.3 = T-FE-01 增量闸
  同标尺）才冷却；新鲜高置信**通过**观测解除冷却（lift，否决有泄压阀）。
- **冷却按 (变体, 池) 键控** + 90 天 TTL：换池即重评（跨池证据迁移是
  启发式），TTL 到期自动失效重评。
- **判别只看 best_horizon**：在变体表现最好的周期上仍无增量才算失败
  （winner's curse 方向 = 偏不冷却，保守侧）；全 4 周期观测照记供审计。
- 并发：统计派任务可并行（launcher 排队），读-改-写必须走 :func:`ledger_lock`
  （flock 独占）；落盘同目录 tmp + ``os.replace`` 原子换（读者永不见
  半截 JSON）；坏 JSON 自愈为空骨架（不抛）。

运维 CLI：``python -m backend.services.engine.alpha_agent.statistical.variant_ledger
--show`` / ``--reset-cooling KEY [--universe POOL]``。
"""

from __future__ import annotations

import fcntl
import json
import os
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from backend.shared.utc_datetime import utc_now, to_utc_iso

from . import candidates as _candidates
from . import ic_stats as _ic

LEDGER_SCHEMA_VERSION = 1

#: 冷却 TTL（天）：到期自动失效重评（复核窗口需覆盖足够长的市场状态）
COOLDOWN_DAYS = 90

#: 高置信线 = IC 有效日判线的现行标尺（``ic_stats.DEFAULT_MIN_VALID_DAYS``
#: = 40）——低于此的观测不判失败（样本不足不给结论）
HIGH_CONF_DAYS = _ic.DEFAULT_MIN_VALID_DAYS

#: 残差 ICIR 判失败线 = T-FE-01 库级增量闸同标尺（对本族父本都过不了增量
#: 标尺 ⇒ 族内冗余）；≥ 此值的观测为「高置信通过」，解除既有冷却
COOL_ICIR_FLOOR = 0.3

#: 每变体保留的最近观测条数（跨 run 累积，裁剪保尾）
MAX_OBSERVATIONS = 24

#: 观望量：观测幂等键的组成字段（同 run 重跑覆盖同一条，不堆积）
_OBS_IDEM_FIELDS = ("universe", "window_start", "window_end", "run_id")

# ── 父子映射（编辑模因；与 candidates 目录的一致性由测试双向钉死）─────

#: 子变体 → 父本（基线）。缺席的键 = 根（无父本）或无母本不判
#: （``vol_kurt_gate_10d``：池内无 10 日无门控母本，宁可留空不臆造）。
BASELINE_BY_KEY: dict[str, str] = {
    # 反转窗链 5→10→20→60（De Bondt 长期反转）
    "reversal_10d": "reversal_5d",
    "reversal_20d": "reversal_10d",
    "reversal_60d": "reversal_20d",
    # 动量窗链 20→60→120；高点接近度 = 同 ~250 日窗估计量变换
    "momentum_60d": "momentum_20d",
    "momentum_120d": "momentum_60d",
    "high_prox_250d": "momentum_120d",
    # 同窗分布矩：MAX → 偏度
    "skew_20d": "max_ret_20d",
    # 波动窗链 + 同窗估计量变换（下行/极值估计量 → close-to-close）
    "vol_60d": "vol_20d",
    "downside_vol_20d": "vol_20d",
    "parkinson_20d": "vol_20d",
    "atr_14": "vol_20d",
    # 非流动性窗链 5→20→放量倍数；$volume→$amount 换算子；量变异系数
    "amihud_20d": "amihud_5d",
    "volume_spike_20d": "amihud_20d",
    "amount_burst_20d": "volume_spike_20d",
    "volume_cv_20d": "volume_spike_20d",
    # 量价相关窗链 20→60
    "pv_corr_60d": "pv_corr_20d",
    # 振幅窗链 5→20；隔夜/跳空同式窗链 5→20；日内位置 = 同窗估计量变换
    "range_20d": "range_5d",
    "overnight_ret_20d": "gap_drift_5d",
    "close_position_20d": "intraday_ret_20d",
    # adaptive 族 = 加门（门控于无门控母本）
    "volreg_reversal_5d": "reversal_5d",
    "trend_r2_gate_20d": "momentum_20d",
    "skew_gate_momentum_20d": "momentum_20d",
}

#: 子变体 → 编辑类型（展示面用；与 BASELINE_BY_KEY 同键集）
EDIT_TYPE_BY_KEY: dict[str, str] = {
    "reversal_10d": "调窗",
    "reversal_20d": "调窗",
    "reversal_60d": "调窗",
    "momentum_60d": "调窗",
    "momentum_120d": "调窗",
    "high_prox_250d": "估计量变换",
    "skew_20d": "矩变换",
    "vol_60d": "调窗",
    "downside_vol_20d": "估计量变换",
    "parkinson_20d": "估计量变换",
    "atr_14": "估计量变换",
    "amihud_20d": "调窗",
    "volume_spike_20d": "估计量变换",
    "amount_burst_20d": "换算子",
    "volume_cv_20d": "估计量变换",
    "pv_corr_60d": "调窗",
    "range_20d": "调窗",
    "overnight_ret_20d": "调窗",
    "close_position_20d": "估计量变换",
    "volreg_reversal_5d": "加门",
    "trend_r2_gate_20d": "加门",
    "skew_gate_momentum_20d": "加门",
}


def protected_parent_keys() -> frozenset[str]:
    """父本保护集：出现在映射值里的键（链中节点）永不被冷却。"""
    return frozenset(BASELINE_BY_KEY.values())


def coolable_keys() -> frozenset[str]:
    """可冷却集 = 有父本的子变体 − 父本保护集（现网 16 键）。"""
    return frozenset(BASELINE_BY_KEY) - protected_parent_keys()


def default_ledger_path() -> Path:
    """台账落点：``QM_VARIANT_LEDGER_PATH`` 覆盖 → else 容器卷 ``/data``。

    与 ``mining_pool_rebuild`` 等运维面同款解析（容器内绝对路径、env
    可覆盖指向 tmp 隔离）。宿主侧跑 runner 请显式设 env。
    """
    override = os.getenv("QM_VARIANT_LEDGER_PATH")
    if override:
        return Path(override)
    return Path("/data/factor_variants/variant_ledger.json")


# ── 台账 IO ────────────────────────────────────────────────────────────


def empty_ledger() -> dict[str, Any]:
    return {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "updated_at": None,
        "variants": {},
        "cooling": [],
    }


def load_ledger(path: Path | str) -> dict[str, Any]:
    """读台账；缺失/坏 JSON/结构不符一律自愈为空骨架（不抛）。"""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("ledger root not a dict")
    except (OSError, ValueError):
        return empty_ledger()
    raw.setdefault("schema_version", LEDGER_SCHEMA_VERSION)
    if not isinstance(raw.get("variants"), dict):
        raw["variants"] = {}
    if not isinstance(raw.get("cooling"), list):
        raw["cooling"] = []
    raw.setdefault("updated_at", None)
    return raw


def save_ledger(path: Path | str, ledger: Mapping[str, Any]) -> None:
    """原子落盘：同目录 tmp 写完 ``os.replace``（读者永不见半截 JSON）。

    ``allow_nan=False``：NaN 漏出即响亮失败（json 方言 ``NaN`` 不是合法
    JSON，Postgres/``JSON.parse`` 都拒收）——观测构造侧已保证 None 化。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(
        json.dumps(
            dict(ledger),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        ),
        encoding="utf-8",
    )
    os.replace(tmp, target)


@contextmanager
def ledger_lock(path: Path | str) -> Iterator[None]:
    """读-改-写全程独占锁（flock，锁文件独立于 JSON 本体）。

    统计派任务可并行，同一台账的「load → upsert → decide → save」必须
    串行；锁文件不随 ``os.replace`` 换体（inode 稳定），进程崩溃自动
    释放（fd 关闭即解锁）。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = target.with_name(target.name + ".lock")
    handle = open(lock_path, "w", encoding="utf-8")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        handle.close()


# ── 观测累积（跨 run）──────────────────────────────────────────────────


def _obs_idem_key(obs: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(str(obs.get(f) or "") for f in _OBS_IDEM_FIELDS)


def _obs_sort_key(obs: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        str(obs.get("window_end") or ""),
        str(obs.get("window_start") or ""),
        str(obs.get("run_id") or ""),
    )


def make_observation(
    *,
    variant_key: str,
    universe: str,
    window_start: str,
    window_end: str,
    run_id: str,
    horizons: Sequence[Mapping[str, Any]],
    best_horizon: int | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """构造一条落账观测（``horizons`` 项 = IncrementalResult.to_dict + horizon）。"""
    if variant_key not in BASELINE_BY_KEY:
        raise ValueError(f"非变体键不可入账：{variant_key!r}")
    return {
        "variant_key": str(variant_key),
        "baseline_key": BASELINE_BY_KEY[variant_key],
        "universe": str(universe),
        "window_start": str(window_start),
        "window_end": str(window_end),
        "run_id": str(run_id),
        "horizons": [dict(h) for h in horizons],
        "best_horizon": int(best_horizon) if best_horizon is not None else None,
        "at": to_utc_iso(now or utc_now()),
    }


def upsert_observations(
    ledger: Mapping[str, Any],
    observations: Sequence[Mapping[str, Any]],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """按幂等键并入观测（同键替换、保尾 24 条）；返回**新** ledger。

    幂等键 = (池, 窗口起, 窗口止, run_id)：同任务重跑/重启续跑覆盖同一条，
    不堆积。观测按 (window_end, window_start, run_id) 升序、裁剪保尾。
    """
    stamp = to_utc_iso(now or utc_now())
    updated = dict(ledger)
    variants: dict[str, Any] = {
        key: dict(entry) for key, entry in (updated.get("variants") or {}).items()
    }
    for raw in observations:
        obs = dict(raw)
        key = str(obs.get("variant_key") or "")
        if key not in BASELINE_BY_KEY:
            raise ValueError(f"非变体键不可入账：{key!r}")
        obs.setdefault("at", stamp)
        obs.setdefault("baseline_key", BASELINE_BY_KEY[key])
        entry = variants.get(key) or {}
        prev = [dict(o) for o in (entry.get("observations") or [])]
        idem = _obs_idem_key(obs)
        for i, old in enumerate(prev):
            if _obs_idem_key(old) == idem:
                prev[i] = obs
                break
        else:
            prev.append(obs)
        prev.sort(key=_obs_sort_key)
        entry["observations"] = prev[-MAX_OBSERVATIONS:]
        variants[key] = entry
    updated["variants"] = variants
    updated["updated_at"] = stamp
    updated.setdefault("schema_version", LEDGER_SCHEMA_VERSION)
    updated.setdefault("cooling", [])
    return updated


# ── 冷却判定（非对称否决）──────────────────────────────────────────────


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed


def _latest_per_universe(entry: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """每池取最新观测（池名排序）——判定按 (变体, 池) 键控：别池**更新**的
    观测不得遮蔽本池（否则交替跑池时空转：A 池失败被 B 池新观测顶掉不判）。"""
    latest: dict[str, Mapping[str, Any]] = {}
    for o in entry.get("observations") or []:
        if not isinstance(o, Mapping):
            continue
        universe = str(o.get("universe") or "")
        cur = latest.get(universe)
        if cur is None or _obs_sort_key(o) > _obs_sort_key(cur):
            latest[universe] = o
    return [latest[u] for u in sorted(latest)]


def _best_horizon_stat(obs: Mapping[str, Any]) -> Mapping[str, Any] | None:
    best = obs.get("best_horizon")
    if best is None:
        return None
    for h in obs.get("horizons") or []:
        if not isinstance(h, Mapping):
            continue
        try:
            if int(h.get("horizon")) == int(best):
                return h
        except (TypeError, ValueError):
            continue
    return None


def _evidence_matches(entry: Mapping[str, Any], obs: Mapping[str, Any]) -> bool:
    """冷却条目证据是否指向同一 (run, 窗口, best 周期) 观测（判过的不再判）。"""
    ev = entry.get("evidence")
    if not isinstance(ev, Mapping):
        return False
    return (
        str(ev.get("run_id") or "") == str(obs.get("run_id") or "")
        and str(ev.get("window_end") or "") == str(obs.get("window_end") or "")
        and str(ev.get("horizon")) == str(obs.get("best_horizon"))
    )


def decide_cooling(
    ledger: Mapping[str, Any], *, now: datetime | None = None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """按最新观测执行非对称否决 → (新 ledger, 动作清单)。

    动作：``cooled``（新入冷/新观测重复失败刷新 TTL）/ ``lifted``（新鲜
    高置信通过解除）。**同一观测只判一次**（``_evidence_matches``）：
    冷却到期后不凭旧观测复活（TTL 语义 = 等新 run 的证据重评）；
    因此同一输入重复调用本函数是幂等的（TTL 不随调用次数漂移）。
    过期条目剪除。只对有父本且在可冷却集的键动作：父本保护（防掐断
    控制链）；判定只看 best_horizon 一格（见模块头注）。判定**每池
    各取最新观测**（(变体, 池) 键控）：别池更新的观测不得遮蔽本池。
    """
    moment = now or utc_now()
    stamp = to_utc_iso(moment)
    updated = dict(ledger)
    coolable = coolable_keys()
    all_entries = [
        dict(e) for e in (ledger.get("cooling") or []) if isinstance(e, Mapping)
    ]
    by_key = {
        (str(e.get("variant_key") or ""), str(e.get("universe") or "")): e
        for e in all_entries
    }
    # 过期剪除（until 解析不了按过期处理——冷却缺席 = 继续评估，数据质量优先）
    cooling = [
        e for e in all_entries if (_parse_iso(e.get("until")) or moment) > moment
    ]
    updates: list[dict[str, Any]] = []

    variants = ledger.get("variants") or {}
    for key in sorted(variants):
        if key not in coolable:
            continue  # 根 / 无母本 / 父本保护：不判不冷
        entry = variants[key]
        if not isinstance(entry, Mapping):
            continue
        for obs in _latest_per_universe(entry):
            stat = _best_horizon_stat(obs)
            if stat is None or stat.get("status") != "ok":
                continue  # 不可验证永不判失败
            ic_ir = stat.get("ic_ir")
            n_days = int(stat.get("n_days") or 0)
            if ic_ir is None or n_days < HIGH_CONF_DAYS:
                continue  # 样本不足不给结论
            ic_ir = float(ic_ir)
            universe = str(obs.get("universe") or "")
            match = by_key.get((key, universe))
            if match is not None and _evidence_matches(match, obs):
                continue  # 该观测已判过（到期条目就此剪除；新 run 才有新判决）
            if ic_ir >= COOL_ICIR_FLOOR:
                if match is not None and match in cooling:
                    cooling.remove(match)
                    updates.append(
                        {
                            "variant_key": key,
                            "universe": universe,
                            "action": "lifted",
                            "residual_ic_ir": ic_ir,
                            "n_days": n_days,
                        }
                    )
                continue
            evidence = {
                "residual_ic_ir": ic_ir,
                "ic_mean": stat.get("ic_mean"),
                "n_days": n_days,
                "horizon": stat.get("horizon"),
                "window_end": obs.get("window_end"),
                "run_id": obs.get("run_id"),
            }
            if match is not None and match in cooling:
                match["until"] = to_utc_iso(moment + timedelta(days=COOLDOWN_DAYS))
                match["evidence"] = evidence
                updates.append(
                    {
                        "variant_key": key,
                        "universe": universe,
                        "action": "cooled",
                        "refreshed": True,
                        "residual_ic_ir": ic_ir,
                    }
                )
            else:
                # 无条目或条目已过期（新观测再次失败）：新/复活冷却，since 从本次起
                revived = match if match is not None else {}
                revived.update(
                    {
                        "variant_key": key,
                        "universe": universe,
                        "baseline_key": BASELINE_BY_KEY[key],
                        "since": stamp,
                        "until": to_utc_iso(moment + timedelta(days=COOLDOWN_DAYS)),
                        "evidence": evidence,
                    }
                )
                cooling.append(revived)
                updates.append(
                    {
                        "variant_key": key,
                        "universe": universe,
                        "action": "cooled",
                        "refreshed": False,
                        "residual_ic_ir": ic_ir,
                    }
                )
    cooling.sort(key=lambda e: (str(e.get("variant_key")), str(e.get("universe"))))
    updated["cooling"] = cooling
    updated["updated_at"] = stamp
    return updated, updates


def active_cooled_keys(
    ledger: Mapping[str, Any], *, universe: str, now: datetime | None = None
) -> frozenset[str]:
    """当前生效（池匹配且未过期）的冷却变体键集。"""
    moment = now or utc_now()
    out: set[str] = set()
    for entry in ledger.get("cooling") or []:
        if not isinstance(entry, Mapping):
            continue
        if str(entry.get("universe") or "") != str(universe):
            continue
        until = _parse_iso(entry.get("until"))
        if until is not None and until > moment:
            key = str(entry.get("variant_key") or "")
            if key in BASELINE_BY_KEY:
                out.add(key)
    return frozenset(out)


# ── 展示骨架（API / 前端共用）──────────────────────────────────────────

#: 展示投影不回的账本内部句柄（``run_id`` = 统计派任务 id）
_DISPLAY_REDACTED_FIELDS = frozenset({"run_id"})


def _display_redact(payload: Mapping[str, Any]) -> dict[str, Any]:
    """展示投影脱敏：账本内部句柄不回传（评审 security-F2，2026-10-12）。

    ``run_id`` 是统计派任务 id——观测与冷却判据里都记着它，但展示面只需要
    窗口/池/读数；句柄出网等于把他人任务 id 泄到前端（跨租户探测面）。
    顶层与 ``evidence`` 子字典两层都过滤；磁盘台账文件保持全量（运维直查
    不受影响），冷却名单本身是池级安全事实、继续平台级共享。
    """
    out = {
        key: value
        for key, value in payload.items()
        if key not in _DISPLAY_REDACTED_FIELDS
    }
    evidence = out.get("evidence")
    if isinstance(evidence, Mapping):
        out["evidence"] = {
            key: value
            for key, value in evidence.items()
            if key not in _DISPLAY_REDACTED_FIELDS
        }
    return out


def ledger_view(
    ledger: Mapping[str, Any],
    *,
    universe: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """台账 → 展示投影：32 骨架行 ×（最新观测 + 冷却态），只读不落盘。

    ``universe`` 给了就按池过滤**观测与冷却**（残差是池内口径，跨池证据
    不可比——看哪个池就只给哪个池的），并给出该池生效集（``cooling_active``
    逐行布尔）；不给则全量原样带出（``cooling_active=None``——池不明时
    「生效」不可判定，前端如实显「—」）。行内 ``latest`` 是最新一条观测
    全量（含逐周期 horizons），``latest_stat`` 是其 best_horizon 那格
    （冷却判定的实际读数）；无观测 = None（前端落「—」，绝不显 0）。

    脱敏：``run_id``（账本内部任务句柄）经 :func:`_display_redact` 在观测与
    冷却判据两层都不回传（评审 security-F2）；排序/归组仍用全量字段，只在
    输出点脱敏。
    """
    moment = now or utc_now()
    pool = str(universe or "")
    variants_state = ledger.get("variants") or {}
    cooling_raw = [
        dict(e) for e in (ledger.get("cooling") or []) if isinstance(e, Mapping)
    ]
    if pool:
        cooling_raw = [e for e in cooling_raw if str(e.get("universe") or "") == pool]
    by_key: dict[str, list[dict[str, Any]]] = {}
    for entry in cooling_raw:
        by_key.setdefault(str(entry.get("variant_key") or ""), []).append(entry)
    active = (
        active_cooled_keys(ledger, universe=pool, now=moment) if pool else frozenset()
    )
    rows: list[dict[str, Any]] = []
    for row in variant_skeleton():
        key = row["key"]
        entry = variants_state.get(key)
        observations: list[dict[str, Any]] = []
        if isinstance(entry, Mapping):
            observations = [
                dict(o)
                for o in (entry.get("observations") or [])
                if isinstance(o, Mapping)
                and (not pool or str(o.get("universe") or "") == pool)
            ]
        latest = max(observations, key=_obs_sort_key) if observations else None
        stat = _best_horizon_stat(latest) if latest is not None else None
        rows.append(
            {
                **row,
                "observations_count": len(observations),
                "latest": _display_redact(latest) if latest is not None else None,
                "latest_stat": dict(stat) if stat is not None else None,
                "cooling": [_display_redact(e) for e in (by_key.get(key) or [])],
                "cooling_active": (key in active) if pool else None,
            }
        )
    return {
        "schema_version": ledger.get("schema_version"),
        "updated_at": ledger.get("updated_at"),
        "universe": pool or None,
        "constants": {
            "cooldown_days": COOLDOWN_DAYS,
            "high_conf_days": HIGH_CONF_DAYS,
            "cool_icir_floor": COOL_ICIR_FLOOR,
            "max_observations": MAX_OBSERVATIONS,
        },
        "cooling": [_display_redact(e) for e in cooling_raw],
        "cooling_active": sorted(active) if pool else None,
        "variants": rows,
    }


def variant_skeleton() -> list[dict[str, Any]]:
    """全候选骨架行（注册表顺序）：展示面按它列 32 行，缺失观测显「—」。

    行字段：``key/name/family/formulation/baseline_key/edit_type/coolable``；
    根与无母本键 ``baseline_key=None``（前端显「—」，不臆造父本）。
    """
    coolable = coolable_keys()
    rows: list[dict[str, Any]] = []
    for candidate in _candidates.CANDIDATES:
        key = candidate.key
        rows.append(
            {
                "key": key,
                "name": candidate.name,
                "family": candidate.family,
                "formulation": candidate.formulation,
                "baseline_key": BASELINE_BY_KEY.get(key),
                "edit_type": EDIT_TYPE_BY_KEY.get(key),
                "coolable": key in coolable,
            }
        )
    return rows


# ── 运维 CLI ───────────────────────────────────────────────────────────


def _cli(argv: Sequence[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="variant_ledger",
        description="变体残差台账运维（T-FE-09）：查看 / 手工解除冷却",
    )
    parser.add_argument("--path", default=None, help="台账路径（默认 env/容器卷）")
    parser.add_argument("--show", action="store_true", help="打印台账摘要")
    parser.add_argument(
        "--reset-cooling",
        metavar="KEY",
        default=None,
        help="手工解除某变体的冷却（全部池；-u 限定单池）",
    )
    parser.add_argument(
        "--universe", "-u", default=None, help="配合 --reset-cooling 限定池"
    )
    args = parser.parse_args(argv)
    path = Path(args.path) if args.path else default_ledger_path()

    if args.reset_cooling:
        key = args.reset_cooling
        if key not in BASELINE_BY_KEY:
            print(f"未知变体键：{key}")
            return 2
        with ledger_lock(path):
            ledger = load_ledger(path)
            before = len(ledger.get("cooling") or [])
            ledger["cooling"] = [
                e
                for e in (ledger.get("cooling") or [])
                if not (
                    str(e.get("variant_key")) == key
                    and (
                        args.universe is None or str(e.get("universe")) == args.universe
                    )
                )
            ]
            removed = before - len(ledger["cooling"])
            save_ledger(path, ledger)
        print(f"已解除 {key} 的冷却条目 {removed} 条（{path}）")
        return 0

    ledger = load_ledger(path)
    variants = ledger.get("variants") or {}
    cooling = ledger.get("cooling") or []
    print(f"台账：{path}（schema v{ledger.get('schema_version')}）")
    print(f"变体观测：{len(variants)} 键 / 冷却中：{len(cooling)} 条")
    for key in sorted(variants):
        obs = (variants[key].get("observations") or [])[-1:]
        if not obs:
            continue
        o = obs[0]
        stat = _best_horizon_stat(o) or {}
        print(
            f"  {key:24s} win_end={o.get('window_end')} "
            f"status={stat.get('status')} icir={stat.get('ic_ir')} "
            f"n_days={stat.get('n_days')} base={o.get('baseline_key')}"
        )
    for entry in cooling:
        print(
            f"  [冷却] {entry.get('variant_key')}@{entry.get('universe')} "
            f"until={entry.get('until')} evidence={entry.get('evidence')}"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_cli())
