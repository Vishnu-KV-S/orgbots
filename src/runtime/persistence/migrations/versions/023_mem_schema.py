"""023 mem schema — CREATE SCHEMA mem, pgvector where it exists, and the collection registry

Revision ID: 023_mem_schema
Revises: 022_credentials

Same shape as 007: the store owns everything inside `mem`, this revision creates the
namespace. Mem0 creates its own collection tables at first write; so does
`NativeMemoryStore`. Neither is described here, because a collection is named after an
`embedding_version` (§3.3) and versions arrive after the migration that would have had
to know about them.

**`CREATE EXTENSION vector` is attempted and its failure is not fatal**, which is the
one thing in this file that needs defending.

pgvector is not a Postgres built-in. It is a package — `postgresql-NN-pgvector`, or a
different base image — and it is absent from both stock `postgres:16-alpine` and the
`scripts/devstack.sh` cluster (see `docs/M3_LIBRARY_FACTS.md` fact 3). A migration that
hard-failed on it would mean `alembic upgrade head` does not run on a developer machine,
and therefore that T46-T54 and evals 4, 5 and 9 — the three zero-tolerance isolation
gates — do not run either. Making the hard gates conditional on an optional package is
how a hard gate becomes decorative.

So the extension is created when available and its absence is *recorded* rather than
ignored: `mem.capabilities` holds one row saying whether this database can do ANN, and
`MemoryStore` adapters read it at startup. An operator who expected pgvector and got the
native store finds out from a table rather than from a latency graph six weeks later.

`mem.collections` is the registry that makes T45 testable. §3.3's rule — *"name the
collection with the version, and treat an embedder change as a new collection plus
backfill, never an in-place edit"* — needs somewhere to hold the dimension a collection
was created at. pgvector holds it in the column type; `real[]` does not, so the native
adapter enforces it from here. Both adapters register, so "which collections exist and
at what width" has one answer regardless of which is running.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "023_mem_schema"
down_revision: str | None = "022_credentials"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS mem")

    # Probed, not assumed. `CREATE EXTENSION` inside a DO block with an exception
    # handler keeps the transaction alive when the package is missing — a bare
    # statement would abort the migration's transaction and take 024 with it.
    op.execute(
        """
        DO $$
        BEGIN
            CREATE EXTENSION IF NOT EXISTS vector;
        EXCEPTION WHEN OTHERS THEN
            RAISE NOTICE 'pgvector unavailable (%); the native memory store will be used', SQLERRM;
        END
        $$
        """
    )

    op.create_table(
        "capabilities",
        sa.Column("id", sa.Boolean(), primary_key=True, server_default=sa.text("true")),
        sa.Column("pgvector", sa.Boolean(), nullable=False),
        sa.Column("pgvector_version", sa.Text(), nullable=True),
        sa.Column(
            "checked_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        # One row, forever. A boolean primary key defaulted to true is the smallest
        # honest way to say "this table has exactly one row" to the database rather
        # than to a comment nobody reads.
        sa.CheckConstraint("id = true", name="ck_capabilities_singleton"),
        schema="mem",
    )
    op.execute(
        """
        INSERT INTO mem.capabilities (id, pgvector, pgvector_version)
        SELECT true,
               EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector'),
               (SELECT extversion FROM pg_extension WHERE extname = 'vector')
        """
    )

    op.create_table(
        "collections",
        sa.Column("name", sa.Text(), primary_key=True),
        sa.Column("embedding_version", sa.Text(), nullable=False),
        sa.Column("dim", sa.Integer(), nullable=False),
        sa.Column("backend", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("row_count", sa.BigInteger(), nullable=False, server_default="0"),
        # A collection is never re-dimensioned. §3.3: an embedder change is a new
        # collection plus a backfill. The constraint says so; `NativeMemoryStore`
        # raises `EmbeddingDimensionMismatch` before it gets this far, and T45 checks
        # that the *old* collection is untouched afterwards.
        sa.CheckConstraint("dim > 0", name="ck_collection_dim"),
        sa.CheckConstraint("backend IN ('native','mem0')", name="ck_collection_backend"),
        schema="mem",
    )
    op.create_index("ix_mem_collection_version", "collections", ["embedding_version"], schema="mem")


def downgrade() -> None:
    # CASCADE, because the collection tables this schema will hold are created at
    # runtime and are not known here. Same trade as 007: correct for a schema this
    # migration created, and the reason T0 asserts reversibility rather than data
    # preservation. The extension is deliberately *not* dropped — another database
    # user may be relying on it and it was not necessarily ours to create.
    op.execute("DROP SCHEMA IF EXISTS mem CASCADE")
