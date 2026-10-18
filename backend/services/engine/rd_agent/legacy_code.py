"""存量因子代码迁移 shim —— 旧 ``daily_pv.h5`` 读法 → QuantDB 取数（H5 退役）.

旧挖出因子代码里的 ``pd.read_hdf('daily_pv.h5', key='data')`` 无法在新执行环境
复算。本模块在执行前做无损正则迁移（参考 superalpha 同名模块）：

* ``pd.read_hdf('daily_pv.h5', key='data')`` → ``qd.panel()``（同形
  MultiIndex[datetime, instrument] + ``$`` 前缀列，语义零变化）；
* 自动补 ``import quantdb_data as qd``。

``result.to_hdf('result.h5', ...)`` 不强改：执行器读回时兼容旧产物（新代码一律
产出 ``result.parquet``）。迁移只在执行前进行（不回写库）；库内代码由一次性
迁移脚本改写。
"""

from __future__ import annotations

import re

# 覆盖平台生成代码的多种形态：key='data' 关键字与位置参数、可选 ./ 前缀、
# 可选 pd. 前缀（from pandas import read_hdf 的写法）
_RE_H5_READ = re.compile(
    r"(?:pd\.)?read_hdf\s*\(\s*['\"](?:\./)?daily_pv\.h5['\"]\s*(?:,\s*(?:key\s*=\s*)?['\"]data['\"]\s*)?\)"
)

_RE_H5_WRITE = re.compile(
    r"\.to_hdf\s*\(\s*['\"]result\.h5['\"]"
    r"(?:\s*,\s*key\s*=\s*['\"]data['\"])?"
    r"(?:\s*,\s*mode\s*=\s*['\"]w['\"])?"
    r"\s*\)"
)


def migrate_legacy_code(code: str) -> tuple[str, bool]:
    """迁移旧因子代码读侧；返回 ``(新代码, 是否发生变化)``。"""
    if not code:
        return code, False
    new = _RE_H5_READ.sub("qd.panel()", code)
    if new == code:
        return code, False
    if "import quantdb_data" not in new:
        lines = new.splitlines(keepends=True)
        insert = 0
        for i, line in enumerate(lines):
            if line.startswith(("import ", "from ")):
                insert = i + 1
        lines.insert(insert, "import quantdb_data as qd\n")
        new = "".join(lines)
    return new, True


def migrate_legacy_write(code: str) -> tuple[str, bool]:
    """迁移写侧 ``result.h5`` → ``result.parquet``；返回 ``(新代码, 是否变化)``。"""
    if not code or "result.h5" not in code:
        return code, False
    new = _RE_H5_WRITE.sub(".to_parquet('result.parquet')", code)
    return new, new != code
