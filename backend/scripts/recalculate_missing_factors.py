#!/usr/bin/env python3
"""
补算旧版 Parquet 缺失的 9 个因子
===================================
从 OHLCV 数据计算:
- amount (近似)
- kdj_k
- tech_bollinger_position
- tech_cci_20
- alpha_decay_ret_10
- alpha_corr_cv_20
- alpha_tsrank_ret_20
- alpha_high_20d_ratio
- alpha_close_open_gap
"""

import os
import sys
from pathlib import Path
import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

# 路径配置
if os.path.exists("/app"):
    DATA_DIR = Path("/app/db/feature_snapshots")
else:
    DATA_DIR = Path("/workspace/quantmind/db/feature_snapshots")


def calculate_kdj_k(high, low, close, n=9):
    """计算 KDJ K 值"""
    low_n = low.rolling(window=n, min_periods=1).min()
    high_n = high.rolling(window=n, min_periods=1).max()
    rsv = (close - low_n) / (high_n - low_n).replace(0, np.nan) * 100
    k = rsv.ewm(com=2, adjust=False).mean()
    return k


def calculate_bollinger_position(close, window=20):
    """计算布林带位置: (close - ma) / (2 * std)"""
    ma = close.rolling(window=window, min_periods=1).mean()
    std = close.rolling(window=window, min_periods=1).std()
    position = (close - ma) / (2 * std).replace(0, np.nan)
    return position.clip(-2, 2)  # 限制在 [-2, 2] 范围


def calculate_cci_20(high, low, close, window=20):
    """计算 CCI (Commodity Channel Index)"""
    tp = (high + low + close) / 3
    ma = tp.rolling(window=window, min_periods=1).mean()
    md = tp.rolling(window=window, min_periods=1).apply(
        lambda x: np.abs(x - x.mean()).mean(), raw=True
    )
    cci = (tp - ma) / (0.015 * md).replace(0, np.nan)
    return cci


def calculate_decay_ret_10(close, decay_factor=0.9):
    """计算衰减加权收益率 (近期权重更大)"""
    ret = close.pct_change()
    weights = np.array([decay_factor ** (9 - i) for i in range(10)])
    weights = weights / weights.sum()

    def weighted_sum(x):
        if len(x) < 10:
            return np.nan
        return np.sum(x[-10:] * weights)

    return ret.rolling(window=10, min_periods=10).apply(weighted_sum, raw=True)


def calculate_corr_cv_20(close, volume, window=20):
    """计算收盘价-成交量相关性"""
    return close.rolling(window=window, min_periods=10).corr(volume)


def calculate_tsrank_ret_20(close, window=20):
    """计算收益率时序排名 (当前收益率在过去 N 天中的百分位)"""
    ret = close.pct_change()

    def rank_current(x):
        if len(x) < window:
            return np.nan
        current = x[-1]
        rank = (x < current).sum() / len(x)
        return rank

    return ret.rolling(window=window, min_periods=window).apply(rank_current, raw=True)


def calculate_high_20d_ratio(high, window=20):
    """计算 20 日新高比例 (当前价格 / 20 日最高价)"""
    high_20d = high.rolling(window=window, min_periods=1).max()
    return high / high_20d.replace(0, np.nan)


def calculate_close_open_gap(close, open_price):
    """计算收盘-开盘缺口"""
    return (close - open_price) / open_price.replace(0, np.nan)


def process_parquet(input_path, output_path):
    """处理单个 parquet 文件，补算缺失因子"""
    print(f"\n处理 {input_path.name}...")

    # 读取数据
    df = pd.read_parquet(input_path)
    print(f"  原始: {len(df):,} 行, {len(df.columns)} 列")

    # 按 symbol 分组计算
    new_cols = {}

    for symbol, group in df.groupby('symbol'):
        idx = group.index
        o, h, l, c, v = group['open'], group['high'], group['low'], group['close'], group['volume']

        # 补算因子
        if 'amount' not in df.columns:
            new_cols.setdefault('amount', pd.Series(index=df.index, dtype='float32'))
            new_cols['amount'].loc[idx] = (c * v).astype('float32')

        if 'kdj_k' not in df.columns:
            new_cols.setdefault('kdj_k', pd.Series(index=df.index, dtype='float32'))
            new_cols['kdj_k'].loc[idx] = calculate_kdj_k(h, l, c).astype('float32')

        if 'tech_bollinger_position' not in df.columns:
            new_cols.setdefault('tech_bollinger_position', pd.Series(index=df.index, dtype='float32'))
            new_cols['tech_bollinger_position'].loc[idx] = calculate_bollinger_position(c).astype('float32')

        if 'tech_cci_20' not in df.columns:
            new_cols.setdefault('tech_cci_20', pd.Series(index=df.index, dtype='float32'))
            new_cols['tech_cci_20'].loc[idx] = calculate_cci_20(h, l, c).astype('float32')

        if 'alpha_decay_ret_10' not in df.columns:
            new_cols.setdefault('alpha_decay_ret_10', pd.Series(index=df.index, dtype='float32'))
            new_cols['alpha_decay_ret_10'].loc[idx] = calculate_decay_ret_10(c).astype('float32')

        if 'alpha_corr_cv_20' not in df.columns:
            new_cols.setdefault('alpha_corr_cv_20', pd.Series(index=df.index, dtype='float32'))
            new_cols['alpha_corr_cv_20'].loc[idx] = calculate_corr_cv_20(c, v).astype('float32')

        if 'alpha_tsrank_ret_20' not in df.columns:
            new_cols.setdefault('alpha_tsrank_ret_20', pd.Series(index=df.index, dtype='float32'))
            new_cols['alpha_tsrank_ret_20'].loc[idx] = calculate_tsrank_ret_20(c).astype('float32')

        if 'alpha_high_20d_ratio' not in df.columns:
            new_cols.setdefault('alpha_high_20d_ratio', pd.Series(index=df.index, dtype='float32'))
            new_cols['alpha_high_20d_ratio'].loc[idx] = calculate_high_20d_ratio(h).astype('float32')

        if 'alpha_close_open_gap' not in df.columns:
            new_cols.setdefault('alpha_close_open_gap', pd.Series(index=df.index, dtype='float32'))
            new_cols['alpha_close_open_gap'].loc[idx] = calculate_close_open_gap(c, o).astype('float32')

    # 添加新列
    for col_name, col_data in new_cols.items():
        df[col_name] = col_data.fillna(0)

    print(f"  新增 {len(new_cols)} 列: {list(new_cols.keys())}")
    print(f"  输出: {len(df):,} 行, {len(df.columns)} 列")

    # 保存
    df.to_parquet(output_path, index=False, engine='pyarrow', compression='snappy')
    print(f"  ✓ 已保存: {output_path.name}")


def main():
    print("=" * 70)
    print("补算旧版 Parquet 缺失因子")
    print("=" * 70)

    # 查找所有 yearly parquet
    yearly_files = sorted(DATA_DIR.glob("model_features_20*.parquet"))
    yearly_files = [f for f in yearly_files if "core" not in f.name and "hk" not in f.name and "us" not in f.name and "crypto" not in f.name]

    if not yearly_files:
        print(f"❌ 未找到 yearly parquet: {DATA_DIR}")
        sys.exit(1)

    print(f"\n找到 {len(yearly_files)} 个文件:")
    for f in yearly_files:
        size_mb = f.stat().st_size / (1024 * 1024)
        print(f"  {f.name}: {size_mb:.1f} MB")

    # 处理每个文件
    for f in yearly_files:
        # 创建临时输出路径
        temp_path = f.with_suffix('.parquet.tmp')
        try:
            process_parquet(f, temp_path)
            # 替换原文件
            temp_path.replace(f)
            print(f"  ✓ 已替换: {f.name}")
        except Exception as e:
            print(f"  ❌ 处理失败: {e}")
            if temp_path.exists():
                temp_path.unlink()
            continue

    print("\n" + "=" * 70)
    print("✅ 完成！请重新运行 rebuild_core_parquet_full.py")
    print("=" * 70)


if __name__ == "__main__":
    main()
