#!/usr/bin/env python3
"""
更新特征字典 JSON 文件 - 只保留 78 个核心因子
"""

import json
from pathlib import Path

# 核心因子列表 (78个)
CORE_FACTORS = {
    # 基础行情 (6个)
    "基础行情": [
        "close", "open", "high", "low", "volume", "amount"
    ],

    # 动量 (12个)
    "动量": [
        "mom_ret_1d", "mom_ret_5d", "mom_ret_20d", "mom_ret_60d",
        "mom_ma_gap_5", "mom_ma_gap_20", "mom_ma_gap_60",
        "mom_macd_hist", "mom_rsi_14", "mom_kdj_k",
        "mom_breakout_20d", "mom_sharpe_20"
    ],

    # 波动率 (10个)
    "波动率": [
        "vol_std_20", "vol_std_60", "vol_atr_14",
        "vol_parkinson_20", "vol_downside_20", "vol_upside_20",
        "vol_realized_rv", "vol_realized_rrv", "vol_realized_rskew",
        "vol_jump_zadj"
    ],

    # 成交量 (6个)
    "成交量": [
        "liq_volume", "liq_amount", "liq_turnover_os",
        "liq_volume_ratio_5", "liq_mfi_14", "liq_amihud_20"
    ],

    # 资金流 (7个)
    "资金流": [
        "flow_net_amount", "flow_net_amount_ratio", "flow_large_net_amount",
        "flow_vpin", "flow_vpin_ma_5", "flow_vpin_ma_20",
        "flow_pressure_index"
    ],

    # 风格因子 (8个)
    "风格因子": [
        "style_ln_mv_total", "style_ln_mv_float", "style_beta_20",
        "style_beta_60", "style_idio_vol_20", "style_residual_ret_20",
        "style_bp", "style_ep_ttm"
    ],

    # 行业因子 (4个)
    "行业因子": [
        "ind_ret_1d", "ind_ret_20d", "ind_strength_20",
        "ind_momentum_rank_20"
    ],

    # 技术形态 (11个)
    "技术形态": [
        "kline_kmid", "kline_klen", "kline_kup", "kline_klow",
        "kline_ksft", "prel_open0", "prel_high0", "prel_low0",
        "prel_vwap0", "tech_bollinger_position", "tech_cci_20"
    ],

    # Alpha因子 (9个)
    "Alpha因子": [
        "alpha_decay_ret_10", "alpha_corr_cv_20", "alpha_tsrank_ret_20",
        "alpha_tsrank_volume_20", "alpha_high_20d_ratio", "alpha_low_20d_ratio",
        "alpha_close_open_gap", "fund_pe_percentile", "fund_pb_percentile"
    ],

    # 趋势质量 (5个)
    "趋势质量": [
        "trend_r2_20", "trend_slope_20", "consecutive_updown_5",
        "pv_corr_20", "pv_divergence_20"
    ],
}

def main():
    # 读取原始文件
    input_file = Path("/workspace/quantmind/config/features/model_training_feature_catalog_v1.json")
    with open(input_file) as f:
        catalog = json.load(f)

    # 创建新的分类列表
    new_categories = []
    total_count = 0

    for cat_name, factor_keys in CORE_FACTORS.items():
        # 查找原始分类中的特征定义
        features = []
        for order_no, key in enumerate(factor_keys, 1):
            # 尝试从原始文件中找到该特征的定义
            original_feat = None
            for orig_cat in catalog.get("categories", []):
                for feat in orig_cat.get("features", []):
                    if feat.get("key") == key:
                        original_feat = feat
                        break
                if original_feat:
                    break

            if original_feat:
                # 使用原始定义，但确保 enabled=True
                features.append({
                    **original_feat,
                    "enabled": True,
                    "order_no": order_no,
                })
            else:
                # 创建新的特征定义
                features.append({
                    "key": key,
                    "name": key.replace("_", " ").title(),
                    "description": f"核心因子: {key}",
                    "enabled": True,
                    "order_no": order_no,
                    "formula": "",
                    "source": "parquet",
                })

        new_categories.append({
            "id": cat_name.lower().replace(" ", "_"),
            "name": cat_name,
            "order": len(new_categories),
            "feature_count": len(features),
            "features": features,
        })
        total_count += len(features)

    # 创建新的 catalog
    new_catalog = {
        "version": "v2_core",
        "version_name": "核心因子集 (78个)",
        "description": "从197个因子中精选的78个核心因子，去冗余、高IC",
        "feature_count": total_count,
        "categories": new_categories,
        "source": "file",
        "metadata": {
            "source": "core_factors_selection",
            "created_at": "2026-06-23",
            "total_original": 197,
            "total_core": total_count,
            "reduction": f"{(1 - total_count/197)*100:.1f}%",
        }
    }

    # 写入新文件
    output_file = Path("/workspace/quantmind/config/features/model_training_feature_catalog_v2_core.json")
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(new_catalog, f, ensure_ascii=False, indent=2)

    print(f"✅ 已生成核心因子目录: {output_file}")
    print(f"   总因子数: {total_count}")
    print(f"   分类数: {len(new_categories)}")

    # 更新符号链接或重命名
    backup_file = input_file.with_suffix(".json.bak")
    if not backup_file.exists():
        input_file.rename(backup_file)
        print(f"   备份原文件: {backup_file}")

    # 创建新文件为默认
    output_file.rename(input_file)
    print(f"   已更新默认文件: {input_file}")

    # 打印统计
    print("\n📊 各分类因子数:")
    for cat in new_categories:
        print(f"   {cat['name']}: {len(cat['features'])} 个")

if __name__ == "__main__":
    main()
