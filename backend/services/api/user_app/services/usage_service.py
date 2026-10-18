"""用量/配额服务（stub）。

推理配额逻辑尚未落地，此处提供总是允许的占位实现，确保 API 服务可启动。
后续接入真实配额数据时替换 check_usage / increment_usage 内部实现即可。
"""

import logging

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


class UsageService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def check_usage(
        self, user_id: str, tenant_id: str, resource: str
    ) -> tuple[bool, str, dict]:
        return True, "ok", {"used": 0, "limit": -1}

    async def increment_usage(
        self, user_id: str, tenant_id: str, resource: str
    ) -> None:
        return None
