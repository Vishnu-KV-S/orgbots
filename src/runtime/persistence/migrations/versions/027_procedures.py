"""027 procedures — procedure_candidates

Revision ID: 027_procedures
Revises: 026_intentions

A procedure candidate is a *proposal* that some sequence of steps is worth remembering
as a way of doing things. It is a candidate and not a procedure, and the whole table is
built around keeping it that way.

The failure mode this guards against is specific. An actor that did something twice and
succeeded twice will happily conclude it has found a method; promote that into company
memory and every future actor is told to do it that way, including in the cases where it
does not work. Two observations is not a method, it is a coincidence with a sample size.

So the table counts. `observed_count` goes up each time the same shape recurs;
`success_count` goes up when the run it appeared in was accepted. Nothing is promoted
into `memory_metadata` as `memory_type='procedure'` until both a threshold and a review
say so — the same queue §8 defines for facts, through the same `memory_promotions`
table. There is no second promotion path, because a second path is a path with no
reviewer.

**`steps_hash` is the recurrence key**, and it is a hash of the *normalised* step
sequence — tool names and node names, not arguments. Arguments differ every time; the
shape is what recurs. Hashing the arguments in would mean nothing ever recurred and the
table filled up with singletons, which is the quiet way a feature does nothing.

`last_failure_run_id` exists because the interesting review question is not "did this
work" but "when did it stop working". A candidate with a high success count and a
failure last Tuesday is the one worth reading.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "027_procedures"
down_revision: str | None = "026_intentions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "procedure_candidates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("actor_name", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False, server_default="private"),
        sa.Column("scope_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("steps", postgresql.JSONB(), nullable=False),
        sa.Column("steps_hash", sa.Text(), nullable=False),
        sa.Column("observed_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("success_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("last_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("last_failure_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("promoted_memory_id", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="OBSERVING"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('OBSERVING','PROPOSED','ADOPTED','REJECTED')",
            name="ck_procedure_status",
        ),
        sa.CheckConstraint(
            "scope IN ('session','private','department','company')", name="ck_procedure_scope"
        ),
        sa.CheckConstraint(
            "observed_count >= success_count + failure_count", name="ck_procedure_counts"
        ),
        # ADOPTED means a promotion happened, and a promotion produces a memory. A row
        # claiming adoption with nothing to point at is the shape of an auto-promotion
        # that skipped the queue, so the database refuses to represent it (T53).
        sa.CheckConstraint(
            "status <> 'ADOPTED' OR promoted_memory_id IS NOT NULL",
            name="ck_procedure_adopted_has_memory",
        ),
        sa.UniqueConstraint(
            "organization_id", "actor_name", "steps_hash", name="uq_procedure_shape"
        ),
    )
    op.create_index(
        "ix_procedure_ready",
        "procedure_candidates",
        ["organization_id", "observed_count"],
        postgresql_where=sa.text("status = 'OBSERVING'"),
    )


def downgrade() -> None:
    op.drop_index("ix_procedure_ready", table_name="procedure_candidates")
    op.drop_table("procedure_candidates")
