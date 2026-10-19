"""因子挖掘 LLM 环境配置 — 统一打通 RD-Agent litellm 后端。

RD-Agent 因子挖掘用 litellm 作为 LLM 后端（rdagent/oai/backend/litellm.py）：
  - LiteLLMSettings env_prefix="LITELLM_" → 读 LITELLM_CHAT_MODEL / LITELLM_OPENAI_API_KEY / LITELLM_OPENAI_API_BASE
  - litellm.completion() 内部还从标准 OPENAI_API_KEY / OPENAI_API_BASE 读 HTTP 认证

但 launcher / rd_loop_wrapper / market_adapters 只设置无前缀变量（OPENAI_API_KEY 等），
且 DEEPSEEK_API_KEY 未接入，导致因子挖掘子进程无法调用任何 LLM（factor 表 0 条）。

本模块统一构建两套变量：
  LITELLM_*（RD-Agent settings 对象用）+ OPENAI_*（litellm HTTP 层用）
优先 DeepSeek（DEEPSEEK_API_KEY），回退现有 AI_IDE/讯飞 MaaS 配置。

向量检索（embedding）是**独立通道**，不在这里构造：
  - 变量名 EMBEDDING_MODEL / EMBEDDING_BASE_URL / EMBEDDING_API_KEY（容器级 .env 已有
    SiliconFlow 默认值），由 rdagent/oai/utils/embedding.py:resolve_embedding_channel 直读。
  - EMBEDDING_* 配置直接来自服务端环境，子进程继承白名单变量。
  chat 与 embedding 必须能分开选：DeepSeek 不提供 embedding 接口，而 bge-m3 这类
  中文向量模型通常来自 SiliconFlow 或本地服务。
"""

from __future__ import annotations

import os
from typing import Any

# docker-compose 占位符，视为未配置
_PLACEHOLDER_KEYS = {"your-deepseek-api-key", "mock-api-key", "mock-api-key-not-configured"}


def _is_placeholder(key: str) -> bool:
    k = (key or "").strip().lower()
    return (not k) or any(p in k for p in _PLACEHOLDER_KEYS) or k.startswith("sk-在此")


def build_llm_env(env: dict[str, Any]) -> dict[str, Any]:
    """在现有 env 基础上补齐 litellm 需要的 LITELLM_ + OPENAI_ 变量。

    API key 优先级：DEEPSEEK_API_KEY（非占位） > LITELLM_OPENAI_API_KEY > AI_IDE_LLM_API_KEY > AI_IDE_API_KEY > OPENAI_API_KEY
    模型优先级：   已解析的 LITELLM_CHAT_MODEL > DEEPSEEK（若 key 生效） > CHAT_MODEL
    """
    deepseek_key = (
        os.getenv("DEEPSEEK_API_KEY", "").strip()
        or env.get("DEEPSEEK_API_KEY", "").strip()
    )
    if _is_placeholder(deepseek_key):
        deepseek_key = ""

    deepseek_base = (
        os.getenv("DEEPSEEK_BASE_URL", "").strip()
        or env.get("DEEPSEEK_BASE_URL", "").strip()
        or "https://api.deepseek.com/v1"
    )
    # DeepSeek base URL 必须带 /v1 后缀，否则 /chat/completions 会 404
    if deepseek_base and not deepseek_base.rstrip("/").endswith("/v1"):
        deepseek_base = deepseek_base.rstrip("/") + "/v1"

    # ---- 决定最终 key / base / model ----
    litellm_key = (
        env.get("LITELLM_OPENAI_API_KEY", "").strip()
        or deepseek_key
        or os.getenv("AI_IDE_LLM_API_KEY", "").strip()
        or os.getenv("AI_IDE_API_KEY", "").strip()
        or os.getenv("OPENAI_API_KEY", "").strip()
    )
    if _is_placeholder(litellm_key):
        litellm_key = ""

    # Base URL：deepseek 优先
    litellm_base = (
        env.get("LITELLM_OPENAI_API_BASE", "").strip()
        or (deepseek_base if deepseek_key else "")
        or os.getenv("OPENAI_BASE_URL", "").strip()
        or os.getenv("OPENAI_API_BASE", "").strip()
    )

    # The selected model must survive removal of vendor credentials from runner
    # environments. Re-running this function in RDLoopWrapper must not replace
    # LITELLM_CHAT_MODEL with an unrelated default after DEEPSEEK_API_KEY is stripped.
    selected_model = env.get("LITELLM_CHAT_MODEL", "").strip()
    if selected_model:
        chat_model = selected_model
        if not chat_model.startswith(("openai/", "azure/", "anthropic/", "huggingface/", "deepseek/")):
            chat_model = f"{'deepseek' if deepseek_key else 'openai'}/{chat_model}"
    elif deepseek_key:
        deepseek_model = (
            env.get("DEEPSEEK_MODEL", "").strip()
            or os.getenv("DEEPSEEK_MODEL", "").strip()
            or "deepseek-chat"
        )
        if not deepseek_model.startswith(("deepseek/", "openai/")):
            deepseek_model = f"deepseek/{deepseek_model}"
        chat_model = deepseek_model
    else:
        chat_model = (
            env.get("CHAT_MODEL", "").strip()
            or os.getenv("CHAT_MODEL", "").strip()
            or "gpt-4-turbo"
        )
        if chat_model and not chat_model.startswith(("openai/", "azure/", "anthropic/", "huggingface/", "deepseek/")):
            chat_model = f"openai/{chat_model}"

    # ---- 写入 LITELLM_（RD-Agent settings）----
    if litellm_key:
        env["LITELLM_OPENAI_API_KEY"] = litellm_key
    if litellm_base:
        env["LITELLM_OPENAI_API_BASE"] = litellm_base
    if chat_model:
        env["LITELLM_CHAT_MODEL"] = chat_model
        env["CHAT_MODEL"] = chat_model
        env["REASONING_MODEL"] = chat_model

    # Keep both settings readers on the same resolved credential and endpoint.
    if litellm_key:
        env["OPENAI_API_KEY"] = litellm_key
    if litellm_base:
        env["OPENAI_BASE_URL"] = litellm_base

    return env


EMBEDDING_ENV_KEYS = ("EMBEDDING_MODEL", "EMBEDDING_BASE_URL", "EMBEDDING_API_KEY")
"""部署级向量检索配置要透传给挖掘子进程的键。

chat 与 embedding 是**独立通道**（DeepSeek 这类 chat 供应商没有 /embeddings 端点），
所以这几个键必须和 LITELLM_/OPENAI_ 那组一起进子进程 env。漏掉任一环，
配置只由部署环境提供；本函数只向子进程筛选已配置的变量。
"""


def embedding_overrides(llm_overrides: dict[str, Any] | None) -> dict[str, str]:
    """从已解析的服务端环境覆盖项中挑出非空 embedding 变量。"""
    if not llm_overrides:
        return {}
    return {
        key: str(llm_overrides[key]).strip()
        for key in EMBEDDING_ENV_KEYS
        if str(llm_overrides.get(key) or "").strip()
    }
