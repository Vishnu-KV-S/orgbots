"""037 bots — persistent AI employees on top of actors

Revision ID: 037_bots
Revises: 036_kill_department

A bot is an actor with a face: everything that decides what it may do — its spec, its
ceilings, its budget pool, the kill switch — is the actor it points at through
`(organization_id, actor_name)`. This table holds only what a person sees and edits:
the name, the label, the standing instructions, what it has learned, and how it sits in
the sidebar.

**Four tables, and one of them is the conversation.** `bot_messages` is an append-only
log ordered by `seq`, holding the person's messages, the bot's replies, and the
activity in between — every observation and action, every question, every approval
request — so the transcript *is* the record of what the bot did. Rows written by a run
carry a deterministic id (uuid5 of run, step and kind), so a replayed graph node
re-inserting the same activity is a no-op rather than a duplicate line.

**`bot_rules` and `bot_pending_actions` are the bot-level approval gate.** The
runtime's approval service keys on a task, and a chat message is not a task; a run
that pauses on an action ends, and the action resumes in a fresh run once a person
decides — the same "a run does not wait" shape as `org/approvals.py`, keyed on the
pending action instead.

**A deleted bot is soft-deleted.** Its actor is kept (runs and audit rows reference
it) and its name is freed by `deleted_at`, so a new bot may reuse it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "037_bots"
down_revision: str | None = "036_kill_department"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("actor_name", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("label", sa.Text, nullable=False, server_default=""),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column("instructions", sa.Text, nullable=False, server_default=""),
        sa.Column("avatar", sa.Text, nullable=False, server_default=""),
        sa.Column("memory", sa.Text, nullable=False, server_default=""),
        sa.Column("pinned", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("hidden", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("unread", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("needs_attention", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("stop_requested", sa.Boolean, nullable=False, server_default=sa.false()),
        # Bumped by every new instruction and every approval decision. A run carries
        # the turn it was started for, and a run whose turn is no longer current stops
        # at its next step — which is how "send another instruction" redirects a bot
        # that is mid-task without a second run fighting the first for the screen.
        sa.Column("turn", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("duplicated_from", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ux_bots_actor_live",
        "bots",
        ["organization_id", "actor_name"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    op.create_table(
        "bot_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("seq", sa.BigInteger, sa.Identity(always=True), nullable=False, unique=True),
        sa.Column(
            "bot_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("bots.id"), nullable=False
        ),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("content", sa.Text, nullable=False, server_default=""),
        sa.Column(
            "payload", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reply_to", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "role IN ('user','bot','activity','approval','system','error')",
            name="ck_bot_message_role",
        ),
    )
    op.create_index("ix_bot_messages_bot_seq", "bot_messages", ["bot_id", "seq"])

    op.create_table(
        "bot_rules",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "bot_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("bots.id"), nullable=False
        ),
        sa.Column("action_type", sa.Text, nullable=False),
        sa.Column("host", sa.Text, nullable=False, server_default=""),
        sa.Column("decision", sa.Text, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("decision IN ('ask','allow')", name="ck_bot_rule_decision"),
        sa.UniqueConstraint("bot_id", "action_type", "host", name="uq_bot_rule"),
    )

    op.create_table(
        "bot_pending_actions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "bot_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("bots.id"), nullable=False
        ),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("action", postgresql.JSONB, nullable=False),
        sa.Column("reason", sa.Text, nullable=False, server_default=""),
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        sa.Column("scope", sa.Text, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending','allowed','denied','expired')", name="ck_bot_pending_status"
        ),
        sa.CheckConstraint(
            "scope IS NULL OR scope IN ('once','always')", name="ck_bot_pending_scope"
        ),
    )
    op.create_index(
        "ix_bot_pending_live",
        "bot_pending_actions",
        ["bot_id"],
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_table("bot_pending_actions")
    op.drop_table("bot_rules")
    op.drop_index("ix_bot_messages_bot_seq", table_name="bot_messages")
    op.drop_table("bot_messages")
    op.drop_index("ux_bots_actor_live", table_name="bots")
    op.drop_table("bots")
