#!/usr/bin/env python3
"""
精选因子集 - 从 197 个因子中筛选 ~75 个核心因子
==============================================

选择原则:
1. IC/ICIR 优先: 保留预测能力强的因子
2. 去冗余: 相关性 > 0.85 的因子只保留 IC 更高的
3. 类别均衡: 确保各类别都有代表
4. 实用性: 保留常用技术指标供参考

输出:
- data/quantdb/reports/core_factors.csv (因子列表)
- data/quantdb/reports/l1_core.parquet (精简后的数据)
"""

import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

# 路径配置：输入直读 QuantDB l1_factors 分区，输出报告落 QuantDB reports
PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = PROJECT_ROOT / "data" / "quantdb" / "reports"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


# ═══════════════════════════════════════════════════════════════════════════
# 精选因子列表 (手工 + 数据驱动)
# ═══════════════════════════════════════════════════════════════════════════

# 基础列 (必须保留)
BASE_COLS = [
    "symbol", "trade_date", "close", "open", "high", "low", "volume", "amount",
    "industry", "is_st", "listing_market",
]

# 核心因子 (~75个)
CORE_FACTORS = {
    # ── 动量因子 (10个) ──
    "momentum": [
        "mom_ret_1d",           # 日收益 (基础)
        "mom_ret_5d",           # 周动量
        "mom_ret_20d",          # 月动量
        "mom_ret_60d",          # 季动量 (IC 最高)
        "mom_ma_gap_5",         # 短期均线偏离
        "mom_ma_gap_20",        # 中期均线偏离
        "mom_rsi_14",           # RSI (经典)
        "mom_macd_hist",        # MACD 柱 (经典)
        "mom_sharpe_20",        # 风险调整动量
        "mom_breakout_20d",     # 突破因子
    ],

    # ── 波动率因子 (8个) ──
    "volatility": [
        "vol_std_20",           # 标准波动率
        "vol_atr_14",           # ATR (经典)
        "vol_parkinson_20",     # Parkinson 波动率
        "vol_downside_20",      # 下行波动
        "vol_upside_20",        # 上行波动
        "vol_realized_rv",      # 已实现波动率
        "vol_realized_rrv",     # 相对波动率 (IC 高)
        "vol_jump_zadj",        # 跳跃因子
    ],

    # ── 流动性因子 (6个) ──
    "liquidity": [
        "liq_volume",           # 成交量
        "liq_amount",           # 成交额
        "liq_turnover_os",      # 换手率
        "liq_volume_ratio_5",   # 量比
        "liq_mfi_14",           # MFI (经典)
        "liq_amihud_20",        # Amihud 非流动性
    ],

    # ── 资金流因子 (7个) ──
    "flow": [
        "flow_net_amount",      # 净流入
        "flow_net_amount_ratio", # 净流入占比
        "flow_large_net_amount", # 大单净流入 (已修复)
        "flow_vpin",            # VPIN
        "flow_vpin_ma_5",       # VPIN 短期
        "flow_vpin_ma_20",      # VPIN 长期
        "flow_pressure_index",  # 压力指数
    ],

    # ── 风格因子 (7个) ──
    "style": [
        "style_ln_mv_total",    # 对数市值 (已修复)
        "style_ln_mv_float",    # 对数流通市值 (已修复)
        "style_beta_20",        # Beta
        "style_beta_60",        # 长期 Beta
        "style_idio_vol_20",    # 特质波动
        "style_bp",             # 账面市值比 (已修复)
        "style_ep_ttm",         # 盈利收益率 (已修复)
    ],

    # ── 基本面因子 (8个) ──
    "fundamental": [
        "pe_ttm",               # PE
        "pb",                   # PB
        "roe",                  # ROE (IC 最高)
        "bp",                   # 账面市值比
        "ep_ttm",               # 盈利收益率 (IC 高)
        "ln_mv_total",          # 对数总市值
        "total_mv",             # 总市值
        "float_mv",             # 流通市值
    ],

    # ── 行业因子 (4个) ──
    "industry": [
        "ind_ret_1d",           # 行业日收益 (已修复)
        "ind_ret_20d",          # 行业20日收益 (已修复)
        "ind_strength_20",      # 行业强度 (已修复)
        "ind_momentum_rank_20", # 行业动量排名 (已修复)
    ],

    # ── K线形态因子 (6个) ──
    "kline": [
        "kline_kmid",           # 实体比
        "kline_klen",           # 振幅比
        "kline_kup2",           # 上影线占比
        "kline_klow2",          # 下影线占比
        "kline_ksft2",          # 重心偏移
        "prel_vwap0",           # VWAP/收盘
    ],

    # ── 技术形态因子 (5个) ──
    "tech": [
        "tech_bollinger_position", # 布林带位置
        "tech_williams_r_14",   # Williams %R
        "tech_cci_20",          # CCI
        "kdj_k",                # KDJ K
        "kdj_d",                # KDJ D
    ],

    # ── Alpha 因子 (7个) ──
    "alpha": [
        "alpha_decay_ret_10",   # 衰减动量
        "alpha_corr_cv_20",     # 量价相关
        "alpha_tsrank_ret_20",  # 收益时序排名
        "alpha_high_20d_ratio", # 创新高频率 (IC 高)
        "alpha_close_open_gap", # 跳空缺口
        "fund_pb_percentile",   # PB 历史分位 (IC 最高!)
        "fund_pe_percentile",   # PE 历史分位
    ],

    # ── 趋势质量因子 (3个) ──
    "trend": [
        "trend_r2_20",          # 趋势 R²
        "trend_slope_20",       # 趋势斜率
        "pv_corr_20",           # 量价相关
    ],

    # ── 价格位置因子 (3个) ──
    "price_position": [
        "price_position_20",    # 20日价格位置
        "price_position_60",    # 60日价格位置
        "dist_to_high_20",      # 距离新高
    ],

    # ── 指数/概念因子 (4个) ──
    "index_concept": [
        "idx_all",              # 全市场指数成分
        "idx_hs300",            # 沪深300
        "idx_margin",           # 融资融券
        "concept_new_energy",   # 新能源概念
    ],
}


def get_all_core_factors() -> list:
    """获取所有核心因子列表"""
    factors = []
    for cat, cols in CORE_FACTORS.items():
        factors.extend(cols)
    return factors


def select_factors_data_driven(df: pd.DataFrame, target_count: int = 75) -> list:
    """
    数据驱动的因子选择 (备用方案)

    步骤:
    1. 计算每个因子的 IC
    2. 按 IC 排序
    3. 贪心去冗余 (每次选 IC 最高的，删除与其相关 > 0.85 的)
    """
    from backend.scripts.evaluate_factors import compute_ic_series, compute_ic_stats

    # 计算未来收益
    df = df.sort_values(["symbol", "trade_date"])
    df["fwd_ret_5d"] = df.groupby("symbol")["close"].transform(lambda x: x.shift(-5) / x - 1)
    df = df.dropna(subset=["fwd_ret_5d"])

    # 获取所有因子列
    exclude = BASE_COLS + ["fwd_ret_5d"]
    factor_cols = [c for c in df.columns if c not in exclude and df[c].dtype in ["float64", "int64"]]

    # 计算 IC
    ic_results = []
    for col in factor_cols:
        ic_series = compute_ic_series(df, col, "fwd_ret_5d")
        stats = compute_ic_stats(ic_series, col)
        ic_results.append(stats)

    ic_df = pd.DataFrame(ic_results).sort_values("icir", ascending=False)

    # 贪心选择
    selected = []
    for _, row in ic_df.iterrows():
        if len(selected) >= target_count:
            break

        col = row["factor"]
        # 检查与已选因子的相关性
        is_redundant = False
        if selected:
            corr = df[[col] + selected[-5:]].corr().iloc[0, 1:].abs().max()
            if corr > 0.85:
                is_redundant = True

        if not is_redundant and row["ic_abs_mean"] > 0.01:
            selected.append(col)

    return selected


def create_core_parquet(df: pd.DataFrame, output_path: Path, factor_list: list):
    """创建精简版 parquet（输入为内存中的 QuantDB l1_factors 全量帧）"""
    print(f"  原始: {len(df):,} 行, {len(df.columns)} 列")

    # 选择列
    all_cols = BASE_COLS + factor_list
    available_cols = [c for c in all_cols if c in df.columns]
    missing_cols = [c for c in all_cols if c not in df.columns]

    if missing_cols:
        print(f"  ⚠️ 缺失列: {missing_cols[:10]}...")

    df_core = df[available_cols].copy()
    print(f"  精选: {len(df_core):,} 行, {len(df_core.columns)} 列")

    # 保存
    df_core.to_parquet(output_path, index=False)
    file_size = output_path.stat().st_size / (1024 * 1024)
    print(f"  已保存: {output_path} ({file_size:.1f} MB)")

    return df_core


def export_factor_catalog(factor_list: list, output_path: Path):
    """导出因子目录 CSV"""
    rows = []
    for cat, cols in CORE_FACTORS.items():
        for col in cols:
            if col in factor_list:
                rows.append({
                    "factor": col,
                    "category": cat,
                    "selected": True,
                })

    catalog = pd.DataFrame(rows)
    catalog.to_csv(output_path, index=False)
    print(f"因子目录已导出: {output_path} ({len(catalog)} 个因子)")


def main():
    print("=" * 70)
    print("精选因子集生成器")
    print("=" * 70)

    # 获取核心因子列表
    core_factors = get_all_core_factors()
    print(f"\n预选因子数: {len(core_factors)}")

    # 统计各类别
    print("\n类别分布:")
    for cat, cols in CORE_FACTORS.items():
        print(f"  {cat}: {len(cols)} 个")

    # 创建精简 parquet（直读 QuantDB l1_factors 分区）
    from backend.shared.feature_source import read_factor_source

    print("\n加载 QuantDB l1_factors 因子分区...")
    df_all = read_factor_source("l1_factors", "CN")
    output_parquet = OUTPUT_DIR / "l1_core.parquet"
    df_core = create_core_parquet(df_all, output_parquet, core_factors)

    # 导出因子目录
    output_catalog = OUTPUT_DIR / "core_factors.csv"
    export_factor_catalog(core_factors, output_catalog)

    # 验证
    print("\n验证:")
    print(f"  行数: {len(df_core):,}")
    print(f"  列数: {len(df_core.columns)}")
    print(f"  日期范围: {df_core['trade_date'].min()} ~ {df_core['trade_date'].max()}")
    print(f"  股票数: {df_core['symbol'].nunique()}")

    print("\n" + "=" * 70)
    print("完成!")
    print("=" * 70)


if __name__ == "__main__":
    main()
