"""Alpha360 —— 原始量价 60 日回溯因子库（360 个，Qlib 原生定义）。

CLOSE{d}/OPEN{d}/HIGH{d}/LOW{d}/VWAP{d} = 字段 t−d 值 / 当日 close（归一）；
VOLUME{d} = volume t−d / 当日 volume。d = 0..59（0=当日）。
因为 360 列全量驻留约 20GB，本模块按「日」流式产出（每日一帧 symbol × 360）。

与 zoo/stock qlib 的差异：vwap 用 QuantDB 推算（amount 口径，与 alpha_library 同源）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

FIELDS = (
    ("CLOSE", "close"),
    ("OPEN", "open"),
    ("HIGH", "high"),
    ("LOW", "low"),
    ("VWAP", "vwap"),
    ("VOLUME", "volume"),
)
N_DAYS = 60


def column_names() -> list[str]:
    return [f"{prefix}{d}" for prefix, _ in FIELDS for d in range(N_DAYS)]


def iter_partitions(daily: dict[str, pd.DataFrame], vwap: pd.DataFrame):
    """逐日产出 (Timestamp, DataFrame[columns=symbol, 360 列 float32])。

    daily: {close/open/high/low/volume: 宽表}；vwap: 宽表（元，同日 close 基准）。
    """
    fields = dict(daily)
    fields["vwap"] = vwap
    syms = daily["close"].columns
    arrs = {p: fields[s].to_numpy(dtype=np.float64) for p, s in FIELDS}
    base_close = arrs["CLOSE"]
    base_vol = arrs["VOLUME"]
    dates = daily["close"].index.to_numpy()
    t = len(dates)
    for i in range(t):
        blocks = []
        for prefix, _ in FIELDS:
            a = arrs[prefix]
            lo = max(0, i - N_DAYS + 1)
            win = a[lo : i + 1][::-1]  # (<=60, N)：d=0 为当日
            if win.shape[0] < N_DAYS:  # 历史不足补 NaN 行
                pad = np.full((N_DAYS - win.shape[0], a.shape[1]), np.nan)
                win = np.vstack([win, pad])
            base = base_vol[i] if prefix == "VOLUME" else base_close[i]
            with np.errstate(divide="ignore", invalid="ignore"):
                blocks.append(win / np.where(np.abs(base) > 0, base, np.nan))
        mat = np.concatenate(blocks, axis=0)  # (360, N) 字段主序，与 column_names 对齐
        yield (
            pd.Timestamp(dates[i]),
            pd.DataFrame(mat.T.astype(np.float32), index=syms, columns=column_names()),
        )
