"""文档解析链凭据解析：只读取服务端环境，不从用户配置存储读取密钥。

当前部署关闭用户认证，因此 MinerU token、地址和本地服务 key 都是部署级配置，
由 ``resolve_mineru_config`` 从环境变量解析。``user_id``、``tenant_id`` 和
``strict`` 仅为兼容旧调用签名保留。
"""

from __future__ import annotations

from backend.services.engine.alpha_agent.mineru_client import (
    MineruConfig,
    resolve_mineru_config,
)

TOKEN_SRC_USER = "user"  # Legacy source marker for rows created before env-only credentials.
TOKEN_SRC_ENV = "env"
TOKEN_SRC_NONE = "none"


async def resolve_effective_mineru_config(
    user_id: str | None,
    tenant_id: str | None,
    *,
    strict: bool = False,
) -> tuple[MineruConfig | None, str]:
    """→ ``(config, src)``，src ∈ ``{"env", "none"}``。"""
    del user_id, tenant_id, strict
    env_cfg = resolve_mineru_config()
    if env_cfg is not None:
        return env_cfg, TOKEN_SRC_ENV
    return None, TOKEN_SRC_NONE
