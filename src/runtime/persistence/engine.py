"""Engine and session plumbing.

Two things here are deliberate and neither is an optimisation:

- `session_factory` hands out sessions with `expire_on_commit=False`, so a domain
  object read inside a transaction stays usable after it commits. Without this,
  every post-commit attribute read fires a lazy load against a closed transaction.
- `separate_connection()` exists for the effect journal. The journal's INTENT
  write must commit independently of whatever transaction the caller is in, on its
  own connection, or a caller rollback would erase the record of an effect that
  already fired.
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

from runtime.settings import Settings, get_settings

_ENGINES: dict[tuple[str, int, int, bool], AsyncEngine] = {}


def _engine_for(url: str, pool_size: int, max_overflow: int, echo: bool) -> AsyncEngine:
    key = (url, pool_size, max_overflow, echo)
    engine = _ENGINES.get(key)
    if engine is not None:
        return engine
    engine = create_async_engine(
        url,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_pre_ping=True,
        # Chaos tests SIGKILL workers mid-transaction; a recycled pool avoids
        # inheriting connections the server has already torn down.
        pool_recycle=1800,
        echo=echo,
        future=True,
    )
    _ENGINES[key] = engine
    return engine


def get_engine(settings: Settings | None = None) -> AsyncEngine:
    s = settings or get_settings()
    return _engine_for(s.database_url, s.db_pool_size, s.db_max_overflow, s.db_echo)


def session_factory(settings: Settings | None = None) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(
        bind=get_engine(settings),
        expire_on_commit=False,
        autoflush=False,
    )


@asynccontextmanager
async def separate_session(settings: Settings | None = None) -> AsyncIterator[AsyncSession]:
    """A session on its own connection, independent of any ambient transaction.

    The effect journal uses this. Do not reuse the caller's session here: the
    INTENT row must survive the caller rolling back.
    """
    factory = session_factory(settings)
    async with factory() as session:
        yield session


async def dispose_engines() -> None:
    """Drop every pooled connection. Called on API shutdown and between test
    modules; a forked worker must call it before touching the DB, since inherited
    sockets are not safe to share across processes."""
    engines = list(_ENGINES.values())
    _ENGINES.clear()
    for engine in engines:
        await engine.dispose()
