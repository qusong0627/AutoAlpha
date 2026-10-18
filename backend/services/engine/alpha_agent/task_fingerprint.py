"""挖掘任务去重指纹（移植 P3）——「同配置重复提交」的唯一判据。

背景（参考实现的 ``find_duplicate_mining`` 同型）：挖掘是钱（LLM token +
GPU 位 + 子进程名额），用户手滑双击、或对同一配置连点「开始挖掘」，都会
再建一个**跑不出任何新东西**的同参任务。去重语义：

- 指纹 = **决定这次挖掘跑什么**的全部请求参数的规范哈希（字段表见
  :func:`canonical_params`）；
- 只对**活跃**任务（pending/queued/running）去重——任务收终态后同配置
  重跑是正当行为（复盘、数据更新后重挖），不拦；
- 命中即**复用既有任务**（回执带既有 task_id），不新建、不排队、不占名额。

不含在指纹里的字段与理由：

- ``direction_meta``：加权抽样的**复现凭证**（seed/权重），是结果不是参数，
  同参数重抽一次抽样证据不同不该产生「新任务」；
- ``seed`` / ``llm_overrides``：前者是调试复现输入、后者是**凭据**——凭据
  绝不进哈希（换 API Key 不是换任务）；
- ``tenant_id``：用户身份已由 ``(user_id, fingerprint)`` 索引承担。

两个进程内并发的同指纹提交都会通过「先查后建」的预检（互相看不见），
最终由 ``rd_agent_mining_tasks`` 的部分唯一索引兜底——见 task_store。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any


def _clean(value: object) -> str:
    """None/空白 → ``""``；其余 strip 后原样（None 与 "" 是同一件事）。"""
    return str(value or "").strip()


def canonical_params(
    *,
    algorithm: str,
    market: str,
    universe: str,
    data_source: str | None,
    direction: str | None,
    direction_mode: str | None,
    quality_gate_mode: str | None,
    loop_n: int,
    freq: str | None,
    mining_start: str | None,
    mining_end: str | None,
    valid_start: str | None,
    valid_end: str | None,
    datasets: Sequence[str] | None,
    source: str,
    doc_id: str | None,
) -> dict[str, Any]:
    """请求参数 → 规范字典（指纹与任务行 ``params_snapshot`` 的**唯一**来源）。

    入参应当是**已归一**的值（算法已 resolve、窗口已退让/物化、数据集已按
    登记序 resolve）——调用点统一在 launcher 建任务处，保证「预览所见 =
    指纹所算 = 实跑所用」。方向文本原样参与（不做大小写/空白归一之外的
    语义猜测：换一字就是另一个研究假设）。

    指纹与快照同源是有意的：任务行存的快照必须**逐字段就是**指纹哈希的那份
    内容，否则「为什么这次没去重」在事后无法从任务行自证。
    """
    return {
        "algorithm": _clean(algorithm),
        "market": _clean(market),
        "universe": _clean(universe),
        "data_source": _clean(data_source),
        "direction": _clean(direction),
        "direction_mode": _clean(direction_mode),
        "quality_gate_mode": _clean(quality_gate_mode),
        "loop_n": int(loop_n),
        "freq": _clean(freq),
        "mining_start": _clean(mining_start),
        "mining_end": _clean(mining_end),
        "valid_start": _clean(valid_start),
        "valid_end": _clean(valid_end),
        # 数据集是集合不是序列：勾选顺序不同命中同一指纹（与 h5 缓存按
        # 集合取名的口径一致）
        "datasets": sorted({_clean(d) for d in (datasets or ()) if _clean(d)}),
        "source": _clean(source) or "text",
        "doc_id": _clean(doc_id),
    }


def fingerprint_of(canonical: Mapping[str, Any]) -> str:
    """:func:`canonical_params` 的产物 → sha256 十六进制指纹（同参恒同指纹）。

    键序不参与（``sort_keys``）：指纹是**内容**哈希，不是序列化格式的哈希——
    加字段/调字段顺序只该在真的改了「跑什么」时改变指纹。
    """
    blob = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
