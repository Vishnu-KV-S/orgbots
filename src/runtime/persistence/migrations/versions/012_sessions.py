"""012 sessions — sessions, session_summaries

Revision ID: 012_sessions
Revises: 011_inbox

A session is one actor's continuing thread of work. The inbox is its message log —
there is no second message table, because "the last six messages" and "the messages
that woke this actor" are the same six messages, and keeping them in two places is
how they start disagreeing.

`session_summaries` is append-only and each row records `upto_message_id`: the
summary describes history *up to* that message and no further. A summarizer that
merely overwrote a text column would make "what did the actor know when it made
this decision" unanswerable a week later, which is precisely the question §10 asks
when the rejection rate is high.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "012_sessions"
down_revision: str | None = "011_inbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("actor_name", sa.Text(), nullable=False),
        sa.Column("session_key", sa.Text(), nullable=False),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="OPEN"),
        sa.Column("message_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("summarized_upto", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        # Derived from (actor, week) by the session service, so two runs of the same
        # actor in one week share a session without a lookup-then-insert race.
        sa.UniqueConstraint("organization_id", "session_key", name="uq_session_key"),
        sa.CheckConstraint("status IN ('OPEN','CLOSED')", name="ck_session_status"),
    )
    op.create_index("ix_sessions_actor", "sessions", ["actor_name", "created_at"])

    op.create_table(
        "session_summaries",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "session_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sessions.id"),
            nullable=False,
        ),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("upto_message_count", sa.Integer(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("session_id", "upto_message_count", name="uq_session_summary_upto"),
    )
    op.create_index("ix_session_summaries", "session_summaries", ["session_id", "id"])


def downgrade() -> None:
    op.drop_index("ix_session_summaries", table_name="session_summaries")
    op.drop_table("session_summaries")
    op.drop_index("ix_sessions_actor", table_name="sessions")
    op.drop_table("sessions")
