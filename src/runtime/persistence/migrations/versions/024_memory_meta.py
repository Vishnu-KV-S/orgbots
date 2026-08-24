"""024 memory meta — memory_metadata, memory_audit

Revision ID: 024_memory_meta
Revises: 023_mem_schema

§4: *"Mem0 owns its tables inside `mem`. **You own the metadata sidecar**, keyed by
Mem0's memory id — scope, trust, provenance and promotion state are your policy, not
the library's."*

Three decisions in here are load-bearing and none of them is obvious.

**`memory_id` is `text`, not `uuid`.** It is the *store's* id and the store is a port
with more than one adapter. Mem0 mints UUID-shaped strings; nothing guarantees the next
adapter does. See `runtime.domain.ids.MemoryId`.

**The sidecar lives in `public`, not in `mem`.** `mem` is the store's namespace and a
`DROP SCHEMA mem CASCADE` — which is what re-seeding a vector store looks like — must
not take the provenance with it. Losing embeddings costs a backfill; losing the record
of *which memories were quarantined and who promoted what* costs the audit trail that
§8 exists to produce.

**`ix_mem_scope` is partial on `status = 'active'`.** Every retrieval carries
`status='active' AND trust != quarantine` (§7, *"in the query, not after"*), so the
index that serves retrieval should not be carrying superseded and retired rows it will
never return. Those rows are kept — eval 6 is a measurement over superseded pairs — but
they are kept out of the hot index.

`ck_memory_quarantine_status` is the constraint that makes §6's rule structural rather
than procedural: a row cannot be `UNTRUSTED_QUARANTINE` and `active` at the same time.
The write path already sets both together; this is what catches the second write path
somebody adds later.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "024_memory_meta"
down_revision: str | None = "023_mem_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memory_metadata",
        sa.Column("memory_id", sa.Text(), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("scope_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scope_key", sa.Text(), nullable=False),
        # `scope_key` is `scope || ':' || scope_id`, denormalised. It is what the
        # vector store's own payload filter matches on (defence in depth, §13 risk 3),
        # and having one string on both sides means the two filters cannot disagree
        # about how a scope is spelled.
        sa.Column("memory_type", sa.Text(), nullable=False),
        sa.Column("trust", sa.Text(), nullable=False),
        sa.Column("source_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_actor_version", sa.BigInteger(), nullable=True),
        sa.Column("source_artifact_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("embedding_version", sa.Text(), nullable=False),
        sa.Column("collection", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("importance", sa.Float(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("last_accessed", sa.DateTime(timezone=True), nullable=True),
        sa.Column("access_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("supersedes", sa.Text(), nullable=True),
        sa.Column("superseded_by", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="active"),
        sa.CheckConstraint(
            "scope IN ('session','private','department','company')", name="ck_memory_scope"
        ),
        sa.CheckConstraint(
            "trust IN ('TRUSTED','DERIVED','UNTRUSTED_QUARANTINE')", name="ck_memory_trust"
        ),
        sa.CheckConstraint(
            "status IN ('active','superseded','quarantined','retired')", name="ck_memory_status"
        ),
        sa.CheckConstraint(
            "memory_type IN ('fact','episode','procedure','entity_ref')", name="ck_memory_type"
        ),
        sa.CheckConstraint(
            "NOT (trust = 'UNTRUSTED_QUARANTINE' AND status = 'active')",
            name="ck_memory_quarantine_status",
        ),
        sa.CheckConstraint("access_count >= 0", name="ck_memory_access_count"),
    )
    op.create_index(
        "ix_mem_scope",
        "memory_metadata",
        ["organization_id", "scope", "scope_id"],
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_index(
        "ix_mem_scope_key",
        "memory_metadata",
        ["organization_id", "scope_key"],
        postgresql_where=sa.text("status = 'active'"),
    )
    # The quarantine read path (§6: "retrievable only by the originating actor") has
    # its own index, because it is a different predicate against a different subset and
    # sharing an index with the hot path would mean the hot path's partial WHERE could
    # not exclude quarantined rows.
    op.create_index(
        "ix_mem_quarantine_actor",
        "memory_metadata",
        ["organization_id", "source_actor_id"],
        postgresql_where=sa.text("status = 'quarantined'"),
    )
    op.create_index("ix_mem_source_run", "memory_metadata", ["source_run_id"])

    op.create_table(
        "memory_audit",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column("memory_id", sa.Text(), nullable=False),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("event", sa.Text(), nullable=False),
        # ADD / UPDATE / DELETE / NOOP as the store reports them (§3.1), plus our own
        # ACCESS, QUARANTINE, RELEASE, PROMOTE, RETIRE. One vocabulary, one table: the
        # question "what has ever happened to this memory" should not require a union.
        sa.Column("actor_name", sa.Text(), nullable=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "detail", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "event IN ('ADD','UPDATE','DELETE','NOOP','ACCESS','QUARANTINE','RELEASE',"
            "'PROMOTE','RETIRE','SUPERSEDE')",
            name="ck_memory_audit_event",
        ),
    )
    op.create_index("ix_memory_audit_memory", "memory_audit", ["memory_id", "occurred_at"])
    op.create_index("ix_memory_audit_org_time", "memory_audit", ["organization_id", "occurred_at"])


def downgrade() -> None:
    op.drop_index("ix_memory_audit_org_time", table_name="memory_audit")
    op.drop_index("ix_memory_audit_memory", table_name="memory_audit")
    op.drop_table("memory_audit")
    op.drop_index("ix_mem_source_run", table_name="memory_metadata")
    op.drop_index("ix_mem_quarantine_actor", table_name="memory_metadata")
    op.drop_index("ix_mem_scope_key", table_name="memory_metadata")
    op.drop_index("ix_mem_scope", table_name="memory_metadata")
    op.drop_table("memory_metadata")
