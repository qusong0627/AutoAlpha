#!/usr/bin/env python3
"""重建完整历史数据的 core parquet (2016-2026)。

训练脚本 docker/training/train.py 优先读 model_features_core.parquet；
文件缺失时会回退到逐年 parquet 并 concat 全部 1000 万+行，实测触发 OOM。

列集合来自 config/features/model_training_feature_catalog_v1.json 中
default_selected=true 的因子，只保留全年份 schema 都存在的列，
外加 label 构建与行过滤所需的基础列。
"""

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

# 路径配置
if os.path.exists("/app"):
    PROJECT_ROOT = Path("/app")
    DATA_DIR = PROJECT_ROOT / "db" / "feature_snapshots"
else:
    PROJECT_ROOT = Path(__file__).resolve().parents[2]
    DATA_DIR = PROJECT_ROOT / "db" / "feature_snapshots"

CATALOG_PATH = PROJECT_ROOT / "config" / "features" / "model_training_feature_catalog_v1.json"

# 基础列：训练脚本用于 label 构建（mom_ret_Nd）、行过滤（is_st/volume）、分组（symbol/trade_date）
BASE_COLUMNS = ["trade_date", "symbol", "volume", "is_st"]

# label 可能用到的任意 horizon，即使未被默认勾选也要保留
LABEL_COLUMNS = [f"mom_ret_{h}d" for h in (1, 3, 5, 10, 20, 60, 120)]

# 行业编码：训练脚本的 industry_as_feature 分支会用到
INDUSTRY_COLUMNS = ["ind_code_l1", "ind_code_l2"]

BATCH_SIZE = 200_000


def _load_default_features() -> list[str]:
    """从特征目录读取 default_selected=true 的因子 key。"""
    if not CATALOG_PATH.exists():
        print(f"❌ 特征目录不存在: {CATALOG_PATH}")
        sys.exit(1)
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    keys: list[str] = []
    for category in catalog.get("categories", []):
        for feat in category.get("features", []):
            if feat.get("default_selected") and feat.get("key"):
                keys.append(str(feat["key"]))
    if not keys:
        print("❌ 特征目录中没有 default_selected=true 的因子")
        sys.exit(1)
    return list(dict.fromkeys(keys))


def _yearly_files() -> list[Path]:
    files = sorted(DATA_DIR.glob("model_features_20*.parquet"))
    skip = ("core", "hk", "us", "crypto")
    return [f for f in files if not any(s in f.name for s in skip)]


def _load_industry_map() -> pd.DataFrame | None:
    """读取 symbol → ind_code_l1 映射。

    编码必须一次性建立并全年复用：若在每个 batch 内各自 Categorical(...).codes，
    同一行业在不同 batch 会得到不同整数，编码彻底失效。
    """
    candidates = [
        Path(os.getenv("QM_QUANTDB_DATA_DIR", str(PROJECT_ROOT / "data" / "quantdb"))) / "2_base_sector",
        DATA_DIR / "2_base_sector",
    ]
    for base in candidates:
        path = base / "instrument_detail" / "instrument_list.parquet"
        if not path.exists():
            path = base / "instrument_detail" / "instrument_detail.parquet"
        if not path.exists():
            continue
        detail = pd.read_parquet(path)
        sym_col = next(
            (c for c in ("Symbol", "symbol", "wind_code") if c in detail.columns), None
        )
        if sym_col is None or "rs_hycode_sim" not in detail.columns:
            print(f"  ⚠️ {path} 缺少 symbol/rs_hycode_sim 列")
            continue
        ind_map = detail[[sym_col, "rs_hycode_sim"]].dropna()
        ind_map = ind_map.rename(columns={sym_col: "symbol", "rs_hycode_sim": "ind_code_l1"})
        ind_map["symbol"] = _normalize_symbol(ind_map["symbol"])
        ind_map = ind_map.drop_duplicates(subset="symbol")
        ind_map["ind_code_l1"] = pd.Categorical(ind_map["ind_code_l1"]).codes.astype(np.float32)
        ind_map["ind_code_l2"] = np.float32(-1.0)
        print(f"  行业映射: {len(ind_map)} 只股票 ← {path}")
        return ind_map.set_index("symbol")
    print("  ⚠️ instrument_detail.parquet 未找到，行业编码填 -1")
    return None


def _normalize_symbol(series: pd.Series) -> pd.Series:
    """统一股票代码为 6 位数字：000001.SZ / SZ000001 / 1 → 000001。

    2026 年 parquet 的最后两个交易日混入了后缀格式（000001.SZ），
    仅 zfill 无法归一，会让同一只股票在 groupby 时裂成两条序列，
    直接破坏按 symbol 分组构建的 label。
    """
    s = series.astype(str).str.strip().str.upper()
    s = s.str.split(".").str[0]
    s = s.str.replace(r"^(SH|SZ|BJ)", "", regex=True)
    return s.str.zfill(6)


def _build_schema(feature_cols: list[str]) -> pa.Schema:
    """固定输出 schema。

    逐年 parquet 的 trade_date dtype 不一致（2016-2025 为 timestamp[ns]，
    2026 为 date32[day]）。若让 ParquetWriter 沿用首个 batch 的 schema，
    写到 2026 会因类型不符抛错。这里统一钉死为 timestamp[ns] + float32。
    """
    fields = [
        pa.field("trade_date", pa.timestamp("ns")),
        pa.field("symbol", pa.string()),
    ]
    fields += [pa.field(c, pa.float32()) for c in feature_cols]
    return pa.schema(fields)


def main() -> None:
    parser = argparse.ArgumentParser(description="重建 core parquet")
    parser.add_argument("--dry-run", action="store_true", help="只打印列集合，不写文件")
    parser.add_argument("--all", action="store_true", help="包含年度文件并集的全部列（解锁 catalog 之外的 L2/微观结构特征）")
    args = parser.parse_args()

    print("=" * 70)
    print("重建完整历史 Core Parquet - 流式分块模式")
    print("=" * 70)

    files = _yearly_files()
    if not files:
        print(f"❌ 未找到 yearly parquet 文件: {DATA_DIR}")
        sys.exit(1)

    schemas = {f: set(pq.ParquetFile(f).schema_arrow.names) for f in files}
    common = set.intersection(*schemas.values())
    all_cols = set.union(*schemas.values())

    if args.all:
        # 全量模式：年度文件并集即特征集（catalog 勾选与否不影响 core 内容）
        _EXCLUDE = set(BASE_COLUMNS) | set(INDUSTRY_COLUMNS) | {
            "trade_date", "symbol", "date",
            # 全历史 notna=0 的死列（catalog 同步排除）
            "concept_flow_rank", "ind_netflow_rank_20",
        }
        default_features, wanted = [], sorted(all_cols - _EXCLUDE)
    else:
        default_features = _load_default_features()
        wanted = list(dict.fromkeys(default_features + LABEL_COLUMNS))

    # 因子列用并集：L2 因子 2023-01 才上线，2016-2022 年份缺列时填 NaN
    # （树模型原生处理 NaN，训练时 fill_values 兜底；交集会直接丢掉 L2）。
    feature_cols = [c for c in wanted if c in all_cols and c not in BASE_COLUMNS]
    dropped = [c for c in wanted if c not in all_cols]
    # is_st / volume 用于行过滤，独立于因子集合
    # is_st 在年份文件中普遍缺失，从 instrument_detail 的 IsSTGP 生成（写入时按 symbol 映射）
    passthrough = [c for c in BASE_COLUMNS if c not in ("trade_date", "symbol") and c in all_cols]
    # 行业编码由 instrument_detail 映射生成，不依赖逐年 parquet（仅 2026 带这两列）
    feature_cols = list(dict.fromkeys(feature_cols + passthrough + INDUSTRY_COLUMNS))

    print(f"\n输入: {len(files)} 个年度文件, 各年共有列 {len(common)}, 并集列 {len(all_cols)}")
    print(f"默认勾选因子: {len(default_features)}")
    if dropped:
        print(f"⚠️ 因所有年份均不存在而剔除 {len(dropped)} 列: {dropped}")
    missing_base = [c for c in BASE_COLUMNS if c not in common]
    if missing_base:
        print(f"⚠️ 基础列缺失（非全年份存在）: {missing_base}")
    print(f"输出列: {len(feature_cols) + 2} (trade_date + symbol + {len(feature_cols)})")

    if args.dry_run:
        print("\nDRY RUN，未写入")
        return

    print("\n读取行业映射...")
    ind_map = _load_industry_map()

    output_path = DATA_DIR / "model_features_core.parquet"
    tmp_path = output_path.with_suffix(".parquet.tmp")
    schema = _build_schema(feature_cols)
    writer = pq.ParquetWriter(tmp_path, schema, compression="snappy")
    total_rows = 0

    try:
        for f in files:
            # 行业编码一律由 instrument_detail 映射生成，不读逐年 parquet 的同名列：
            # 2026 自带的 ind_code_l1 覆盖率仅 1.5% 且编码口径与映射不同（原始 rs_hycode
            # vs Categorical 序号），混用会让同一行业在不同年份对应不同数值。
            available = [
                c
                for c in ["trade_date", "symbol"] + feature_cols
                if c in schemas[f] and c not in INDUSTRY_COLUMNS
            ]
            year_rows = 0
            for batch in pq.ParquetFile(f).iter_batches(
                batch_size=BATCH_SIZE, columns=available
            ):
                df = batch.to_pandas()
                df["trade_date"] = pd.to_datetime(df["trade_date"], errors="coerce")
                df["symbol"] = _normalize_symbol(df["symbol"])

                # 行业编码：从全局映射取，保证跨 batch/跨年编码一致
                for col in INDUSTRY_COLUMNS:
                    if ind_map is not None:
                        df[col] = (
                            df["symbol"].map(ind_map[col]).fillna(-1.0).astype(np.float32)
                        )
                    else:
                        df[col] = np.float32(-1.0)

                # 该年缺失的列补 NaN，保持 schema 一致
                for col in feature_cols:
                    if col not in df.columns:
                        df[col] = np.nan
                    df[col] = pd.to_numeric(df[col], errors="coerce").astype(np.float32)

                table = pa.Table.from_pandas(
                    df[["trade_date", "symbol"] + feature_cols],
                    schema=schema,
                    preserve_index=False,
                )
                writer.write_table(table)
                year_rows += len(df)
                total_rows += len(df)
            print(f"  {f.name}: {year_rows:,} 行 (累计 {total_rows:,})")
    except Exception:
        writer.close()
        tmp_path.unlink(missing_ok=True)
        raise

    writer.close()
    tmp_path.replace(output_path)

    size_mb = output_path.stat().st_size / (1024 * 1024)
    out = pq.ParquetFile(output_path)
    print(f"\n✅ 写入完成: {output_path}")
    print(f"  大小: {size_mb:.1f} MB")
    print(f"  行数: {out.metadata.num_rows:,}")
    print(f"  列数: {len(out.schema_arrow.names)}")

    print("\n" + "=" * 70)
    print("✅ 完成! Core parquet 已重建")
    print("=" * 70)


if __name__ == "__main__":
    main()
