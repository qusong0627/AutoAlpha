"""内置物化门禁（六个，全部默认 soft；阈值可经 yaml/env 热调）。

判定语义统一走 ``_ThresholdGate``：
- min 型：``value ≥ threshold`` 通过；max 型：``value ≤ threshold`` 通过；
- 观测值缺失（None）→ ``skipped``，消息说明原因——**不判不拦**，硬模式
  也不拦（缺失 = 口径没算过，不是算出来很差）；
- fail 消息带 4 位小数的观测与阈值（物化 manifest / 前端徽标直接展示）。

| key | 观测 | 方向 | 默认阈值 | 出处 |
|---|---|---|---|---|
| pfs_floor        | quality.pfs      | ≥ | 0.90 | 扰动保真度（alpha_agent._compute_pfs_quality） |
| rre_floor        | rre              | ≥ | 0.50 | 时序排序稳定性（reliability 评估器） |
| ic_pool_pct      | 池内 IC 分位     | ≥ | 0.30 | 池内相对位置（池刷新时算） |
| turnover_cap     | ann_turnover     | ≤ | 60.0 | 年化换手（turnover_cost 评估器） |
| net_return_floor | ann_return_net   | ≥ | 0.00 | 扣费年化收益（turnover_cost 评估器） |
| incremental_ic_ir_floor | 残差 IC_IR | ≥ | 0.30 | 对库冗余基施密特正交后的独立信息（statistical.incremental，T-FE-01） |

升级路径：yaml ``gates.<key>.mode: hard`` 或 env ``QM_MINING_GATES_MODE=strict``；
关闭：``QM_MINING_GATES_DISABLED=<key>[,<key>]`` 或 yaml ``enabled: false``。
"""

from __future__ import annotations

from ..base import GateContext, GateDescriptor, GateOutcome
from ..registry import register_gate

#: 缺失原因统一话术（skipped 消息尾缀）
_MISSING_NOTE = "缺失（未回测或旧口径），不判不拦"


class _ThresholdGate:
    """阈值型门禁基类：子类给 name/descriptor/阈值/观测提取即可注册。"""

    name: str
    descriptor: GateDescriptor
    default_threshold: float
    metric_label: str  # 消息里的短名
    direction: str = "min"  # min | max
    reason: str = ""  # fail 时的原因尾缀
    _missing_note: str = _MISSING_NOTE

    def _observed(self, ctx: GateContext) -> float | None:
        raise NotImplementedError

    def check(self, ctx: GateContext, threshold: float) -> GateOutcome:
        value = self._observed(ctx)
        if value is None:
            return GateOutcome(
                key=self.name,
                label=self.descriptor.label,
                status="skipped",
                message=f"{self.metric_label} {self._missing_note}",
                threshold=threshold,
            )
        value = float(value)
        if self.direction == "min":
            ok = value >= threshold
            relation = "≥" if ok else "<"
        else:
            ok = value <= threshold
            relation = "≤" if ok else ">"
        message = f"{self.metric_label}={value:.4f} {relation} {threshold:.4f}"
        if not ok and self.reason:
            message += f"（{self.reason}）"
        return GateOutcome(
            key=self.name,
            label=self.descriptor.label,
            status="pass" if ok else "fail",
            message=message,
            observed=value,
            threshold=threshold,
        )


class _PfsFloor(_ThresholdGate):
    name = "pfs_floor"
    descriptor = GateDescriptor(
        key="pfs_floor",
        label="PFS 下限",
        default_mode="soft",
        description="扰动保真度（PFS）低于阈值的因子进入池前仅告警",
    )
    default_threshold = 0.9
    metric_label = "PFS"
    reason = "扰动保真度不足"

    def _observed(self, ctx: GateContext) -> float | None:
        value = ctx.metrics.get("pfs")
        return None if value is None else float(value)


class _RreFloor(_ThresholdGate):
    name = "rre_floor"
    descriptor = GateDescriptor(
        key="rre_floor",
        label="RRE 下限",
        default_mode="soft",
        description="时序排序稳定性（RRE）低于阈值的因子进入池前仅告警",
    )
    default_threshold = 0.5
    metric_label = "RRE"
    reason = "排序结构日间漂移过大"

    def _observed(self, ctx: GateContext) -> float | None:
        value = ctx.metrics.get("rre")
        return None if value is None else float(value)


class _IcPoolPct(_ThresholdGate):
    name = "ic_pool_pct"
    descriptor = GateDescriptor(
        key="ic_pool_pct",
        label="池内 IC 分位下限",
        default_mode="soft",
        description="IC 在池内分位过低（相对位置靠后）的因子进入池前仅告警",
    )
    default_threshold = 0.3
    metric_label = "IC 池内分位"
    reason = "池内相对位置过靠后"
    _missing_note = "不可得（无池或池太小），不判不拦"

    def _observed(self, ctx: GateContext) -> float | None:
        return None if ctx.pool_ic_pct is None else float(ctx.pool_ic_pct)


class _TurnoverCap(_ThresholdGate):
    name = "turnover_cap"
    descriptor = GateDescriptor(
        key="turnover_cap",
        label="年化换手上限",
        default_mode="soft",
        description="年化换手超过阈值的因子进入池前仅告警（成本侵蚀提示）",
    )
    default_threshold = 60.0
    metric_label = "年化换手"
    direction = "max"
    reason = "换手过高、成本侵蚀严重"

    def _observed(self, ctx: GateContext) -> float | None:
        value = ctx.metrics.get("ann_turnover")
        return None if value is None else float(value)


class _NetReturnFloor(_ThresholdGate):
    name = "net_return_floor"
    descriptor = GateDescriptor(
        key="net_return_floor",
        label="扣费年化收益下限",
        default_mode="soft",
        description="扣成本后年化收益低于阈值的因子进入池前仅告警",
    )
    default_threshold = 0.0
    metric_label = "扣费年化"
    reason = "扣成本后无收益"

    def _observed(self, ctx: GateContext) -> float | None:
        value = ctx.metrics.get("ann_return_net")
        return None if value is None else float(value)


class _IncrementalIcIrFloor(_ThresholdGate):
    """增量正交闸（T-FE-01）：残差 IC_IR 下限。

    观测来自物化期新算的 ``statistical.incremental.incremental_ic_ir``
    （对库 |ρ|≥0.3 的对照列施密特正交 → 残差对前向收益的 IC_IR）。
    **default_mode=soft 是刻意口径**：ICIR 族阈值默认不设硬拦（分母是
    族常数、硬拦会批量拒绝），台账字面「>0.3 才入库」的硬语义由用户经
    ``gates.incremental_ic_ir_floor.mode: hard`` 一行显式开启。

    三态判定（非 None 二分）：
    - ``incremental_basis_n`` 缺失 → skipped（旧条目未算过，不判不拦）；
    - ``basis_n == 0``（无 |ρ|≥0.3 的库列）→ **pass**——无冗余基，
      残差即原值，没有「独立信息不足」可言；
    - ``basis_n > 0`` 但 ic_ir 缺失（残差 IC 有效窗口不足/不可算）→
      skipped——不可验证 ≠ 差；
    - 否则二元比较。``--gates-only`` 重评时由 manifest 保存的 metrics
      回放（增量指标依赖因子值，重评无法现算）。
    """

    name = "incremental_ic_ir_floor"
    descriptor = GateDescriptor(
        key="incremental_ic_ir_floor",
        label="增量残差 IC_IR 下限",
        default_mode="soft",
        description=(
            "对库冗余（|ρ|≥0.3 对照列施密特正交）后残差 IC_IR 过低的因子"
            "进入池前仅告警——守住「库里已有的信息」不重复入库"
        ),
    )
    default_threshold = 0.3
    metric_label = "增量残差 IC_IR"
    reason = "对库冗余、独立信息不足"

    def check(self, ctx: GateContext, threshold: float) -> GateOutcome:
        basis_n = ctx.metrics.get("incremental_basis_n")
        if basis_n is None:
            return GateOutcome(
                key=self.name,
                label=self.descriptor.label,
                status="skipped",
                message=f"{self.metric_label} {self._missing_note}",
                threshold=threshold,
            )
        basis_n = int(basis_n)
        if basis_n == 0:
            return GateOutcome(
                key=self.name,
                label=self.descriptor.label,
                status="pass",
                message="无 |ρ|≥0.3 的对照列（无冗余基），视为通过",
                threshold=threshold,
            )
        value = ctx.metrics.get("incremental_ic_ir")
        if value is None:
            return GateOutcome(
                key=self.name,
                label=self.descriptor.label,
                status="skipped",
                message=(
                    f"{self.metric_label} 不可验证（对照 {basis_n} 列、"
                    "残差有效窗口不足），不判不拦"
                ),
                threshold=threshold,
            )
        value = float(value)
        ok = value >= threshold
        message = (
            f"{self.metric_label}={value:.4f} {'≥' if ok else '<'} {threshold:.4f}"
        )
        if not ok:
            message += f"（{self.reason}；对照 {basis_n} 列）"
        return GateOutcome(
            key=self.name,
            label=self.descriptor.label,
            status="pass" if ok else "fail",
            message=message,
            observed=value,
            threshold=threshold,
        )


_BUILTIN_GATES = (
    _PfsFloor,
    _RreFloor,
    _IcPoolPct,
    _TurnoverCap,
    _NetReturnFloor,
    _IncrementalIcIrFloor,
)


def _register_builtin_gates() -> list[str]:
    names: list[str] = []
    for gate_cls in _BUILTIN_GATES:
        register_gate(gate_cls())
        names.append(gate_cls.name)
    return names


_BUILTIN_GATE_NAMES = _register_builtin_gates()
