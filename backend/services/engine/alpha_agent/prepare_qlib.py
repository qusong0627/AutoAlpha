"""Prepare Qlib data in a cancellable child process owned by the mining task."""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result-file", type=Path, required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    logging.getLogger(__name__).info("正在准备 QuantDB / Qlib 数据")

    from backend.services.engine.rd_agent.rd_loop_wrapper import RDLoopWrapper
    from backend.services.engine.qlib_data_builder import ensure_qlib_cache

    quantdb_dir = RDLoopWrapper._resolve_quantdb_dir()
    provider_uri = ensure_qlib_cache(quantdb_dir) if quantdb_dir else "/app/db/feature_snapshots"
    fd = os.open(args.result_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as result:
        json.dump({"provider_uri": provider_uri}, result)


if __name__ == "__main__":
    main()
