"""011 inbox — inbox_messages

Revision ID: 011_inbox
Revises: 010_evaluations

The durable queue between actors, and — because it is durable and in Postgres —
the thing that makes actor-to-actor delivery survive a Redis wipe for free.

Four columns carry the safety properties:

`dedupe_key` is UNIQUE. Every fact an actor announces has one deterministic key, so
re-announcing is a no-op rather than a second wake-up. This is the same trick
`uq_run_idem` plays for runs, applied one level up.

`hop_count` is what stops A→B→A→B forever. Every message copies its parent's count
plus one; `ck_inbox_hop_cap` refuses at 8. A refused message is written with status
`DROPPED`, never discarded — the row is the evidence that a loop was cut. T20.

`correlation_id` ties the whole chain back to the cron fire that started it.

`delivered_run_id` is how the dispatcher stays idempotent: a message is claimed by
a conditional UPDATE that sets it, so two dispatchers racing produce one run.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "011_inbox"
down_revision: str | None = "010_evaluations"
branch_labels = None
depends_on = None

MAX_HOP_COUNT = 8


def upgrade() -> None:
    op.create_table(
        "inbox_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("causation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("sender_actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("sender_name", sa.Text(), nullable=True),
        sa.Column("recipient_name", sa.Text(), nullable=False),
        sa.Column("subject", sa.Text(), nullable=False, server_default=""),
        sa.Column("body", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("artifact_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("hop_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="PENDING"),
        sa.Column("drop_reason", sa.Text(), nullable=True),
        sa.Column("delivered_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("dedupe_key", name="uq_inbox_dedupe"),
        sa.CheckConstraint("status IN ('PENDING','DELIVERED','DROPPED')", name="ck_inbox_status"),
        sa.CheckConstraint(
            f"hop_count >= 0 AND hop_count <= {MAX_HOP_COUNT}", name="ck_inbox_hop_cap"
        ),
    )
    op.create_index(
        "ix_inbox_undelivered",
        "inbox_messages",
        ["created_at"],
        postgresql_where=sa.text("status = 'PENDING'"),
    )
    op.create_index("ix_inbox_recipient", "inbox_messages", ["recipient_name", "created_at"])
    op.create_index("ix_inbox_correlation", "inbox_messages", ["correlation_id"])
    op.create_index(
        "ix_inbox_session",
        "inbox_messages",
        ["session_id", "created_at"],
        postgresql_where=sa.text("session_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_inbox_session", table_name="inbox_messages")
    op.drop_index("ix_inbox_correlation", table_name="inbox_messages")
    op.drop_index("ix_inbox_recipient", table_name="inbox_messages")
    op.drop_index("ix_inbox_undelivered", table_name="inbox_messages")
    op.drop_table("inbox_messages")
