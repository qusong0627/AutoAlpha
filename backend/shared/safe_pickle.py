"""受限 pickle 反序列化 —— 读取挖掘载荷（LLM 生成代码产出的 pkl）的统一机制。

## 为什么需要

alpha_agent 挖掘子进程按设计执行 LLM 生成的代码，其写入日志工作区的 pkl
载荷视同不可信输入。裸 ``pickle.load`` 会在 ``find_class`` 处按载荷里的类
路径 import + 取属性（``__reduce__`` 可触发任意调用），因此主进程反序列化
这些载荷必须走白名单收口。

## 白名单口径（真样本全体实测钉死，2026-10-12）

对日志根全部 5555 个 pkl 做 opcode 级普查（pickletools，从不反序列化），
五个真实读取站点的 glob 真读集 1619 文件，引用类全集 = 7 类：

    datetime.datetime                        272 文件（time_info 计时）
    rdagent.components.coder.factor_coder.factor.FactorTask      68
    rdagent.core.experiment.RunningInfo                          51
    rdagent.components.coder.factor_coder.factor.FactorFBWorkspace  51
    target_task.FactorTask                                       51
    pathlib.PosixPath                                            51
    rdagent.core.proposal.HypothesisFeedback                     51

``target_task`` 根为兼容性所需：coder result 载荷里的 ``workspace.target_task``
是挖掘工作目录下生成模块（``target_task.py``）的实例（读取集 51 文件载此类），
不放行则这些文件整体反序列化失败、被静默跳过。实测注记（2026-10-12）：当前
语料里剔除该根，wrapper 因子提取的**名称集**无差异（同名因子可从同批其它文件
恢复），故它对现有聚合输出非承重、属防误伤放行；但单文件可读性直接受它
决定（放行后该文件 LOAD OK），未来任何依赖这些文件的读取面都会因剔除而
静默丢数。另加 builtins 数据容器/标量 16 名（见 ``ALLOWED_PICKLE_BUILTINS``）。

## 使用面

- ``load_pickle``：**抛异常**的裸机制（受限反序列化 + 单次有界读取），
  调用方自行决定降级策略（告警 + None / continue / debug 日志）；
- 展示链路（``trajectory_store.default_load_pickle``）在机制之上只加
  「告警 + None」策略层。

新增任何读取挖掘载荷的站点一律接本模块，禁止再写裸 ``pickle.load``。
（torch/pandas 等受信本地产物的模型文件不走本模块。）
"""

from __future__ import annotations

import io
import pickle
from pathlib import Path
from typing import Any

__all__ = [
    "ALLOWED_PICKLE_BUILTINS",
    "ALLOWED_PICKLE_MODULE_ROOTS",
    "MAX_PICKLE_BYTES",
    "PayloadTooLargeError",
    "RestrictedUnpickler",
    "is_allowed_pickle_class",
    "load_pickle",
]

#: 单载荷大小上限（超限不解析）。真样本最大 ≈0.9MiB（2026-10-12 实测），
#: 32MiB 只拦病态/注入的超大载荷，正常轨迹永不触达。
MAX_PICKLE_BYTES = 32 * 1024 * 1024

#: 白名单 · 模块根：只认上述实测量出的数据类来源。判定按第一段精确匹配，
#: 前缀相似不感染（``rdagentx`` 不通过）。其余模块一律拒绝。
ALLOWED_PICKLE_MODULE_ROOTS = frozenset(
    {"rdagent", "datetime", "pathlib", "collections", "target_task"}
)

#: 白名单 · builtins：数据容器与标量（eval/exec/getattr 等一切可执行名拒绝）
ALLOWED_PICKLE_BUILTINS = frozenset(
    {
        "NoneType",
        "bool",
        "bytearray",
        "bytes",
        "complex",
        "dict",
        "float",
        "frozenset",
        "int",
        "list",
        "object",
        "range",
        "set",
        "slice",
        "str",
        "tuple",
    }
)


def is_allowed_pickle_class(module: str, name: str) -> bool:
    """白名单判定（纯函数）：模块根 + builtins 名双层收口。

    常量按模块全局名实时读取（不冻结到闭包），测试可整体替换白名单。
    """
    root = str(module).split(".", 1)[0]
    if root == "builtins":
        return name in ALLOWED_PICKLE_BUILTINS
    return root in ALLOWED_PICKLE_MODULE_ROOTS


class RestrictedUnpickler(pickle.Unpickler):
    """白名单反序列化器：``find_class`` 级拦截，白名单外直接拒绝。

    载荷由挖掘子进程写入（该子进程按设计执行 LLM 生成代码）——主进程只
    反序列化展示/归因所需的数据类，绝不让载荷里的 ``__reduce__`` 类路径
    在调用进程触发任意调用。
    """

    def find_class(self, module: str, name: str) -> Any:
        if not is_allowed_pickle_class(module, name):
            raise pickle.UnpicklingError(f"载荷读取白名单拒绝：{module}.{name}")
        return super().find_class(module, name)


class PayloadTooLargeError(Exception):
    """载荷超过 ``MAX_PICKLE_BYTES``（单次有界读取后即弃，不解析）。"""


def load_pickle(path: str | Path) -> Any:
    """受限反序列化 + 单次有界读取；失败**抛异常**（调用方决定降级策略）。

    有界读取 = ``read(MAX_PICKLE_BYTES + 1)``：先 stat 再 open 的 TOCTOU
    已消除；写入侧持续增长也只读上限 +1 字节。``OSError``（不存在/无权限）
    与 ``pickle.UnpicklingError``（白名单拒绝/损坏）原样上抛。
    """
    p = Path(path)
    with p.open("rb") as fh:
        raw = fh.read(MAX_PICKLE_BYTES + 1)
    if len(raw) > MAX_PICKLE_BYTES:
        raise PayloadTooLargeError(
            f"载荷超上限（>{MAX_PICKLE_BYTES} 字节），跳过解析：{p}"
        )
    return RestrictedUnpickler(io.BytesIO(raw)).load()
