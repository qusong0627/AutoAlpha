"""一次性脚本：重建非 A 股市场的 qlib 缓存 features 目录。

背景：qlib 的 FileFeatureStorage 强制 instrument.lower() 拼路径，而旧缓存
用原始大小写写目录（hk_00700.HK / us_AAPL / bc_AAVEUSDT），导致特征读取
静默返回空。修复已并入 qlib_data_builder._feat_dir_name（非 CN 小写）。
本脚本把旧 features 目录改名备份后走 build_features_bulk 重建。

用法（容器内）：
    docker exec quantmind python /app/backend/scripts/rebuild_noncn_qlib_cache.py [MARKET...]
    MARKET: HK US CRYPTO FUTURES（默认全部）
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

# 本容器 ENABLE_CRYPTO=false 隐藏了 crypto 市场，但数据目录实际存在；
# 重建属于运维操作，先恢复启用再解析目录
os.environ["ENABLE_CRYPTO"] = "true"

sys.path.insert(0, "/app")

from backend.services.engine.qlib_data_builder import QlibDataBuilder

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
logger = logging.getLogger("rebuild_noncn_cache")

MARKETS = ["HK", "US", "CRYPTO", "FUTURES"]


def rebuild(market: str) -> None:
    builder = QlibDataBuilder.for_market(market)
    qlib_dir = builder.qlib_dir
    features_dir = qlib_dir / "features"
    logger.info("=== %s: qlib_dir=%s ===", market, qlib_dir)

    if not builder.hub.available:
        logger.warning("%s 数据目录不可用，跳过", market)
        return

    # 确保日历和 instruments 是最新的
    n_cal = builder.build_calendar()
    n_inst = builder.build_instruments()
    logger.info("%s: calendar=%d, instruments=%d", market, n_cal, n_inst)

    # 备份旧 features（保留一次回滚机会）
    backup = qlib_dir / "features.old_case"
    if features_dir.is_dir():
        if backup.exists():
            shutil.rmtree(backup)
        shutil.move(str(features_dir), str(backup))
        logger.info("%s: 旧 features 已备份到 %s", market, backup)

    result = builder.build_features_bulk()
    logger.info("%s: 重建完成 %s", market, result)

    # 校验：抽样一个目录，确认小写且 bin 可读
    features_dir.mkdir(parents=True, exist_ok=True)
    samples = sorted(p.name for p in features_dir.iterdir() if p.is_dir())[:3]
    for s in samples:
        if s != s.lower():
            logger.error("%s: 发现大写目录 %s，重建未生效！", market, s)
        else:
            close_bin = features_dir / s / "close.day.bin"
            logger.info("%s: 校验 %s -> close.day.bin %s", market, s,
                        f"{close_bin.stat().st_size} bytes" if close_bin.exists() else "缺失")
    logger.info("=== %s 完成 ===", market)


def main() -> int:
    targets = [m.upper() for m in sys.argv[1:]] or MARKETS
    for market in targets:
        if market not in MARKETS:
            logger.error("未知市场: %s（可选: %s）", market, ", ".join(MARKETS))
            return 2
    for market in targets:
        try:
            rebuild(market)
        except Exception:
            logger.exception("%s 重建失败", market)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
