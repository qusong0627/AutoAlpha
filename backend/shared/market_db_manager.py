import logging
import os
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

logger = logging.getLogger(__name__)

# ============================================================
# 行情数据库配置 — 使用容器内本地 PostgreSQL
# ============================================================
MARKET_DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
MARKET_DB_PORT = int(os.getenv("DB_PORT", "5432"))
MARKET_DB_NAME = os.getenv("DB_NAME", "quantmind")
MARKET_DB_USER = os.getenv("DB_USER", "quantmind")
MARKET_DB_PASSWORD = os.getenv("DB_PASSWORD", "")

MARKET_DB_URL = f"postgresql+asyncpg://{MARKET_DB_USER}:{MARKET_DB_PASSWORD}@{MARKET_DB_HOST}:{MARKET_DB_PORT}/{MARKET_DB_NAME}"

_market_engine: AsyncEngine | None = None
_market_session_factory: sessionmaker | None = None


def get_market_engine() -> AsyncEngine:
    """获取行情数据库的异步引擎（容器内 PG）"""
    global _market_engine, _market_session_factory
    if _market_engine is None:
        logger.info(f"Initializing market DB engine: {MARKET_DB_HOST}:{MARKET_DB_PORT}/{MARKET_DB_NAME}")
        _market_engine = create_async_engine(
            MARKET_DB_URL,
            pool_size=10,
            max_overflow=20,
            pool_pre_ping=True,
            echo=False,
        )
        _market_session_factory = sessionmaker(
            _market_engine, class_=AsyncSession, expire_on_commit=False
        )
    return _market_engine


@asynccontextmanager
async def get_market_session() -> AsyncGenerator[AsyncSession, None]:
    """获取行情数据库会话"""
    get_market_engine()  # Ensure engine is initialized
    async with _market_session_factory() as session:
        try:
            yield session
        finally:
            await session.close()
