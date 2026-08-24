"""LangGraph checkpointer, pinned to schema `lg`.

The checkpointer owns its own tables and migrates them itself. Putting them in a
separate schema means a framework upgrade that changes checkpoint storage is not
also an Alembic conflict, and `pg_dump --schema=public` of our own data does not
drag along whatever the framework is doing this month.

The schema is selected by `search_path` on the connection rather than by patching
table names, because the framework's SQL is not ours to rewrite.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import AsyncConnection
from psycopg.rows import DictRow, dict_row
from psycopg_pool import AsyncConnectionPool

from runtime.settings import Settings, get_settings

LG_SCHEMA = "lg"


def _dsn(settings: Settings) -> str:
    """SQLAlchemy DSN → plain libpq DSN. psycopg_pool does not want the dialect."""
    return settings.database_url.replace("postgresql+psycopg://", "postgresql://")


@asynccontextmanager
async def checkpointer(
    settings: Settings | None = None, *, setup: bool = True
) -> AsyncIterator[AsyncPostgresSaver]:
    """Open a checkpointer bound to `lg`.

    `setup=False` for hot paths where the tables are known to exist; the framework's
    `setup()` is idempotent but it is still a round trip per worker start.
    """
    resolved = settings or get_settings()
    # `row_factory=dict_row` is what the framework's own `from_conn_string` sets;
    # its SQL indexes rows by column name.
    pool: AsyncConnectionPool[AsyncConnection[DictRow]] = AsyncConnectionPool(
        conninfo=_dsn(resolved),
        max_size=10,
        open=False,
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
            "options": f"-c search_path={LG_SCHEMA}",
        },
    )
    async with pool:
        await pool.open(wait=True)
        saver = AsyncPostgresSaver(pool)
        if setup:
            await saver.setup()
        yield saver


async def ensure_checkpoint_tables(settings: Settings | None = None) -> None:
    """Run the framework's migrations once, at startup, so workers do not race."""
    async with checkpointer(settings, setup=True):
        return
