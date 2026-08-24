"""026 intentions — scheduled_intentions

Revision ID: 026_intentions
Revises: 025_entities

An intention is *"come back to this on Thursday"* written down somewhere a machine can
read it. Without one, an actor that decides to revisit something has exactly two places
to put that decision: a sentence in a session summary that the next summarization
compresses away, or a memory that retrieval may or may not surface on the right day.
Both are how a plan silently stops existing.

**This is not a second scheduler.** M1 has one — `triggers` / `trigger_fires`, with
catch-up policy, dedupe and a cron parser — and building a second would mean two
answers to "what is due" and one of them wrong. `scheduled_intentions` is a *queue of
due things*; the existing scheduler is what wakes up and drains it. `due_at` is a
timestamp, not a cron expression, and that is the distinction: a trigger recurs, an
intention happens once and is done.

**`dedupe_key` is what stops the loop.** An actor that re-derives the same intention on
every run — and it will, because the condition that produced it is still true —
accumulates one row per run until Thursday arrives with forty identical reminders. The
partial unique index makes the second one a no-op while the first is still pending.

`fired_run_id` closes the loop the other way: an intention that fired points at the run
it caused, so *"did the thing we promised to revisit actually get revisited"* is a join
rather than a search. That question is one of §1's three hypotheses, and a hypothesis
you cannot query is a hope.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "026_intentions"
down_revision: str | None = "025_entities"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scheduled_intentions",
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
        sa.Column("intent", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="PENDING"),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.Column("source_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("source_memory_id", sa.Text(), nullable=True),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("fired_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("fired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.CheckConstraint(
            "status IN ('PENDING','FIRED','CANCELLED','EXPIRED')", name="ck_intention_status"
        ),
        sa.CheckConstraint(
            "scope IN ('session','private','department','company')", name="ck_intention_scope"
        ),
    )
    op.create_index(
        "uq_intention_pending",
        "scheduled_intentions",
        ["organization_id", "actor_name", "dedupe_key"],
        unique=True,
        postgresql_where=sa.text("status = 'PENDING'"),
    )
    # The drain query: everything pending and due, oldest first. Partial on PENDING
    # because a fired intention is history and history should not be in the index the
    # scheduler hits every twenty seconds.
    op.create_index(
        "ix_intention_due",
        "scheduled_intentions",
        ["due_at"],
        postgresql_where=sa.text("status = 'PENDING'"),
    )
    op.create_index("ix_intention_actor", "scheduled_intentions", ["organization_id", "actor_name"])


def downgrade() -> None:
    op.drop_index("ix_intention_actor", table_name="scheduled_intentions")
    op.drop_index("ix_intention_due", table_name="scheduled_intentions")
    op.drop_index("uq_intention_pending", table_name="scheduled_intentions")
    op.drop_table("scheduled_intentions")
