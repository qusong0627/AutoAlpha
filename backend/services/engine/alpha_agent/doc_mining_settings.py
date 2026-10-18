"""Legacy MinerU Redis cleanup; runtime credentials come only from server env."""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

KEY_PREFIX = "qm:docmining:mineru_settings"


def _default_redis_client():
    import redis as redis_lib

    return redis_lib.Redis(
        host=os.getenv("REDIS_HOST") or "redis",
        port=int(os.getenv("REDIS_PORT", "6379")),
        db=int(os.getenv("REDIS_DB", "0")),
        password=os.getenv("REDIS_PASSWORD") or None,
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=5,
    )


class DocMiningSettingsStore:
    """Store that can delete legacy rows but cannot read or write credentials."""

    def __init__(self, *, redis_client=None) -> None:
        self._redis = redis_client

    def _client(self):
        if self._redis is None:
            self._redis = _default_redis_client()
        return self._redis

    @staticmethod
    def _key(user_id: str, tenant_id: str) -> str:
        return f"{KEY_PREFIX}:{tenant_id}:{user_id}"

    def get(
        self, user_id: str | None, tenant_id: str | None, *, strict: bool = False
    ) -> None:
        """Never read historical user-scoped credential records."""
        del user_id, tenant_id, strict
        return None

    def save(
        self, user_id: str, tenant_id: str, payload: object
    ) -> None:
        """Reject user-scoped writes; configure MinerU in the service environment."""
        del user_id, tenant_id, payload
        raise RuntimeError(
            "User-level MinerU settings are disabled; use server environment variables"
        )

    def clear(self, user_id: str | None, tenant_id: str | None) -> bool:
        """Delete one legacy record without reading or returning its contents."""
        uid, tid = str(user_id or ""), str(tenant_id or "")
        if not uid or not tid:
            return False
        removed = bool(self._client().delete(self._key(uid, tid)))
        if removed:
            logger.info("Legacy MinerU settings cleared for user=%s", uid)
        return removed


_store: DocMiningSettingsStore | None = None


def get_doc_mining_settings_store() -> DocMiningSettingsStore:
    global _store
    if _store is None:
        _store = DocMiningSettingsStore()
    return _store
