"""047 bot groups — group chats, bots messaging bots, and reactions

Revision ID: 047_bot_groups
Revises: 046_bot_rule_deny

**`bot_groups`** and **`bot_group_members`**: a conversation between a person and two to
six bots, with a lead who answers a message that names nobody. **`bot_group_messages`**
is that conversation, append-only like `bot_messages`, ordered by `seq`; a message in a
thread names its root in `thread_root`. A bot's post carries its run, and an id derived
from it, so a replayed step posts once.

**`bot_wakes`** is the queue of deliveries a bot works on when it is free: a group
message that named it, or a message from another bot (which is already in the
recipient's own conversation; the wake only says to start). The id is derived from
what caused it, so a replayed step queues nothing new, and `hops` bounds how far one
person's message can echo between bots.

**`bot_reactions`** keeps the person's reactions, which lived in the browser until now,
on either kind of message (`scope`).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "047_bot_groups"
down_revision: str | None = "046_bot_rule_deny"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bot_groups",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("lead_bot_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("unread", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "bot_group_members",
        sa.Column(
            "group_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("bot_groups.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "bot_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("bots.id"), primary_key=True
        ),
        sa.Column(
            "added_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "bot_group_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("seq", sa.BigInteger, sa.Identity(always=True), nullable=False, unique=True),
        sa.Column(
            "group_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("bot_groups.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("author_kind", sa.Text, nullable=False),
        sa.Column("author_bot_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("author_name", sa.Text, nullable=False, server_default=""),
        sa.Column("content", sa.Text, nullable=False, server_default=""),
        sa.Column(
            "payload", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("thread_root", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "author_kind IN ('person','bot','system')", name="ck_bot_group_message_author"
        ),
    )
    op.create_index("ix_bot_group_messages_seq", "bot_group_messages", ["group_id", "seq"])

    op.create_table(
        "bot_wakes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "bot_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("bots.id"), nullable=False
        ),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("group_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("group_message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("thread_root", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("from_bot_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("hops", sa.Integer, nullable=False, server_default="0"),
        sa.Column("expects_reply", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("handoff", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("status", sa.Text, nullable=False, server_default="queued"),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("detail", sa.Text, nullable=False, server_default=""),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind IN ('group','message')", name="ck_bot_wake_kind"),
        sa.CheckConstraint("status IN ('queued','started','skipped')", name="ck_bot_wake_status"),
    )
    op.create_index(
        "ix_bot_wakes_queued",
        "bot_wakes",
        ["created_at"],
        postgresql_where=sa.text("status = 'queued'"),
    )

    op.create_table(
        "bot_reactions",
        sa.Column("message_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("emoji", sa.Text, primary_key=True),
        sa.Column("scope", sa.Text, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("scope IN ('bot','group')", name="ck_bot_reaction_scope"),
    )


def downgrade() -> None:
    op.drop_table("bot_reactions")
    op.drop_index("ix_bot_wakes_queued", table_name="bot_wakes")
    op.drop_table("bot_wakes")
    op.drop_index("ix_bot_group_messages_seq", table_name="bot_group_messages")
    op.drop_table("bot_group_messages")
    op.drop_table("bot_group_members")
    op.drop_table("bot_groups")
