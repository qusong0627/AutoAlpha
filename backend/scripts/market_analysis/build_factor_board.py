#!/usr/bin/env python3
"""因子概率板每日产物 CLI（市场分析 P0，细案 §六）。

用法（容器内）：
  docker exec quantmind python backend/scripts/market_analysis/build_factor_board.py
用法（宿主开发，CSV 注入模型信号）：
  python3 backend/scripts/market_analysis/build_factor_board.py \
      --signals-csv /tmp/qm_model_signals.csv --model-date 2026-10-12

产物：{out_dir}/{decision_date}.json + latest.json（原子写；同日重跑覆盖）。
退码：0 成功 / 1 构建失败（硬门不过，不落盘）/ 2 参数错误。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.shared.factor_board import config as C  # noqa: E402
from backend.shared.factor_board import write_board, write_members_index  # noqa: E402
from backend.shared.factor_board.builder import BuildError, build_board  # noqa: E402

log = logging.getLogger("build_factor_board")


def _summary(board: dict) -> dict:
    meta = board["meta"]
    coverage = meta["coverage"]
    sectors = board["sectors"]
    verdict_count = {}
    for key, payload in sectors.items():
        counts: dict[str, int] = {}
        for item in payload["items"]:
            counts[item["verdict"]] = counts.get(item["verdict"], 0) + 1
        verdict_count[key] = counts
    return {
        "decision_date": meta["decision_date"],
        "trade_date": meta["trade_date"],
        "selected": coverage["selected"],
        "gated": coverage["gated"],
        "composite_n": coverage["composite_n"],
        "model_cov_n": coverage["model_cov_n"],
        "spearman": board["resonance"]["spearman"],
        "validation": board["validation"],
        "verdicts": verdict_count,
        "warnings": meta["warnings"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="构建因子概率板每日产物")
    parser.add_argument(
        "--data-dir", default=None, help="QuantDB 数据根（默认 env/容器自动解析）"
    )
    parser.add_argument(
        "--out-dir", default=None, help="产物目录（默认 env/容器自动解析）"
    )
    parser.add_argument(
        "--model-date",
        default=None,
        help="模型信号目标交易日 YYYY-MM-DD（默认取 PG 最新 batch/CN）",
    )
    parser.add_argument(
        "--signals-csv",
        default=None,
        help="离线模型信号 CSV（列: symbol,fusion_score,rank_pct,signal_side），"
        "宿主开发用；给了它就不连 PG",
    )
    parser.add_argument("--dry-run", action="store_true", help="只算不落盘")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )

    data_dir = C.resolve_quantdb_root(args.data_dir)
    out_dir = C.resolve_board_dir(args.out_dir)
    if not data_dir.is_dir():
        print(f"[error] QuantDB 数据根不存在: {data_dir}", file=sys.stderr)
        return 2
    if args.signals_csv and not Path(args.signals_csv).is_file():
        print(f"[error] signals CSV 不存在: {args.signals_csv}", file=sys.stderr)
        return 2

    log.info("data_dir=%s out_dir=%s", data_dir, out_dir)
    members_out: dict = {}
    try:
        board = build_board(
            data_dir,
            model_date=args.model_date,
            signals_csv=args.signals_csv,
            board_dir=out_dir,
            members_out=members_out,
        )
    except BuildError as exc:
        log.error("构建失败（硬门）: %s", exc)
        return 1
    except (FileNotFoundError, LookupError, ValueError) as exc:
        log.error("构建失败: %s", exc, exc_info=True)
        return 1

    summary = _summary(board)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    if args.dry_run:
        print("[dry-run] 未落盘")
        return 0
    try:
        dated, latest = write_board(board, out_dir)
    except ValueError as exc:
        log.error("产物校验失败: %s", exc)
        return 1
    print(f"[ok] 产物: {dated}；latest: {latest}")
    if members_out:
        try:
            mpath = write_members_index(members_out, out_dir)
            n_sectors = sum(len(v) for v in members_out["sectors"].values())
            print(f"[ok] 板块成分明细: {mpath}（{n_sectors} 个板块）")
        except (ValueError, OSError) as exc:
            log.warning("板块成分明细落盘失败（板已落盘，钻取端点将 404）: %s", exc)
    else:
        log.warning("板块成分明细为空（生成异常），未落盘")
    return 0


if __name__ == "__main__":
    sys.exit(main())
