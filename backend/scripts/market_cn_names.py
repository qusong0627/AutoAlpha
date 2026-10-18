#!/usr/bin/env python3
"""港美股证券主表（security_master）+ 中文名称回填。

security_master: {market}/2_base_sector/security_master/data.parquet
  列: symbol, cn_name, en_name, source, updated_at
  每次同步全市场刷新一次（HK: 新浪全市场快照；US: 腾讯批量行情），
  新上市股票自动进入主表。

f10 回填: {market}/2_base_sector/f10/{symbol}.parquet 的 name 列，
  yahoo f10 的 name 是英文长名，用主表 cn_name 覆盖为中文名。

用法:
  python backend/scripts/market_cn_names.py --market HK
  python backend/scripts/market_cn_names.py --market US
  python backend/scripts/market_cn_names.py --market HK,US --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("market_cn_names")

_MARKET_ENV = {"US": "QM_QUANTUS_DATA_DIR", "HK": "QM_QUANTHK_DATA_DIR"}
_MARKET_DEFAULT_DIR = {"US": "/data/quantus", "HK": "/data/quanthk"}
TX_BATCH = 60
TX_TIMEOUT = 20

SECURITY_MASTER_DIR = "2_base_sector/security_master"

# 已退市标的（新浪/腾讯均无数据），静态回退中文名
DELISTED_HK_NAMES = {
    "0011.HK": "恒生银行",
    "3799.HK": "达利食品",
    "6837.HK": "海通证券",
}
DELISTED_US_NAMES = {
    "SIVB": "硅谷银行",
    "SBNY": "签名银行",
}


def _data_dir(market: str) -> Path:
    env_val = os.getenv(_MARKET_ENV[market], "").strip()
    if env_val:
        return Path(env_val)
    container_dir = Path(_MARKET_DEFAULT_DIR[market])
    if container_dir.is_dir():
        return container_dir
    local = PROJECT_ROOT / "data" / ("quantus" if market == "US" else "quanthk")
    local.mkdir(parents=True, exist_ok=True)
    return local


def _hk_csv_path() -> Path | None:
    for p in (
        Path(__file__).parent / "hk.csv",
        Path("/app/backend/scripts/hk.csv"),
        Path("/data/hk.csv"),
    ):
        if p.is_file():
            return p
    return None


def _hk_symbol(code: str) -> str:
    """00001 → 0001.HK（QuantHK 内部格式）。"""
    code = str(code).strip().split(".")[0].zfill(5)
    if code.startswith("0") and len(code) == 5:
        code = code[1:]
    return f"{code}.HK"


def _tx_code(symbol: str) -> str:
    return symbol.replace("-", ".")


def _hk_tencent_code(symbol: str) -> str:
    """0001.HK → 00001（腾讯 5 位代码）。"""
    return symbol.split(".")[0].zfill(5)


def fetch_tencent_hk_fill(symbols: list[str]) -> dict[str, str]:
    """腾讯港股批量行情，补新浪主表缺名的标的 → {0001.HK: 长和}。

    腾讯不返回退市标的的行，绝不能按请求顺序 zip 对齐；
    必须逐行解析行内代码再匹配。退市标的不在返回中，由调用方走静态回退。
    """
    import requests

    wanted = {_hk_tencent_code(s) for s in symbols}
    mapping: dict[str, str] = {}
    for i in range(0, len(symbols), TX_BATCH):
        batch = symbols[i : i + TX_BATCH]
        codes = [_hk_tencent_code(s) for s in batch]
        url = "https://qt.gtimg.cn/q=" + ",".join(f"r_hk{c}" for c in codes)
        try:
            resp = requests.get(url, timeout=TX_TIMEOUT)
            resp.encoding = "gbk"
        except Exception as exc:  # noqa: BLE001
            log.warning("腾讯港股批次失败(%s-%s): %s", batch[0], batch[-1], exc)
            continue
        for line in resp.text.split(";"):
            if '="' not in line:
                continue
            # v_r_hk00823="100~领展房产基金~00823~38.78~...": 字段2=名称, 字段3=5位代码
            parts = line.split("~")
            if len(parts) < 3:
                continue
            name, code = parts[1].strip(), parts[2].strip()
            if code not in wanted:
                continue
            if not name or name.startswith("v_") or name.isdigit():
                continue
            mapping[f"{code[-4:]}.HK"] = name
    return mapping


def fetch_hk_master() -> pd.DataFrame:
    """新浪港股全市场快照（一次请求，含中文/英文名）。"""
    import akshare as ak

    df = ak.stock_hk_spot()
    if df is None or df.empty:
        raise RuntimeError("新浪港股全市场快照为空")
    out = pd.DataFrame({
        "symbol": df["代码"].map(_hk_symbol),
        "cn_name": df["中文名称"].astype(str).str.strip(),
        "en_name": df["英文名称"].astype(str).str.strip(),
    })
    out = out[out["cn_name"].ne("nan") & out["cn_name"].ne("")]
    return out.drop_duplicates(subset=["symbol"], keep="first").reset_index(drop=True)


def _fetch_tencent_us(symbols: list[str]) -> dict[str, str]:
    """腾讯批量行情 → {AAPL: 苹果}。

    响应行内自带代码（字段3: AAPL.OQ），必须逐行解析匹配，
    不能按请求顺序 zip 对齐（腾讯会跳过无数据的标的）。
    """
    import requests

    wanted = set(symbols)
    mapping: dict[str, str] = {}
    for i in range(0, len(symbols), TX_BATCH):
        batch = symbols[i : i + TX_BATCH]
        codes = [_tx_code(s) for s in batch]
        url = "https://qt.gtimg.cn/q=" + ",".join(f"us{c}" for c in codes)
        try:
            resp = requests.get(url, timeout=TX_TIMEOUT)
            resp.encoding = "gbk"
        except Exception as exc:  # noqa: BLE001
            log.warning("腾讯行情批次失败(%s-%s): %s", batch[0], batch[-1], exc)
            continue
        for line in resp.text.split(";"):
            if '="' not in line:
                continue
            # v_usAAPL="200~苹果~AAPL.OQ~305.93~...": 字段2=名称, 字段3=代码
            # 后缀 .OQ=纳斯达克 .N=纽交所 .AM=美交所 .PS=粉单；B 类股 BRK.B.N → 剥最后一个后缀
            parts = line.split("~")
            if len(parts) < 3:
                continue
            name = parts[1].strip()
            code = parts[2].strip()
            for suffix in (".OQ", ".N", ".AM", ".PS"):
                if code.endswith(suffix):
                    code = code[: -len(suffix)]
                    break
            sym = code.replace("-", ".")
            wanted_alt = code.replace(".", "-")
            if sym not in wanted and wanted_alt not in wanted:
                continue
            if not name or name.startswith("v_") or name.isdigit():
                continue
            mapping[wanted_alt if wanted_alt in wanted else sym] = name
    return mapping


def fetch_us_master(symbols: list[str] | None = None) -> pd.DataFrame:
    """腾讯批量行情 → 美股代码+中文名。"""
    if symbols is None:
        try:
            from backend.services.engine.rd_agent.data_pipeline.us_data import US_SYMBOLS

            symbols = list(US_SYMBOLS)
        except Exception as exc:  # noqa: BLE001
            log.warning("导入 US_SYMBOLS 失败: %s", exc)
            return pd.DataFrame(columns=["symbol", "cn_name", "en_name"])

    mapping = _fetch_tencent_us(symbols)
    if not mapping:
        raise RuntimeError("腾讯行情未返回任何美股")
    return pd.DataFrame(
        [{"symbol": s, "cn_name": n, "en_name": ""} for s, n in mapping.items()]
    ).drop_duplicates(subset=["symbol"], keep="first").reset_index(drop=True)


def build_security_master(market: str, symbols: list[str] | None = None) -> dict:
    """全市场拉取证券主表并落盘（覆盖式快照）。"""
    market = market.upper()
    if market not in _MARKET_ENV:
        raise ValueError(f"market 必须是 US/HK，收到 {market}")

    try:
        if market == "HK":
            df = fetch_hk_master()
            source = "akshare_stock_hk_spot"
        else:
            df = fetch_us_master(symbols)
            source = "tencent_quote"
    except Exception as exc:  # noqa: BLE001
        log.error("[%s] 主表拉取失败: %s", market, exc)
        return {"market": market, "status": "error", "error": str(exc)}

    df = df.copy()
    df["source"] = source
    df["updated_at"] = datetime.now().isoformat(timespec="seconds")

    # 退市标的静态名并入主表，保证所有读主表的入口（搜索/回填/下拉）都能拿到
    delisted = DELISTED_HK_NAMES if market == "HK" else DELISTED_US_NAMES
    if delisted:
        extra = pd.DataFrame([
            {"symbol": s, "cn_name": n, "en_name": "", "source": "delisted_static", "updated_at": datetime.now().isoformat(timespec="seconds")}
            for s, n in delisted.items()
        ])
        df = pd.concat([df, extra], ignore_index=True)
    root = _data_dir(market) / SECURITY_MASTER_DIR
    root.mkdir(parents=True, exist_ok=True)
    target = root / "data.parquet"
    df.to_parquet(target, index=False)
    log.info("[%s] 证券主表刷新: %d 只 → %s", market, len(df), target)
    return {"market": market, "rows": int(len(df)), "target": str(target)}


def _read_security_master(market: str) -> dict[str, str]:
    """只读主表文件 → {symbol: cn_name}（不存在返回空）。"""
    p = _data_dir(market) / SECURITY_MASTER_DIR / "data.parquet"
    if not p.exists():
        return {}
    df = pd.read_parquet(p)
    if "cn_name" not in df.columns or "symbol" not in df.columns:
        log.warning("[%s] 主表缺 symbol/cn_name 列: %s", market, list(df.columns))
        return {}
    mapping: dict[str, str] = {}
    for _, row in df.iterrows():
        name = str(row["cn_name"]).strip()
        if name and name.lower() != "nan":
            mapping[str(row["symbol"])] = name
    return mapping


def load_security_master(market: str) -> dict[str, str]:
    """读主表 → {symbol: cn_name}；主表不存在时现场构建。"""
    mapping = _read_security_master(market)
    if mapping:
        return mapping
    r = build_security_master(market)
    if r.get("status") == "error":
        return {}
    return _read_security_master(market)


def _hk_csv_fallback() -> dict[str, str]:
    """hk.csv 离线回退（新浪接口不可用时）。"""
    p = _hk_csv_path()
    if p is None:
        return {}
    df = pd.read_csv(p, encoding="utf-8-sig")
    if "id" not in df.columns or "name" not in df.columns:
        return {}
    mapping: dict[str, str] = {}
    for _, row in df.iterrows():
        name = str(row["name"]).strip()
        if name and name.lower() != "nan":
            mapping[_hk_symbol(row["id"])] = name
    return mapping


def backfill(market: str, symbols: list[str] | None = None, *, rebuild_master: bool = True) -> dict:
    """把中文名写回 f10 parquet 的 name 列（就地覆盖 name，保留其他列）。

    返回 {backfilled, missing_name, no_f10, total}。
    """
    market = market.upper()
    if market not in _MARKET_ENV:
        raise ValueError(f"market 必须是 US/HK，收到 {market}")

    f10_dir = _data_dir(market) / "2_base_sector" / "f10"
    if not f10_dir.is_dir():
        return {"market": market, "backfilled": 0, "missing_name": 0, "no_f10": 0, "total": 0}

    if symbols:
        targets = sorted(symbols)
    else:
        targets = sorted(p.stem for p in f10_dir.glob("*.parquet"))

    if rebuild_master:
        mapping = load_security_master(market)
    else:
        mapping = _read_security_master(market)
    if market == "HK" and not mapping:
        mapping = _hk_csv_fallback()

    # 新浪主表可能漏掉个别标的（如 REIT），缺名时用腾讯港股行情补查并持久化
    if market == "HK":
        mapping.update(DELISTED_HK_NAMES)
        missing_targets = [s for s in targets if s not in mapping]
        if missing_targets:
            fill = fetch_tencent_hk_fill(missing_targets)
            if fill:
                mapping.update(fill)
                master_p = _data_dir(market) / SECURITY_MASTER_DIR / "data.parquet"
                if master_p.exists():
                    try:
                        master_df = pd.read_parquet(master_p)
                        fill_df = pd.DataFrame([
                            {"symbol": s, "cn_name": n, "en_name": "", "source": "tencent_quote", "updated_at": datetime.now().isoformat(timespec="seconds")}
                            for s, n in fill.items()
                        ])
                        merged = pd.concat([master_df, fill_df], ignore_index=True)
                        merged = merged.drop_duplicates(subset=["symbol"], keep="first")
                        merged.to_parquet(master_p, index=False)
                        log.info("[%s] 主表补漏 %d 只（腾讯）: %s", market, len(fill), list(fill))
                    except Exception as exc:  # noqa: BLE001
                        log.warning("[%s] 主表补漏写入失败: %s", market, exc)

    # 美股主表缺名时（如新增标的）用腾讯行情补查
    if mapping and market == "US":
        mapping.update(DELISTED_US_NAMES)
        missing_targets = [s for s in targets if s not in mapping]
        if missing_targets:
            fill = _fetch_tencent_us(missing_targets)
            if fill:
                mapping.update(fill)

    stats = {"market": market, "backfilled": 0, "missing_name": 0, "no_f10": 0, "total": len(targets)}
    for symbol in targets:
        target = f10_dir / f"{symbol}.parquet"
        if not target.exists():
            stats["no_f10"] += 1
            continue
        cn_name = mapping.get(symbol)
        if not cn_name:
            stats["missing_name"] += 1
            continue
        df = pd.read_parquet(target)
        if "name" not in df.columns:
            stats["no_f10"] += 1
            continue
        df["name"] = cn_name
        df.to_parquet(target, index=False)
        stats["backfilled"] += 1
    log.info("[%s] 中文名回填: %s", market, stats)
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="港美股证券主表刷新 + f10 中文名称回填")
    parser.add_argument("--market", required=True, help="US/HK，逗号分隔多市场")
    parser.add_argument("--symbols", default=None, help="逗号分隔标的（默认 f10 目录全部）")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写盘")
    args = parser.parse_args()

    for market in [m.strip().upper() for m in args.market.split(",") if m.strip()]:
        syms = [s.strip() for s in args.symbols.split(",") if s.strip()] if args.symbols else None
        if args.dry_run:
            if market == "HK":
                try:
                    df = fetch_hk_master()
                except Exception as exc:  # noqa: BLE001
                    print(f"[{market}] 主表拉取失败: {exc}")
                    continue
                mapping = dict(zip(df["symbol"], df["cn_name"], strict=False))
            else:
                mapping = load_security_master(market)
            f10_dir = _data_dir(market) / "2_base_sector" / "f10"
            targets = syms or (sorted(p.stem for p in f10_dir.glob("*.parquet")) if f10_dir.is_dir() else [])
            missing = [s for s in targets if s not in mapping]
            print(f"[{market}] f10={len(targets)} 主表={len(mapping)} 缺名={len(missing)} {missing[:10]}")
        else:
            print(build_security_master(market, syms))
            print(backfill(market, syms))
    return 0


if __name__ == "__main__":
    sys.exit(main())
