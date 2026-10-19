"""统计量价挖掘 runner（移植 P2）——由 launcher 作为子进程调用。

算法 manifest：``backend/services/engine/alpha_agent/algorithms/statistical.yaml``。
与 LLM 派 runner（``run_rd_agent.py``）共用同一信封，launcher 无需分支：

- **入参**：``--task-id --user-id --market --universe --log-dir``，窗口走任务级
  环境变量 ``QM_MINING_WINDOW_*`` / ``QM_VALIDATION_WINDOW_*``（写入方
  ``mining_config.task_env``）；未给窗口时用默认 5 年（4 挖 + 1 检）。
- **出参**：因子库（``rd_agent_factors``，factor_code = **可执行 qlib 表达式**，
  与 LLM 因子同表同链路，回测/物化/验证链路无需改动）；
  ``$LOG_DIR/result.json``（launcher 读的收尾信封）与
  ``$LOG_DIR/mining_report.json``（全量审计：未入选者也带拒因与统计量）。
- **进度**：``$LOG_DIR/progress.jsonl`` 每事件**追加**一行 JSON（阶段/说明/时刻）
  ——launcher 轮询读末行做实时进度、全量行做时间线（覆盖写会丢历史，进度面板
  只剩最后一个阶段）。阶段 key 与 manifest ``stages`` 一一对应。
- **变体台账（T-FE-09）**：开跑前读 ``variant_ledger.json`` 冷却名单传给引擎
  （``--no-cooling`` 绕过读名单，观测照记）；收尾把本次子变体对父本的残差
  观测并入台账并执行非对称否决（flock 读-改-写、原子换体）。台账环节失败
  **只留痕不拦收尾**：``mining_report.json`` 的 ``variant_ledger`` 块记
  ``cooled_applied / observations_written / cooling_updates``，失败记 ``error``。

不做的事（都要写在明面上）：

- **不查名称重**：候选是固定目录（32 个 key 恒定），按名去重会把「同候选的
  新一次实验」全部判重拒掉。统计派的落库身份走 P3 任务指纹（launcher 注入
  ``QM_MINING_TASK_FINGERPRINT``）：``factor_id = md5(statistical:user:market
  :universe:candidate_key:指纹)``——同一份配置重跑**幂等刷新**同一行
  （``save_factor`` 走 ``ON CONFLICT (factor_id) DO UPDATE``），不同配置
  各留一行；缺指纹（手工直跑/旧任务）如实退回任务级身份，metadata 记
  ``identity_scheme`` 可追溯。跨配置的值级查重仍归物化器（离线全库操作）。
- **不跑硬件锁**：``assert_factor_mining_hardware`` 是 LLM 派的 GPU 约束，
  统计派纯 CPU（pandas + qlib 取数），不占用显卡。
- **不挂 h5 物化，但自动验证已挂**：统计派因子表达式是 qlib 原生式，回测/
  验证直接走 T-FB 阶段引擎执行，不需要 RD-Agent 的 h5 物化（那是 functional
  类因子的入口）。落库后自动拉起 T-MV-09 六节验证（``validate_spawn``，
  与 LLM 派同一份实现；``RD_AGENT_AUTO_VALIDATE=false`` 可关）：未附
  ``--h5-path``（无 h5），验证器对 expression 类因子在 h5 阶段（衰减/PIT）
  如实降级标注，T-FB 阶段照常全市场执行。是否拉起写进 ``result.json`` 的
  ``validation_spawned``——自动链失败不阻断挖掘收尾，需手工补跑时看得见。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from collections.abc import Sequence
from datetime import date
from pathlib import Path

_project_root = Path(__file__).resolve().parent.parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("statistical_mining_run")


# ── 窗口 ─────────────────────────────────────────────────────────────


def _data_end() -> date | None:
    """回测可用末端（QuantDB 交易日历最新日）；取不到返回 None（不假装知道）。"""
    import pandas as pd  # noqa: PLC0415

    try:
        from backend.services.engine.data_platform.quantdb_hub import (  # noqa: PLC0415
            QuantDBDataHub,
        )

        cal = QuantDBDataHub.get_instance().fetch_calendar()
        if cal is not None and not cal.empty:
            for col in ("trade_date", "date", "time", "cal_date", "TradingDate"):
                if col in cal.columns:
                    return pd.to_datetime(cal[col]).max().date()
    except Exception as exc:  # noqa: BLE001 - 末端保护是兜底，不是主链路
        logger.warning("交易日历读取失败，不做末端退让：%s", exc)
    return None


def _resolve_windows() -> tuple[object, object | None, str]:
    """任务 env → (挖掘窗, 检验窗, 窗口种类)；未配置则用默认 5 年。"""
    from backend.services.engine.alpha_agent.mining_config import (  # noqa: PLC0415
        default_windows,
        window_from_env,
    )

    data_end = _data_end()
    parsed = window_from_env()
    if parsed is not None:
        mining, validation = parsed
        return (
            mining,
            validation,
            "custom" if validation is not None else "custom_mining_only",
        )
    mining, validation = default_windows(data_end=data_end)
    return mining, validation, "default_4y1y"


# ── regime 月状态（T-FE-03「分市场状态 IC」的标签来源）────────────────


def _load_regime_by_month() -> tuple[dict[str, str] | None, dict]:
    """``YYYY-MM`` → bull/neutral/bear（CN=沪深300，``market_regime`` 单源）。

    取数/口径全部走既有件：``regime_persist.load_index_history`` +
    ``build_state_series`` + ``monthly_states``（月多数票、平票→neutral）。
    **任何失败 → ``(None, source)``**：报告少一列，挖掘主链不因 regime
    数据缺席失败（缺失如实留空，不硬凑）。第二个返回值是降级/就绪的
    审计 source（成功=指数与月份数；失败=错误摘要）——报告里「为什么没有
    这一列」可追溯，不再只有一个 warning 就沉默（评审 M4）。
    """
    try:
        from backend.services.engine.regime_persist import (  # noqa: PLC0415
            load_index_history,
        )
        from backend.shared.market_regime import (  # noqa: PLC0415
            REGIME_INDEX_BY_MARKET,
            STATES,
            build_state_series,
            monthly_states,
        )

        symbol = REGIME_INDEX_BY_MARKET["CN"]
        history = load_index_history("CN", symbol)
        if len(history["dates"]) < 2:
            raise ValueError(f"regime 指数 {symbol} 无日线历史")
        states = build_state_series(
            history["closes"], history["volumes"], history["dates"]
        )
        by_month = monthly_states(states)
        if not by_month:
            raise ValueError("索引历史不足一个稳态窗口，月状态为空")
        dist = {s: sum(1 for v in by_month.values() if v == s) for s in STATES}
        logger.info("regime 月状态就绪：%d 个月（%s）%s", len(by_month), symbol, dist)
        return by_month, {"market": "CN", "index": symbol, "months": len(by_month)}
    except Exception as exc:  # noqa: BLE001 - 报告证据降级，不拖垮挖掘主链
        logger.warning(
            "regime 月状态不可用——「分市场状态 IC」列将留空：%s", exc, exc_info=True
        )
        return None, {"market": "CN", "error": str(exc)[:200]}


# ── 落库 ─────────────────────────────────────────────────────────────


def _trade_metadata_fields(report) -> dict:
    """T-FE-03 交易性证据 → metadata 字段（单候选与合成共用同一形状）。

    ``regime_ic`` 空元组 → None（缺时间线不是「三个空桶」）；桶内 NaN
    经 ``ser.json_safe`` 落 null（缺失一律 null 的同一口径）。
    """
    return {
        "factor_autocorr": report.factor_autocorr,
        "factor_autocorr_days": report.factor_autocorr_days,
        "rebalance_days": report.rebalance_days,
        "turnover_daily": report.turnover_daily,
        "ann_turnover": report.ann_turnover,
        "ic_net_mean": report.ic_net_mean,
        "ic_net_days": report.ic_net_days,
        "regime_ic": (
            {
                r.state: {
                    "n_days": r.n_days,
                    "rank_ic_mean": r.rank_ic_mean,
                    "weak": r.weak,
                }
                for r in report.regime_ic
            }
            or None
        ),
    }


def _factor_metadata(
    report,
    *,
    task_id: str,
    market: str,
    audit: dict,
    task_fingerprint: str = "",
    identity_scheme: str = "",
) -> dict:
    """入因子库的 metadata：口径可追溯（窗口/中性化/过滤/统计量全带上）。

    必须经 ``json_safe`` 出门：被闸门拒掉的周期 IC 是 NaN，而 Postgres
    ``json`` 列拒收 NaN（实测：五条入选因子全量落库失败）。
    """
    from backend.services.engine.alpha_agent.statistical import (  # noqa: PLC0415
        serialize as ser,
    )

    horizons = {
        str(h.horizon): {
            "n_days": h.n_days,
            "ic_mean": h.ic_mean,
            "rank_ic_mean": h.rank_ic_mean,
            "icir": h.icir,
            "rank_ic_t": h.rank_ic_t,
            "rank_ic_p": h.rank_ic_p,
            "rank_ic_t_naive": h.rank_ic_t_naive,
            "rank_ic_t_lags": h.rank_ic_t_lags,
            "fdr_pass": h.fdr_pass,
        }
        for h in report.horizons
    }
    return ser.json_safe(
        {
            "source": "statistical",
            "algorithm": "statistical",
            "market": market,
            "task_id": task_id,
            "task_fingerprint": task_fingerprint or None,
            "identity_scheme": identity_scheme or None,
            "candidate_key": report.key,
            "family": report.family,
            "literature": report.source,
            "min_history": report.min_history,
            "best_horizon": report.best_horizon,
            "horizons": horizons,
            "rank_icir": report.icir,
            "fdr_pass": report.fdr_pass,
            "fdr_horizons": list(report.fdr_horizons),
            "oos_verified": report.oos_verified,
            "oos_horizon": report.oos_horizon,
            "oos_rank_ic": report.oos_rank_ic,
            "quantile_monotonicity": report.quantile_monotonicity,
            "spread_mean": report.spread_mean,
            # Tier 3 证据（不可判定 → null，与展示面「缺失一律 —」同口径）
            "fold_ics": list(report.fold_ics),
            "fold_sign_consistency": report.fold_sign_consistency,
            "selection_score": report.selection_score,
            "reselect_rate": report.reselect_rate,
            "reselect_hits": report.reselect_hits,
            "reselect_checkpoints": report.reselect_checkpoints,
            "ic_half_life": report.ic_half_life,
            "ic_decay_slope": report.ic_decay_slope,
            "max_abs_corr": report.max_abs_corr,
            "corr_warn": report.corr_warn or None,
            # T-FE-05 结构复杂度计数（选择分里简单性因子的可复算输入；
            # 口径单源 statistical/complexity.py）
            "op_count": report.op_count,
            "literal_count": report.literal_count,
            # T-FE-03 交易性证据（自相关→调仓建议 / 换手·扣费净 IC / 分状态 IC）
            **_trade_metadata_fields(report),
            "window": audit.get("window"),
            "neutralize_mode": audit.get("neutralize_mode"),
            "filter": audit.get("filter"),
            # 审计自带口径：这份 IC 是在哪个池子上算的（请求与实跑同一值，
            # 引擎层已保证）——落库行 universe 列与 metadata 不打架
            "universe": audit.get("universe"),
        }
    )


def _composite_metadata(
    report,
    *,
    task_id: str,
    market: str,
    audit: dict,
    task_fingerprint: str = "",
    identity_scheme: str = "",
) -> dict:
    """合成因子 metadata：分量/权重/口径全带，两条「刻意不做」写在明面上。

    ``fdr_applied=False`` / ``neutralized=False`` 是报告与落库表达式的等价
    前提：合成因子不属于原始候选池（不进 BH-FDR），中性化回归在单行 qlib
    表达式中复现不了（不做）——算了它，回测链路也复现不出报告数字。
    """
    from backend.services.engine.alpha_agent.statistical import (  # noqa: PLC0415
        serialize as ser,
    )

    horizons = {
        str(h.horizon): {
            "n_days": h.n_days,
            "ic_mean": h.ic_mean,
            "rank_ic_mean": h.rank_ic_mean,
            "icir": h.icir,
            "rank_ic_t": h.rank_ic_t,
            "rank_ic_p": h.rank_ic_p,
            "rank_ic_t_naive": h.rank_ic_t_naive,
            "rank_ic_t_lags": h.rank_ic_t_lags,
        }
        for h in report.horizons
    }
    return ser.json_safe(
        {
            "source": "statistical",
            "algorithm": "statistical",
            "market": market,
            "task_id": task_id,
            "task_fingerprint": task_fingerprint or None,
            "identity_scheme": identity_scheme or None,
            "candidate_key": report.key,
            "family": "composite",
            "literature": "Tier 3：入选因子按目标周期 RankIC 加权合成",
            "composite": True,
            "components": list(report.components),
            "weights": list(report.weights),
            "component_rank_ics": list(report.component_rank_ics),
            "weight_horizon": report.horizon,
            "z_window": report.z_window,
            "fdr_applied": False,
            "neutralized": False,
            "min_history": report.min_history,
            "best_horizon": report.best_horizon,
            "horizons": horizons,
            "rank_icir": report.icir,
            "oos_verified": report.oos_verified,
            "oos_horizon": report.oos_horizon,
            "oos_rank_ic": report.oos_rank_ic,
            "quantile_monotonicity": report.quantile_monotonicity,
            "spread_mean": report.spread_mean,
            # T-FE-03 交易性证据（合成与单候选同一份形状）
            **_trade_metadata_fields(report),
            "window": audit.get("window"),
            "neutralize_mode": audit.get("neutralize_mode"),
            "filter": audit.get("filter"),
            "universe": audit.get("universe"),
        }
    )


async def _persist_selected(
    result,
    *,
    task_id: str,
    user_id: str,
    market: str,
    universe: str,
    audit: dict,
    task_fingerprint: str = "",
) -> tuple[int, int]:
    """入选因子 + 合成因子落库；返回 ``(入选落库数, 合成落库数)``。

    factor_code = qlib 表达式（与 LLM 因子同链路）；身份走稳定因子 id
    （P3 指纹优先，见模块 docstring）。合成因子的样本外闸不过即**只记账
    不落库**（闸的实际作用），日志写明拒因。
    """
    from backend.services.engine.alpha_agent.statistical import (  # noqa: PLC0415
        dedup as dedup_mod,
    )
    from backend.services.engine.qlib_app.services.rd_agent_persistence import (  # noqa: PLC0415
        RDAgentFactorPersistence,
    )

    persistence = RDAgentFactorPersistence()
    await persistence.ensure_tables()
    date_range = f"{audit['window']['mining_start']}~{audit['window']['mining_end']}"

    async def _save_one(
        factor_id: str, report, *, metadata: dict, formulation: str
    ) -> None:
        """落一行因子 + 指标（失败向上抛，由调用方记账）。"""
        await persistence.save_factor(
            factor_id=factor_id,
            factor_name=f"{report.name}（{report.key}）",
            factor_code=report.expression,
            user_id=user_id,
            metadata=metadata,
            market=market,
            universe=universe,
            factor_formulation=formulation,
            data_source="statistical",
        )
        await persistence.update_factor_metrics(
            factor_id=factor_id,
            status="completed",
            ic_value=report.horizons[0].ic_mean if report.horizons else None,
            rank_ic=report.rank_ic_mean,
            icir=report.icir,
            rank_icir=report.icir,
            universe=universe,
            date_range=date_range,
            metadata=metadata,
        )

    def _identity(factor_key: str) -> tuple[str, str]:
        return dedup_mod.stable_factor_id(
            user_id=user_id,
            market=market,
            universe=universe,
            candidate_key=factor_key,
            task_fingerprint=task_fingerprint,
            task_id=task_id,
        )

    saved = 0
    for report in [r for r in result.candidates if r.selected]:
        factor_id, scheme = _identity(report.key)
        metadata = _factor_metadata(
            report,
            task_id=task_id,
            market=market,
            audit=audit,
            task_fingerprint=task_fingerprint,
            identity_scheme=scheme,
        )
        try:
            await _save_one(
                factor_id, report, metadata=metadata, formulation=report.formulation
            )
            saved += 1
            logger.info(
                "落库 %s（%s，身份=%s）：RankIC=%.4f 周期=%s τ=%.2f",
                report.name,
                report.key,
                scheme,
                report.rank_ic_mean,
                report.best_horizon,
                report.quantile_monotonicity,
            )
        except Exception as exc:  # noqa: BLE001 - 单因子失败不拖垮整批
            logger.warning("落库失败 %s：%s", report.key, exc)

    saved_composite = 0
    composite = result.composite
    if composite is None:
        return saved, saved_composite
    if not composite.eligible:
        logger.info(
            "合成因子不落库（%s）：%s",
            composite.key,
            composite.reject_reason or "不适格",
        )
        return saved, saved_composite
    factor_id, scheme = _identity(composite.key)
    metadata = _composite_metadata(
        composite,
        task_id=task_id,
        market=market,
        audit=audit,
        task_fingerprint=task_fingerprint,
        identity_scheme=scheme,
    )
    try:
        await _save_one(
            factor_id,
            composite,
            metadata=metadata,
            formulation=(
                f"Σ w_i·zscore(f_i,{composite.z_window})，"
                f"w = RankIC({composite.horizon}) 归一"
            ),
        )
        saved_composite += 1
        logger.info(
            "落库合成因子（%s，身份=%s）：分量 %d 个，RankIC=%.4f（h*=%d）",
            composite.key,
            scheme,
            len(composite.components),
            composite.rank_ic_mean,
            composite.horizon,
        )
    except Exception as exc:  # noqa: BLE001 - 合成失败不拖垮整批
        logger.warning("落库失败 %s（合成）：%s", composite.key, exc)
    return saved, saved_composite


def _spawn_validation_and_write_result(
    *,
    task_id: str,
    market: str,
    log_dir: Path,
    persisted_total: int,
    payload: dict,
    logger: logging.Logger,
) -> bool:
    """落库后自动验证（T-MV-09）+ ``result.json`` 落盘（main 尾部的薄切片）。

    ``payload`` 是 result.json 的其余字段；本函数补 ``validation_spawned`` 并
    写盘（json_safe 兜底 NaN），返回是否真的拉起验证子进程——单独成函数是为了
    接线可测（误传 ``persisted``/漏写字段要被测试拦住，而不是靠人看）。
    """
    from backend.services.engine.alpha_agent.statistical import (  # noqa: PLC0415
        serialize as ser,
    )
    from backend.services.engine.alpha_agent.validate_spawn import (  # noqa: PLC0415
        spawn_validation,
    )

    validation_spawned = spawn_validation(
        task_id=task_id,
        market=market,
        log_dir=log_dir,
        persisted=persisted_total,
        log=logger,
    )
    (log_dir / "result.json").write_text(
        json.dumps(
            # json_safe 兜底：dedup/composite 段带 NaN 语义（缺失 = null），
            # 裸 NaN 会让下游严格 JSON 解析器整份读不出来
            ser.json_safe({**payload, "validation_spawned": validation_spawned}),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return validation_spawned


# ── 变体台账（T-FE-09）───────────────────────────────────────────────


def _update_variant_ledger(
    result,
    *,
    universe: str,
    run_id: str,
    cooled_applied: Sequence[str] = (),
    ledger_path: Path | None = None,
) -> dict:
    """收尾：本 run 的子变体残差观测并入台账 + 非对称否决；返回报告块。

    只有**有父本的子变体**（``variant_of`` 非空）入账——残差是引擎在环内
    同窗同池同管线算好的，这里原样搬运，不重算。写失败**不拦收尾**：台账
    是审计面，挖掘主链（落库/验证）已完成，失败如实记进 ``error`` 块。
    零观测（全被冷却/无子变体）不触台账文件。
    """
    from backend.services.engine.alpha_agent.statistical import (  # noqa: PLC0415
        variant_ledger as vl,
    )

    path = Path(ledger_path) if ledger_path is not None else vl.default_ledger_path()
    block: dict = {
        "schema_version": vl.LEDGER_SCHEMA_VERSION,
        "ledger_path": str(path),
        "cooled_applied": sorted(str(k) for k in cooled_applied),
        "observations_written": 0,
        "cooling_updates": [],
    }
    window = dict(result.window or {})
    try:
        observations = [
            vl.make_observation(
                variant_key=report.key,
                universe=result.universe or universe,
                window_start=window.get("mining_start") or "",
                window_end=window.get("mining_end") or "",
                run_id=run_id,
                horizons=report.variant_residual or (),
                best_horizon=report.best_horizon,
            )
            for report in result.candidates
            if getattr(report, "variant_of", None) is not None
        ]
        if not observations:
            return block
        with vl.ledger_lock(path):
            ledger = vl.load_ledger(path)
            ledger = vl.upsert_observations(ledger, observations)
            ledger, updates = vl.decide_cooling(ledger)
            vl.save_ledger(path, ledger)
        block["observations_written"] = len(observations)
        block["cooling_updates"] = updates
    except Exception as exc:  # noqa: BLE001 - 台账失败绝不拦收尾
        logger.warning("变体台账更新失败（不拦收尾）：%s", exc, exc_info=True)
        block["error"] = str(exc)[:200]
    return block


# ── 主流程 ───────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="QuantMind 统计量价挖掘 Runner")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--market", default="a_share")
    parser.add_argument("--universe", default="all_a")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--log-dir", default="")
    parser.add_argument(
        "--no-cooling",
        action="store_true",
        help="本轮忽略变体台账冷却名单（观测照记，仍参与冷却判定）",
    )
    args = parser.parse_args()

    log_dir = Path(
        args.log_dir or os.getenv("LOG_TRACE_PATH", "/tmp/statistical_logs")
    ).resolve()
    log_dir.mkdir(parents=True, exist_ok=True)

    logger.info("=" * 60)
    logger.info("统计量价挖掘 starting")
    logger.info("  Task ID:  %s", args.task_id)
    logger.info("  User ID:  %s", args.user_id)
    logger.info("  Market:   %s", args.market)
    logger.info("  Top-K:    %d", args.top_k)
    logger.info("  Log dir:  %s", log_dir)
    logger.info("=" * 60)

    if args.market != "a_share":
        logger.error("统计派只支持 A 股（收到 market=%s）", args.market)
        raise SystemExit(2)

    from backend.services.engine.alpha_agent.mining_config import (  # noqa: PLC0415
        task_fingerprint_from_env,
    )
    from backend.services.engine.alpha_agent.statistical import engine as eng  # noqa: PLC0415
    from backend.services.engine.alpha_agent.statistical import serialize as ser  # noqa: PLC0415
    from backend.services.engine.alpha_agent.statistical import (  # noqa: PLC0415
        variant_ledger as vl,
    )

    # P3 指纹（launcher 注入）：决定落库因子 id 的身份口径；空 = 任务级兜底
    task_fingerprint = task_fingerprint_from_env()
    logger.info(
        "任务指纹：%s",
        task_fingerprint
        or "(缺省——落库身份退回任务级，metadata 记 identity_scheme=task)",
    )

    mining, validation, window_kind = _resolve_windows()
    mining_start, mining_end = mining.iso()
    valid_start, valid_end = (
        validation.iso() if validation is not None else (None, None)
    )
    logger.info(
        "窗口（%s）：挖掘 %s~%s；检验 %s",
        window_kind,
        mining_start,
        mining_end,
        f"{valid_start}~{valid_end}" if valid_start else "(无)",
    )

    progress_file = log_dir / "progress.jsonl"

    def emit(stage: str, detail: str) -> None:
        """进度事件**追加**一行 JSONL；写进度失败绝不拖垮挖掘（仅记日志）。"""
        logger.info("[%s] %s", stage, detail)
        event = {
            "stage": stage,
            "detail": detail,
            "ts": time.time(),
            "task_id": args.task_id,
        }
        try:
            with open(progress_file, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError as exc:  # noqa: BLE001 - 进度面不是主链
            logger.warning("进度写入失败：%s", exc)

    # T-FE-09：变体台账冷却名单（前置读取；只读不加锁——save 走原子换体，
    # 读者永不见半截 JSON）。读失败 = 本轮无冷却，不拦开跑。
    ledger_path = vl.default_ledger_path()
    cooled_keys: frozenset[str] = frozenset()
    if args.no_cooling:
        logger.info("--no-cooling：本轮忽略台账冷却名单（观测照记）")
    else:
        try:
            cooled_keys = vl.active_cooled_keys(
                vl.load_ledger(ledger_path), universe=args.universe
            )
        except Exception as exc:  # noqa: BLE001 - 读失败不拦开跑
            logger.warning("变体台账读取失败（本轮无冷却）：%s", exc)
    if cooled_keys:
        logger.info(
            "冷却名单（%s，%d 键）：%s",
            args.universe,
            len(cooled_keys),
            "、".join(sorted(cooled_keys)),
        )

    # T-FE-03：regime 月状态（取不到 → None，报告少一列不拖垮主链；
    # regime_source 是降级/就绪的可追溯痕迹，落进审计报告）
    regime_by_month, regime_source = _load_regime_by_month()

    t0 = time.time()
    try:
        result = eng.run_statistical_mining(
            mining_start,
            mining_end,
            valid_start=valid_start,
            valid_end=valid_end,
            cfg=eng.StatisticalConfig(top_k=args.top_k),
            universe=args.universe,
            progress=emit,
            regime_by_month=regime_by_month,
            cooled_keys=cooled_keys,
        )
    except Exception as exc:  # noqa: BLE001 - 子进程出口：日志 + 非零码
        logger.exception("统计量价挖掘失败：%s", exc)
        raise SystemExit(1) from exc
    elapsed = time.time() - t0

    audit = ser.result_to_dict(result)
    audit["window_kind"] = window_kind
    audit["regime_source"] = regime_source
    # T-FE-09：子变体残差观测并入台账 + 非对称否决（失败只留痕，不拦收尾；
    # 名单实际生效集以引擎回传的 result.cooled_keys 为准）
    audit["variant_ledger"] = _update_variant_ledger(
        result,
        universe=args.universe,
        run_id=args.task_id,
        cooled_applied=result.cooled_keys,
        ledger_path=ledger_path,
    )
    ledger_block = audit["variant_ledger"]
    if ledger_block.get("error"):
        logger.warning("变体台账未更新：%s", ledger_block["error"])
    else:
        logger.info(
            "变体台账（%s）：观测 %d 条入账，冷却动作 %d 次",
            ledger_path,
            ledger_block["observations_written"],
            len(ledger_block["cooling_updates"]),
        )
    # 落盘也用 json_safe：裸 NaN 是 Python 方言，JSON.parse / Postgres 都不认
    (log_dir / "mining_report.json").write_text(
        json.dumps(ser.json_safe(audit), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info("审计报告：%s", log_dir / "mining_report.json")

    persisted, persisted_composite = asyncio.run(
        _persist_selected(
            result,
            task_id=args.task_id,
            user_id=args.user_id,
            market=args.market,
            universe=args.universe,
            audit=audit,
            task_fingerprint=task_fingerprint,
        )
    )
    rejected = [r for r in result.candidates if not r.selected]
    logger.info(
        "候选 %d：入选 %d，落库 %d，合成落库 %d",
        len(result.candidates),
        len(result.selected),
        persisted,
        persisted_composite,
    )
    for report in rejected:
        logger.info("  未入选 %s：%s", report.key, report.reject_reason)

    # 落库后自动验证（T-MV-09 六节报告）——后台子进程，不阻塞收尾；无 h5，
    # 验证器对 expression 类因子在 h5 阶段如实降级（口径唯一实现见 validate_spawn）
    _spawn_validation_and_write_result(
        task_id=args.task_id,
        market=args.market,
        log_dir=log_dir,
        persisted_total=persisted + persisted_composite,
        payload={
            "task_id": args.task_id,
            "algorithm": "statistical",
            "market": args.market,
            # 与审计报告同一口径：引擎真跑的池子（= 任务请求的 universe）
            "universe": audit.get("universe") or args.universe,
            "window": audit["window"],
            "window_kind": window_kind,
            "total_candidates": len(result.candidates),
            "selected_factors": len(result.selected),
            "persisted_factors": persisted,
            "persisted_composite": persisted_composite,
            "composite": audit.get("composite"),
            "dedup": audit.get("dedup"),
            "elapsed_seconds": elapsed,
        },
        logger=logger,
    )
    logger.info("=" * 60)
    logger.info("统计量价挖掘完成！task_id=%s，用时 %.1fs", args.task_id, elapsed)
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
