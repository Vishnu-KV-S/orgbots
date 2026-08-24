"""Alembic environment.

Runs synchronously against the psycopg driver. Migrations are DDL; there is no
reason to pay for an async event loop to issue them, and a sync path keeps
`alembic upgrade head` usable from a shell without an app process.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from runtime.persistence.models import Base
from runtime.settings import get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

UNMANAGED_TABLES = ("audit_logs",)
"""Tables autogenerate must not touch.

`audit_logs` (020) is `PARTITION BY RANGE` with a partition per month. The
declarative models cannot express that, and its partitions are created at runtime by
`ensure_audit_partition()`, so autogenerate sees twenty-odd tables it did not put in
`Base.metadata` and proposes dropping all of them. Excluding the family by prefix is
the honest fix: the table is managed by raw DDL, on purpose, and saying so here is
better than a models file that lies about owning it.
"""


def include_object(
    obj: object, name: str | None, type_: str, reflected: bool, compare_to: object
) -> bool:
    """Keep autogenerate away from tables the migrations own by hand."""
    if type_ in {"table", "index"} and name is not None:
        return not any(name.startswith(prefix) for prefix in UNMANAGED_TABLES)
    return True


def _url() -> str:
    return get_settings().sync_database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        include_object=include_object,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool, future=True)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            # `lg` is owned by the LangGraph checkpointer's own setup(); autogenerate
            # must never try to manage tables it did not create.
            include_schemas=False,
            version_table_schema=None,
            include_object=include_object,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
