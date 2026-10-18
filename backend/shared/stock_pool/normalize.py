"""股票池成员代码口径归一。

分层口径（见 AGENTS.md）：
- 库内成员统一存「后缀式」：CN `600036.SH` / HK `0700.HK` / US `AAPL` /
  FUTURES `AU2512.SHF` / BC `BTCUSDT`。
- API 出入参统一「前缀式」：CN `SH600036`；HK/US/FUTURES/BC 保持后缀式原文。

所有转换必须经本模块（内部再委托 StockCodeUtil），禁止散落手写切片。
"""

from __future__ import annotations

import re

from backend.shared.stock_utils import StockCodeUtil

from .constants import MARKET_CN, MARKET_HK, MARKETS

_US_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,14}$")


def normalize_market(market: str | None) -> str:
    """市场代码归一：空值默认 CN，统一大写。"""
    code = (market or "").strip().upper()
    if not code:
        return MARKET_CN
    if code in ("A", "A_SHARE", "ASHARE", "CN_STOCK", "CHINA"):
        return MARKET_CN
    if code in ("HK", "HONG_KONG", "HKSTOCK"):
        return MARKET_HK
    return code if code in MARKETS else code


def to_storage_symbol(symbol: str, market: str = MARKET_CN) -> str:
    """任意输入 → 库内后缀式口径。"""
    raw = str(symbol or "").strip()
    if not raw:
        return ""

    mk = normalize_market(market)

    if mk == MARKET_CN:
        return StockCodeUtil.to_suffix(raw)

    if mk == MARKET_HK:
        return StockCodeUtil.to_hk_suffix(raw)

    # US / FUTURES / BC：统一大写，去掉可能存在的交易所前缀
    code = raw.upper()
    if mk == "US":
        # 兼容 "US.AAPL" / "NASDAQ:AAPL" 写法
        if code.startswith("US."):
            code = code[3:]
        if ":" in code:
            code = code.split(":", 1)[1]
        return code
    return code


def to_api_symbol(symbol: str, market: str = MARKET_CN) -> str:
    """库内后缀式 → API 前缀式口径。"""
    raw = str(symbol or "").strip()
    if not raw:
        return ""
    mk = normalize_market(market)
    if mk == MARKET_CN:
        return StockCodeUtil.to_prefix(raw)
    return raw.upper()


def is_valid_symbol(symbol: str, market: str = MARKET_CN) -> bool:
    """按市场做轻量格式校验（不查库，仅拦明显脏数据）。"""
    code = to_storage_symbol(symbol, market)
    if not code:
        return False
    mk = normalize_market(market)
    if mk == MARKET_CN:
        return bool(re.match(r"^\d{6}\.(SH|SZ|BJ)$", code))
    if mk == MARKET_HK:
        return bool(re.match(r"^\d{4,5}\.HK$", code))
    if mk == "US":
        return bool(_US_TICKER_RE.match(code))
    # FUTURES / BC 无统一规则，只要非空且长度合理即放行
    return 1 <= len(code) <= 32


def normalize_symbols(symbols, market: str = MARKET_CN) -> list[str]:
    """批量归一 + 去重（保序）。"""
    seen: set[str] = set()
    out: list[str] = []
    for item in symbols or []:
        code = to_storage_symbol(item, market)
        if not code or code in seen:
            continue
        seen.add(code)
        out.append(code)
    return out


def normalize_to_qlib(symbol: str, market: str = MARKET_CN) -> str:
    """库内后缀式 → Qlib 桥接格式（全小写前缀，如 sh600036）。"""
    code = to_storage_symbol(symbol, market)
    if normalize_market(market) != MARKET_CN:
        return code.lower()
    return StockCodeUtil.to_qlib(code)


def checksum_symbols(symbols) -> str:
    """成员集合的稳定校验和（排序后 sha256 前 16 位），用于版本可复现性。"""
    import hashlib

    joined = "\n".join(sorted(str(s) for s in (symbols or [])))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]
