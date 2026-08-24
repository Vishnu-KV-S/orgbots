"""025 entities — entities, entity_links

Revision ID: 025_entities
Revises: 024_memory_meta

§2 scope: *"Entity tables (relational, not vector)."* The parenthesis is the whole
design and it is worth saying why, because "put the entities in the vector store too"
is the obvious move and it is wrong.

"Which competitor did we write about in March" is a *lookup*, not a similarity search.
Embedding a company name and asking for its nearest neighbours returns the four other
companies in the same sentence, ranked by how alike their descriptions are — which is
an answer to a question nobody asked. A relational row with a unique key answers the
question exactly, in a millisecond, with no embedder in the path and no `[VERIFY]`
attached to it.

So entities are rows, and the *link* between an entity and the memories that mention it
is a row too. `entity_links` is the join that makes "everything we know about
Competitor X" a single indexed read rather than a retrieval whose recall is a
measurement.

**`canonical_name` is the identity, `aliases` is how you find it.** Cross-session
identity is one of §3.6's open problems, and nothing here solves it: two spellings of
the same company become two entities until somebody merges them. What the schema does
provide is a merge that is cheap — repoint `entity_links.entity_id`, add the loser's
name to the winner's aliases — rather than a re-extraction.

The `kind` column is deliberately not a CHECK constraint. Entity kinds are open in a
way scopes and trust levels are not: a marketing department will want `campaign` and a
finance one will want `vendor`, and forcing a migration for each is how the table stops
being used.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "025_entities"
down_revision: str | None = "024_memory_meta"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "entities",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("canonical_name", sa.Text(), nullable=False),
        sa.Column(
            "aliases", postgresql.ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'")
        ),
        sa.Column(
            "attributes", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("scope", sa.Text(), nullable=False, server_default="department"),
        sa.Column("scope_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("first_seen_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("mention_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("merged_into", postgresql.UUID(as_uuid=True), nullable=True),
        # Case-insensitive uniqueness on the *canonical* name within a kind and a
        # scope. `lower()` in the index rather than a citext column: citext is another
        # extension, and §3.3 has already taught us what depending on one costs.
        sa.CheckConstraint(
            "scope IN ('session','private','department','company')", name="ck_entity_scope"
        ),
    )
    op.create_index(
        "uq_entity_canonical",
        "entities",
        ["organization_id", "kind", "scope_id", sa.text("lower(canonical_name)")],
        unique=True,
        postgresql_where=sa.text("merged_into IS NULL"),
    )
    op.create_index("ix_entity_org_kind", "entities", ["organization_id", "kind"])
    # GIN over aliases, so "is this spelling already known" is one index probe rather
    # than a scan of every entity of that kind. Alias lookup happens on every
    # extraction, which makes it hot enough to index and small enough not to worry
    # about the write cost.
    op.create_index("ix_entity_aliases", "entities", ["aliases"], postgresql_using="gin")

    op.create_table(
        "entity_links",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column(
            "entity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("entities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("memory_id", sa.Text(), nullable=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("artifact_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("relation", sa.Text(), nullable=False, server_default="mentions"),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        # No FK to `memory_metadata`. A link may point at a memory the store has since
        # superseded, and a cascade would delete the evidence that the link ever
        # existed — which is the thing an audit of a bad company-scoped fact needs.
        sa.CheckConstraint(
            "memory_id IS NOT NULL OR run_id IS NOT NULL OR artifact_id IS NOT NULL",
            name="ck_entity_link_target",
        ),
    )
    op.create_index("ix_entity_link_entity", "entity_links", ["entity_id"])
    op.create_index(
        "uq_entity_link_memory",
        "entity_links",
        ["entity_id", "memory_id", "relation"],
        unique=True,
        postgresql_where=sa.text("memory_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_entity_link_memory", table_name="entity_links")
    op.drop_index("ix_entity_link_entity", table_name="entity_links")
    op.drop_table("entity_links")
    op.drop_index("ix_entity_aliases", table_name="entities")
    op.drop_index("ix_entity_org_kind", table_name="entities")
    op.drop_index("uq_entity_canonical", table_name="entities")
    op.drop_table("entities")
