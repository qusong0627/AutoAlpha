#!/usr/bin/env python3
"""美国宏观因子拉取 → QuantUS 本地 parquet。

从 akshare 拉取美国宏观指标（PMI/失业率/PPI/零售/房价/贸易），
统一为日期序列，落盘供全市场共用特征。

落盘格式:
  {quantus}/5_technical_derived/macro_usa/macro_usa.parquet
  列: trade_date, macro_pmi, macro_unemployment, macro_ppi,
      macro_retail_sales, macro_spcs20, macro_trade_balance

用法:
  python backend/scripts/sync_macro_usa.py
  python backend/scripts/sync_macro_usa.py --dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("sync_macro_usa")

QUANTUS_DATA_DIR = Path(os.getenv("QM_QUANTUS_DATA_DIR", str(PROJECT_ROOT / "data" / "quantus")))
REL_DIR = "5_technical_derived/macro_usa"

# akshare 接口 → (特征名, 转换函数)
# 原始 df: 商品/日期/今值/预测值/前值，取今值
_MACRO_INTERFACES = {
    "macro_usa_pmi": "macro_pmi",
    "macro_usa_unemployment_rate": "macro_unemployment",
    "macro_usa_ppi": "macro_ppi",
    "macro_usa_retail_sales": "macro_retail_sales",
    "macro_usa_spcs20": "macro_spcs20",
    "macro_usa_trade_balance": "macro_trade_balance",
    # 2026-08 扩充: 通胀/就业/景气/地产/货币
    "macro_usa_cpi_monthly": "macro_cpi_mom",
    "macro_usa_cpi_yoy": "macro_cpi_yoy",
    "macro_usa_core_cpi_monthly": "macro_core_cpi_mom",
    "macro_usa_core_ppi": "macro_core_ppi",
    "macro_usa_core_pce_price": "macro_core_pce",
    "macro_usa_non_farm": "macro_non_farm",
    "macro_usa_initial_jobless": "macro_initial_jobless",
    "macro_usa_adp_employment": "macro_adp_employment",
    "macro_usa_ism_pmi": "macro_ism_pmi",
    "macro_usa_ism_non_pmi": "macro_ism_non_pmi",
    "macro_usa_services_pmi": "macro_services_pmi",
    "macro_usa_michigan_consumer_sentiment": "macro_michigan_sentiment",
    "macro_usa_gdp_monthly": "macro_gdp_monthly",
    "macro_usa_durable_goods_orders": "macro_durable_goods",
    "macro_usa_house_starts": "macro_house_starts",
    "macro_usa_exist_home_sales": "macro_exist_home_sales",
    "macro_usa_industrial_production": "macro_industrial_production",
    "macro_usa_eia_crude_rate": "macro_eia_crude",
    "macro_usa_api_crude_stock": "macro_api_crude_stock",
    "macro_usa_rig_count": "macro_rig_count",
}


def _quantus_root() -> Path:
    env_val = os.getenv("QM_QUANTUS_DATA_DIR", "").strip()
    if env_val:
        p = Path(env_val)
        p.mkdir(parents=True, exist_ok=True)
        return p
    if Path("/data/quantus").is_dir() and any(Path("/data/quantus").iterdir()):
        return Path("/data/quantus")
    QUANTUS_DATA_DIR.mkdir(parents=True, exist_ok=True)
    return QUANTUS_DATA_DIR


def _fetch_series(interface: str) -> pd.DataFrame | None:
    """拉取单个宏观接口 → (trade_date, value) 序列。"""
    import akshare as ak

    try:
        df = getattr(ak, interface)()
    except Exception as exc:  # noqa: BLE001
        log.warning("%s 拉取失败: %s", interface, str(exc)[:100])
        return None
    if df is None or df.empty:
        return None

    # 列: 商品/日期/今值/预测值/前值
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    if "日期" not in df.columns or "今值" not in df.columns:
        log.warning("%s 缺少日期/今值列: %s", interface, list(df.columns))
        return None

    out = pd.DataFrame({
        "trade_date": pd.to_datetime(df["日期"], errors="coerce"),
        "value": pd.to_numeric(df["今值"], errors="coerce"),
    })
    out = out.dropna(subset=["trade_date"])
    out = out.sort_values("trade_date").reset_index(drop=True)
    return out


def sync_macro(*, dry_run: bool = False) -> dict:
    """拉取全部宏观接口，合并为单表落盘。"""
    series_map: dict[str, pd.DataFrame] = {}
    errors = []

    for interface, feat_name in _MACRO_INTERFACES.items():
        s = _fetch_series(interface)
        if s is None:
            errors.append(interface)
            continue
        s = s.rename(columns={"value": feat_name})
        series_map[feat_name] = s.set_index("trade_date")[feat_name]
        log.info("[%s] %s: %d 行, %s ~ %s", interface, feat_name,
                 len(s), s["trade_date"].min().date(), s["trade_date"].max().date())

    if not series_map:
        return {"status": "error", "error": "所有宏观接口拉取失败", "errors": errors}

    # 外连接合并（不同指标发布日不同，按日期对齐，缺失为 NaN）
    merged = pd.DataFrame(index=pd.date_range(
        min(s.index.min() for s in series_map.values()),
        max(s.index.max() for s in series_map.values()),
        freq="D",
    ))
    for feat, s in series_map.items():
        merged = merged.join(s, how="left")

    merged = merged.reset_index().rename(columns={"index": "trade_date"})
    merged["trade_date"] = merged["trade_date"].dt.date

    target_dir = _quantus_root() / REL_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    out_path = target_dir / "macro_usa.parquet"

    if dry_run:
        return {
            "status": "dry_run",
            "rows": len(merged),
            "cols": list(merged.columns),
            "range": f"{merged['trade_date'].min()} ~ {merged['trade_date'].max()}",
            "errors": errors,
            "target": str(out_path),
        }

    merged.to_parquet(out_path, index=False)
    log.info("写入 %s: %d 行, %d 列", out_path, len(merged), len(merged.columns))

    return {
        "status": "ok",
        "rows": len(merged),
        "cols": list(merged.columns),
        "range": f"{merged['trade_date'].min()} ~ {merged['trade_date'].max()}",
        "errors": errors,
        "target": str(out_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="美国宏观因子拉取")
    parser.add_argument("--dry-run", action="store_true", help="仅预览")
    args = parser.parse_args()

    try:
        result = sync_macro(dry_run=args.dry_run)
        print(result)
        return 0
    except Exception as exc:  # noqa: BLE001
        log.error("宏观因子同步失败: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
