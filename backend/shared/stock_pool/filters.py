"""全局股票池 - 信号过滤（消费方共用）。

推理 / 模拟盘 / 实盘都需要「把信号裁到池内」。三处各写一遍必然漂移，
因此统一放这里。

**严格语义**（与既有「单股补推」的宽松兜底刻意区分开）：

- 池非空 → 只保留命中的信号；一条都没命中时返回空结果并给出 `empty_result` 标记，
  由调用方决定失败策略（推理链路应显式失败，不能静默退化为全市场）。
- 池为空（`snapshot.is_empty`）→ 视为异常输入，返回空结果 + 告警，
  **不会**被当作「不过滤」。
- `snapshot.unfiltered` → 明确的「不过滤」，原样返回。

对照：`script_runner.execute(symbols=...)` 的单股补推语义是「未命中则保留全量」，
那是为了让个股页能立即看到分数，属于有意为之；池过滤不能沿用那个兜底。
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from backend.shared.stock_utils import StockCodeUtil

logger = logging.getLogger(__name__)


@dataclass
class PoolFilterOutcome:
    """过滤结果与可观测信息。"""

    kept: list[dict] = field(default_factory=list)
    total_in: int = 0
    dropped: int = 0
    unfiltered: bool = False
    empty_pool: bool = False
    empty_result: bool = False
    pool_id: str | None = None
    pool_checksum: str | None = None
    pool_symbol_count: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def applied(self) -> bool:
        """是否真的做了裁剪。"""
        return not self.unfiltered and not self.empty_pool

    def as_dict(self) -> dict[str, Any]:
        return {
            "pool_id": self.pool_id,
            "pool_checksum": self.pool_checksum,
            "pool_symbol_count": self.pool_symbol_count,
            "total_in": self.total_in,
            "kept": len(self.kept),
            "dropped": self.dropped,
            "unfiltered": self.unfiltered,
            "empty_pool": self.empty_pool,
            "empty_result": self.empty_result,
            "warnings": self.warnings,
        }


def _to_prefix(symbol: Any) -> str:
    raw = str(symbol or "").strip()
    if not raw:
        return ""
    try:
        return StockCodeUtil.to_prefix(raw)
    except Exception:  # noqa: BLE001
        return raw


def filter_signals_by_pool(
    signals: Sequence[dict] | None,
    snapshot,
    *,
    symbol_key: str = "symbol",
    strict: bool = True,
) -> PoolFilterOutcome:
    """按 `PoolSnapshot` 裁剪信号列表。

    Args:
        signals: 形如 `[{"symbol": "SH600036", "score": 1.2}, ...]`
        snapshot: `PoolResolver` 的解析结果
        symbol_key: 信号里的代码字段名
        strict: True 时空结果只是标记 `empty_result`（由调用方决定是否抛错）；
                False 时同样标记，不抛异常 —— 本函数**从不抛异常**，
                失败策略一律交给调用方，避免在深层链路里冒泡出难定位的错误。

    Returns:
        `PoolFilterOutcome`；`kept` 为保留的信号（保序）。
    """
    items = list(signals or [])
    outcome = PoolFilterOutcome(total_in=len(items))

    if snapshot is None:
        outcome.warnings.append("未提供股票池快照，未做过滤")
        outcome.kept = items
        outcome.unfiltered = True
        return outcome

    outcome.pool_id = getattr(snapshot, "pool_id", None)
    outcome.pool_checksum = getattr(snapshot, "checksum", None)
    outcome.warnings = list(getattr(snapshot, "warnings", None) or [])

    if getattr(snapshot, "unfiltered", False):
        outcome.unfiltered = True
        outcome.kept = items
        return outcome

    allowed = {_to_prefix(s) for s in (getattr(snapshot, "api_symbols", None) or [])}
    allowed.discard("")
    outcome.pool_symbol_count = len(allowed)

    if not allowed:
        outcome.empty_pool = True
        outcome.warnings.append(
            f"股票池 {outcome.pool_id or ''} 解析为空池，"
            "拒绝按『不过滤』处理（调用方应显式失败）"
        )
        logger.warning(
            "池过滤：池为空 pool_id=%s warnings=%s",
            outcome.pool_id,
            outcome.warnings,
        )
        return outcome

    kept: list[dict] = []
    dropped = 0
    for sig in items:
        if _to_prefix(sig.get(symbol_key)) in allowed:
            kept.append(sig)
        else:
            dropped += 1

    outcome.kept = kept
    outcome.dropped = dropped
    if not kept:
        outcome.empty_result = True
        outcome.warnings.append(
            f"股票池 {outcome.pool_id or ''} 内没有任何信号命中"
            f"（池 {outcome.pool_symbol_count} 只 / 信号 {outcome.total_in} 条）"
        )

    logger.info(
        "池过滤完成 pool_id=%s checksum=%s kept=%d dropped=%d pool_size=%d",
        outcome.pool_id,
        outcome.pool_checksum,
        len(kept),
        dropped,
        outcome.pool_symbol_count,
    )
    return outcome


def intersect_symbols(
    base: Iterable[str] | None, extra: Iterable[str] | None
) -> list[str]:
    """两组代码求交集（前缀式比较，保序取 base）。用于「池 ∩ 显式指定股票」。"""
    if base is None:
        return [_to_prefix(s) for s in (extra or [])]
    if extra is None:
        return [_to_prefix(s) for s in base]
    extra_norm = {_to_prefix(s) for s in extra}
    return [_to_prefix(s) for s in base if _to_prefix(s) in extra_norm]
