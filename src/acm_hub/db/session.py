"""Async engine and session lifecycle.

Everything is async end to end: a synchronous driver here would block the
Discord gateway heartbeat while a query runs and get the bot disconnected.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from acm_hub.observability.logging import get_logger

logger = get_logger(__name__)


class Database:
    """Owns the engine and hands out sessions.

    ``pool_pre_ping`` is on because the bot is a long-lived process: without it,
    a connection dropped overnight by the database or an intervening proxy
    surfaces as a failed command rather than a transparent reconnect.
    """

    def __init__(self, url: str, *, echo: bool = False, pool_size: int = 5) -> None:
        self._url = url
        engine_kwargs: dict[str, object] = {"echo": echo, "pool_pre_ping": True}
        if not url.startswith("sqlite"):
            engine_kwargs["pool_size"] = pool_size
            engine_kwargs["max_overflow"] = pool_size
            engine_kwargs["pool_recycle"] = 1800
        self._engine: AsyncEngine = create_async_engine(url, **engine_kwargs)
        self._session_factory = async_sessionmaker(
            self._engine, expire_on_commit=False, class_=AsyncSession
        )

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        return self._session_factory

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Yield a session, committing on success and rolling back on error."""
        async with self._session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def check_connection(self) -> bool:
        """Return whether a trivial query succeeds, for the health command."""
        import sqlalchemy as sa

        try:
            async with self._engine.connect() as connection:
                await connection.execute(sa.text("SELECT 1"))
        except Exception as exc:  # noqa: BLE001 - health check reports, never raises
            logger.warning("database.health_check_failed", error=f"{type(exc).__name__}: {exc}")
            return False
        return True

    async def dispose(self) -> None:
        """Close all pooled connections during shutdown."""
        await self._engine.dispose()
        logger.info("database.disposed")
