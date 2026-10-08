"""051 bot template shares — a link another person can make their own bot from

Revision ID: 051_bot_template_shares
Revises: 050_push_notifications

A share is a **snapshot**: the bot's template (`domain.templates.BotTemplate`) as it was
when the link was made, so an edit made afterwards (a new standing note, a rule) is not
published to everyone who holds an old link. The token is the capability; a revoked
share keeps its row, so a link that stopped working says so instead of "not found".
`bot_id` is kept for the bot's own list of its links, and nulled if the bot is deleted —
the snapshot does not need it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "051_bot_template_shares"
down_revision: str | None = "050_push_notifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bot_template_shares",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column(
            "bot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("bots.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("token", sa.Text, nullable=False, unique=True),
        sa.Column("template", postgresql.JSONB, nullable=False),
        sa.Column("uses", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_bot_template_shares_bot", "bot_template_shares", ["bot_id"])


def downgrade() -> None:
    op.drop_table("bot_template_shares")
