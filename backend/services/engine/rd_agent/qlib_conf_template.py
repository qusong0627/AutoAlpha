"""qlib conf 模板渲染 + 分钟改写 —— 让 vendored RD-Agent 的 ``conf_*.yaml`` 真正可执行。

**为什么存在**（2026-10-10 实证，非猜测）：

``rdagent/scenarios/qlib/experiment/factor_template/conf_*.yaml`` 是 Jinja 模板
（``start_time: {{ train_start | default("2008-01-01", true) }}``、
``feature: - {{ feature_expressions }}`` …），但 vendored 树里**没有任何渲染环节**
——``QlibFBWorkspace.execute()`` 直接 ``qrun conf_*.yaml``。未渲染的模板不是
"日期缺省"，而是**非法数据**：YAML 把 ``{{ x | default(...) }}`` 解析成 flow
mapping（``{{ x }}`` → 嵌套映射），``Alpha158DL._parse_fields_info`` 随即抛
``NotImplementedError: This type of input is not supported``。

证据链（历史产物，与任何窗口/数据集配置无关的既有 bug）：

- ``/data/alpha_agent_logs/**`` 下有 104 份 ``conf_baseline.yaml``，全部仍是
  ``{{ ... }}`` 原文；全盘 **0 个** ``qlib_res.csv``；
- 任务日志反复出现 ``Qlib execution failed: Command failed (exit 1): qrun
  conf_baseline.yaml``；
- 容器内手工复现：渲染问题之外还叠了一层 mlflow 3.x 文件后端开关（见
  ``rd_loop_wrapper._configure_env`` 的 ``MLFLOW_ALLOW_FILE_STORE``）。

**渲染契约**：上下文 = ``factor_runner.develop()`` 传给 ``execute()`` 的
``run_env``（train/valid/test 分段 + ``feature_names``/``feature_expressions``，
这些键在模板里都带 ``| default(...)``，缺省合法）。产物写
``<stem>.rendered.yaml`` 与模板**并存**——workspace 里既保留原始模板，也留下
实际执行的配置（排障要看的是后者）。渲染失败**绝不**降级为"跑原始模板"
（那正是本模块要消灭的失败态），而是按 ``execute()`` 的失败契约返回
``(None, 错误信息)``，由 ``factor_runner`` 转成 ``FactorEmptyError`` 跳过本轮。

**分钟改写契约**（移植 P1.5）：任务 env ``QM_MINING_FREQ=1min`` 时，
渲染产物再走一次 :func:`apply_minute_overlay`——``qlib_init.provider_uri`` 换成
分钟 provider 目录（day 层 + 1min 层的叠加目录），每个 Alpha158DL loader 注入
``freq: "1min"``（freq 挂在 **loader** 上：``QlibDataLoader.__init__(…, freq=…)``
是表达式解析的频率来源，挂在 handler 上不生效）。改写产物写
``<stem>.minute.yaml``，qrun 执行的是它。

改写的**结构校验是硬闸**：conf 里找不到 ``qlib_init.provider_uri`` /
``task…data_loader`` / 任何 Alpha158DL 条目，一律抛
:class:`MinuteOverlayError`（``ConfTemplateError`` 子类）。绝不静默跳过——分钟
任务最坏的失败形态不是崩溃，而是**悄悄按日线跑完、产出一批自称分钟因子的
东西**。改写是"解析 → 校验 → 再序列化"的全量重写（丢注释/YAML 别名），原始
模板与 rendered 产物仍在 workspace 留存审计。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import yaml
from jinja2 import Environment, StrictUndefined

#: 渲染产物后缀：``conf_baseline.yaml`` → ``conf_baseline.rendered.yaml``
RENDERED_SUFFIX = ".rendered.yaml"

#: 分钟改写产物后缀：``conf_baseline.rendered.yaml`` → ``conf_baseline.minute.yaml``
MINUTE_SUFFIX = ".minute.yaml"

#: Alpha158 数据 loader 类名后缀（freq 注入点；class 值可能是全路径）
_ALPHA_LOADER_SUFFIX = "Alpha158DL"
#: 嵌套 loader 的类名后缀（条目在 kwargs.dataloader_l 里）
_NESTED_LOADER_SUFFIX = "NestedDataLoader"

#: StrictUndefined：模板里出现未预料的占位符时**报错**而不是渲染成空串
_JINJA = Environment(undefined=StrictUndefined, keep_trailing_newline=True)


class ConfTemplateError(RuntimeError):
    """conf 模板渲染失败（不可读 / 语法错 / 未定义变量 / 渲染后仍有占位符）。"""


class MinuteOverlayError(ConfTemplateError):
    """分钟改写失败（provider 未就绪 / conf 结构与预期不符）。

    继承 :class:`ConfTemplateError`：``patched_execute`` 的失败契约
    （返回 ``(None, 说明)`` → 上层转 ``FactorEmptyError``）对本类同样成立。
    """


def render_conf_template(template: str, context: Mapping[str, object]) -> str:
    """渲染模板文本；任何问题抛 :class:`ConfTemplateError`（不静默）。

    上下文值由 ``factor_runner`` 用 ``str(...)`` 生成（列表字面量），模板把它当
    YAML **flow 序列**消费（``- ['$close']``）。因子表达式若含引号会破坏该假设，
    但 qlib 表达式（``Ref($close, 1)/$close - 1`` 一类）不含引号——与上游同假设。
    """
    try:
        rendered = _JINJA.from_string(template).render(**context)
    except Exception as exc:  # jinja2.TemplateError / TypeError（上下文不可渲染）
        raise ConfTemplateError(f"conf 模板渲染失败: {exc}") from exc
    if "{{" in rendered or "{%" in rendered:
        raise ConfTemplateError("conf 模板渲染不完整：产物中仍残留 Jinja 占位符")
    return rendered


def render_conf_file(conf_path: Path, context: Mapping[str, object]) -> Path:
    """渲染 ``conf_path`` 到同目录 ``<stem>.rendered.yaml``，返回产物路径。"""
    try:
        template = conf_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfTemplateError(f"conf 模板不可读: {conf_path}（{exc}）") from exc
    rendered = render_conf_template(template, context)
    out_path = conf_path.with_name(conf_path.stem + RENDERED_SUFFIX)
    try:
        out_path.write_text(rendered, encoding="utf-8")
    except OSError as exc:
        raise ConfTemplateError(f"渲染产物不可写: {out_path}（{exc}）") from exc
    return out_path


def _loader_entries(data_loader: object) -> list[dict]:
    """从 data_handler 的 ``data_loader`` 配置取出 loader 条目列表。

    支持两种形状（本仓 conf 模板的实际形状 + 裸 loader 兜底）：

    - ``class: NestedDataLoader`` → ``kwargs.dataloader_l``（列表，非空）；
    - 顶层直接是单个 loader 条目（如裸 ``Alpha158DL``）。

    结构不符（不是 dict / 缺 kwargs / 列表为空）抛 :class:`MinuteOverlayError`
    ——调用方按"配置不可改写"处理，不猜。
    """
    if not isinstance(data_loader, dict):
        raise MinuteOverlayError(
            f"conf 的 data_handler.data_loader 不是映射（{type(data_loader).__name__}），无法改写为分钟配置"
        )
    cls = str(data_loader.get("class") or "")
    if cls.endswith(_NESTED_LOADER_SUFFIX):
        kwargs = data_loader.get("kwargs")
        entries = kwargs.get("dataloader_l") if isinstance(kwargs, dict) else None
        if not isinstance(entries, list) or not entries:
            raise MinuteOverlayError(
                "conf 的 NestedDataLoader 缺 kwargs.dataloader_l（或为空列表），无法改写为分钟配置"
            )
        for entry in entries:
            if not isinstance(entry, dict):
                raise MinuteOverlayError("NestedDataLoader.dataloader_l 含非映射条目，无法改写为分钟配置")
        return list(entries)
    return [data_loader]


def _inject_loader_freq(entry: dict, freq: str) -> dict:
    """返回注入了 ``freq`` 的 loader 条目副本（原 dict 不动）。"""
    kwargs = entry.get("kwargs")
    new_kwargs = {**(kwargs if isinstance(kwargs, dict) else {}), "freq": freq}
    return {**entry, "kwargs": new_kwargs}


def apply_minute_overlay(rendered_text: str, *, provider_uri: str, freq: str) -> str:
    """把已渲染的 conf 文本改写成分钟形态（provider_uri 换分钟目录 + loader 注 freq）。

    :param rendered_text: ``render_conf_template`` 的产物（合法 YAML）
    :param provider_uri: 分钟 provider 目录（绝对路径；由调用方
        ``minute_qlib_provider.resolve_minute_provider_dir()`` 解析）
    :param freq: qlib 频率串（``minute_qlib_provider.MINUTE_FREQ`` = ``"1min"``）
    :returns: 改写后的 YAML 文本（**新对象**，入参文本不变）

    纯函数：不做路径存在性/可用性判断（那是调用方的事），只保证"改出来的 conf
    一定带分钟 provider 与 loader freq"或"抛错"这两种结局。
    """
    provider_uri = str(provider_uri or "").strip()
    freq = str(freq or "").strip()
    if not provider_uri or not freq:
        raise MinuteOverlayError(f"分钟改写的 provider_uri/freq 不能为空（{provider_uri!r}/{freq!r}）")
    try:
        doc = yaml.safe_load(rendered_text)
    except yaml.YAMLError as exc:
        raise MinuteOverlayError(f"conf 不是合法 YAML，无法改写为分钟配置: {exc}") from exc
    if not isinstance(doc, dict):
        raise MinuteOverlayError("conf 顶层不是映射，无法改写为分钟配置")

    qlib_init = doc.get("qlib_init")
    if not isinstance(qlib_init, dict) or "provider_uri" not in qlib_init:
        raise MinuteOverlayError("conf 缺 qlib_init.provider_uri，无法改写为分钟配置")

    try:
        data_loader = doc["task"]["dataset"]["kwargs"]["handler"]["kwargs"]["data_loader"]
    except (KeyError, TypeError) as exc:
        raise MinuteOverlayError(
            "conf 缺 task.dataset.kwargs.handler.kwargs.data_loader，无法改写为分钟配置"
        ) from exc

    entries = [
        _inject_loader_freq(e, freq) if str(e.get("class") or "").endswith(_ALPHA_LOADER_SUFFIX) else e
        for e in _loader_entries(data_loader)
    ]
    if not any(str(e.get("class") or "").endswith(_ALPHA_LOADER_SUFFIX) for e in entries):
        raise MinuteOverlayError(
            f"conf 的 data_loader 里没有 {_ALPHA_LOADER_SUFFIX} 条目（特征表达式无处取分钟口径），"
            "拒绝按日线口径跑分钟任务"
        )

    new_loader = (
        {**data_loader, "kwargs": {**data_loader["kwargs"], "dataloader_l": entries}}
        if str(data_loader.get("class") or "").endswith(_NESTED_LOADER_SUFFIX)
        else entries[0]
    )
    # 逐层构造新对象（不原地改 parsed doc）：改写是显式的、可审计的
    new_conf = {
        **doc,
        "qlib_init": {**qlib_init, "provider_uri": provider_uri},
        "task": {
            **doc["task"],
            "dataset": {
                **doc["task"]["dataset"],
                "kwargs": {
                    **doc["task"]["dataset"]["kwargs"],
                    "handler": {
                        **doc["task"]["dataset"]["kwargs"]["handler"],
                        "kwargs": {
                            **doc["task"]["dataset"]["kwargs"]["handler"]["kwargs"],
                            "data_loader": new_loader,
                        },
                    },
                },
            },
        },
    }
    # 模板里 ``task…handler.kwargs`` 与顶层 ``data_handler_config`` 是同一个
    # YAML 锚点（``*data_handler_config``）。再序列化后别名关系不复原——若不
    # 同步改写顶层键，minute 文件里会留一份**日线口径**的 data_handler_config，
    # 既是"读文件的人被误导"的雷，也是将来有消费方读该键时的静默降级。
    if isinstance(doc.get("data_handler_config"), dict):
        new_conf["data_handler_config"] = {**doc["data_handler_config"], "data_loader": new_loader}
    return yaml.safe_dump(new_conf, sort_keys=False, allow_unicode=True)


def _is_minute_task() -> bool:
    """当前任务是否分钟模式（读 ``QM_MINING_FREQ``；非法值 → ``MinuteOverlayError``）。"""
    from backend.services.engine.alpha_agent.mining_config import FREQ_MINUTE, freq_from_env

    try:
        return freq_from_env() == FREQ_MINUTE
    except ValueError as exc:  # 频率串非法：显式失败，不按默认值日线跑
        raise MinuteOverlayError(f"任务挖掘频率非法: {exc}") from exc


def _write_minute_overlay(rendered: Path) -> Path:
    """把渲染产物改写成分钟配置，返回 ``<stem>.minute.yaml`` 路径。

    provider 就绪性在这里**硬校验**：分钟 provider（``QM_QLIB_MINUTE_DIR``，
    由 ``minute_qlib_provider`` 构建）不在场时直接失败——不存在"退回日线"分支。
    """
    from backend.services.engine import minute_qlib_provider as MP

    provider_dir = MP.resolve_minute_provider_dir()
    if not MP.is_minute_provider_ready(provider_dir):
        raise MinuteOverlayError(
            f"分钟 provider 未就绪: {provider_dir}"
            "（应先由 minute_qlib_provider 构建；见 rd_loop_wrapper._ensure_minute_provider）"
        )
    try:
        rendered_text = rendered.read_text(encoding="utf-8")
    except OSError as exc:
        raise MinuteOverlayError(f"渲染产物不可读: {rendered}（{exc}）") from exc
    overlay = apply_minute_overlay(
        rendered_text, provider_uri=str(provider_dir), freq=MP.MINUTE_FREQ
    )
    out_path = rendered.with_name(
        rendered.name.replace(RENDERED_SUFFIX, MINUTE_SUFFIX, 1)
        if rendered.name.endswith(RENDERED_SUFFIX)
        else rendered.stem + MINUTE_SUFFIX
    )
    try:
        out_path.write_text(overlay, encoding="utf-8")
    except OSError as exc:
        raise MinuteOverlayError(f"分钟配置不可写: {out_path}（{exc}）") from exc
    return out_path


def install_qlib_execute_patch() -> bool:
    """在 ``QlibFBWorkspace.execute`` 上补渲染（+ 分钟改写）环节（幂等）。

    ``factor_runner`` 的每一次实验执行（baseline / combined / sota_model）都经
    这个唯一入口，所以在这里补一次即可覆盖全部 conf。返回是否由本次调用完成
    安装（已安装过返回 False）。

    每次执行两步，任一失败都不跑 qrun（按 ``execute()`` 失败契约返回）：

    1. 渲染 Jinja 模板 → ``<stem>.rendered.yaml``；
    2. 任务为分钟模式（``QM_MINING_FREQ=1min``）时再改写 → ``<stem>.minute.yaml``，
       qrun 实际执行的是后者。日线任务跳过第 2 步，执行文件与旧行为逐字节一致。

    import 失败不抛：RD-Agent 不在场时（如纯数据面测试）调用方继续，由 qrun
    自身的失败被上层捕获。
    """
    import logging

    logger = logging.getLogger(__name__)
    try:
        from rdagent.scenarios.qlib.experiment.workspace import QlibFBWorkspace
    except Exception as exc:  # noqa: BLE001 — 无 rdagent 环境不拦主流程
        logger.error("Failed to patch qlib conf rendering: %s", exc)
        return False

    if getattr(QlibFBWorkspace, "_qm_conf_rendering_patched", False):
        return False
    original_execute = QlibFBWorkspace.execute

    def patched_execute(
        self, qlib_config_name: str = "conf.yaml", run_env: dict | None = None, *args, **kwargs
    ):
        context = run_env or {}
        try:
            rendered = render_conf_file(Path(self.workspace_path) / qlib_config_name, context)
            exec_conf = _write_minute_overlay(rendered) if _is_minute_task() else rendered
        except MinuteOverlayError as exc:
            logger.error("Qlib 分钟配置准备失败（%s）: %s", self.workspace_path, exc)
            return None, f"分钟配置准备失败: {exc}"
        except ConfTemplateError as exc:
            logger.error("Qlib conf 渲染失败（%s）: %s", self.workspace_path, exc)
            # 与 execute() 的失败契约同形（(None, 说明) → 上层转 FactorEmptyError）
            return None, f"conf 模板渲染失败: {exc}"
        return original_execute(
            self, qlib_config_name=exec_conf.name, run_env=context, *args, **kwargs
        )

    QlibFBWorkspace.execute = patched_execute
    QlibFBWorkspace._qm_conf_rendering_patched = True
    logger.info("Patched RD-Agent QlibFBWorkspace.execute (Jinja conf rendering)")
    return True
