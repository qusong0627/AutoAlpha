"""因子概率板（市场分析 P0）：因子精选 × 合成 × 三层共振 → 每日离线产物。

入口：``builder.build_board(quantdb_root, ...)`` → ``board_io.write_board``；
CLI：``python backend/scripts/market_analysis/build_factor_board.py``。
"""

from backend.shared.factor_board.board_io import (
    available_dates,
    load_board,
    load_members_index,
    validate_board,
    write_board,
    write_members_index,
)
from backend.shared.factor_board.builder import BuildError, build_board

__all__ = [
    "BuildError",
    "available_dates",
    "build_board",
    "load_board",
    "load_members_index",
    "validate_board",
    "write_board",
    "write_members_index",
]
