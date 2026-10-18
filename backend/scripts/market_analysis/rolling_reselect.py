#!/usr/bin/env python3
"""因子概率板滚动重选 + OOS 回填 CLI（P2-f，细案 §七）。

beat ``factor-board-reselect``（每月 1–12 日工作日 08:50）调本脚本；任务体
判「最近可用因子分区在上一自然月 → 今天是应用月首个交易日」，非月初静默跳过
（退码 0）。历史回填用 ``--as-of``（训练窗尾 ISO/紧凑日期）。

用法（容器内，日频自动）：
  python backend/scripts/market_analysis/rolling_reselect.py
用法（宿主/容器，回填历史月份，依次 08-31 → 09-30 → 10-31…）：
  python backend/scripts/market_analysis/rolling_reselect.py --as-of 2026-08-31
  python backend/scripts/market_analysis/rolling_reselect.py --as-of 2026-09-30

产物：{board_dir}/rolling/{YYYY-MM}.json（应用月台本；OOS 由下月运行回填）。
退码：0 成功/跳过 / 1 失败（硬门不过）/ 2 参数错误。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.shared.factor_board import composite as CP  # noqa: E402
from backend.shared.factor_board import config as C  # noqa: E402
from backend.shared.factor_board import rolling as ROLL  # noqa: E402
from backend.shared.factor_board.builder import BuildError  # noqa: E402

log = logging.getLogger("rolling_reselect")


def main() -> int:
    parser = argparse.ArgumentParser(description="因子概率板滚动重选（月频）")
    parser.add_argument(
        "--data-dir", default=None, help="QuantDB 数据根（默认 env/容器自动解析）"
    )
    parser.add_argument(
        "--board-dir", default=None, help="产物目录（默认 env/容器自动解析）"
    )
    parser.add_argument(
        "--as-of",
        default=None,
        help="训练窗尾（'YYYY-MM-DD' 或 'YYYYMMDD'；缺省=最近一个 < 今日的分区）。"
        "显式给出=历史回填，不做月初判据",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="缺省路径下跳过「今天是应用月首个交易日」判据（调试用）",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    data_dir = C.resolve_quantdb_root(args.data_dir)
    board_dir = C.resolve_board_dir(args.board_dir)
    if not data_dir.is_dir():
        print(f"[error] QuantDB 数据根不存在: {data_dir}", file=sys.stderr)
        return 2

    try:
        if args.as_of is None and not args.force:
            dates_all = CP.list_factor_dates(data_dir)
            trigger, last = ROLL.is_month_start(dates_all, date.today())
            if not trigger:
                print(
                    json.dumps(
                        {
                            "skipped": True,
                            "reason": "非应用月首个交易日",
                            "latest_prior_date": last,
                        },
                        ensure_ascii=False,
                    )
                )
                return 0
        summary = ROLL.run_rolling(data_dir, board_dir, as_of=args.as_of)
    except BuildError as exc:
        log.error("滚动重选失败（硬门）: %s", exc)
        return 1
    except (FileNotFoundError, LookupError, ValueError, RuntimeError) as exc:
        log.error("滚动重选失败: %s", exc, exc_info=True)
        return 1

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
