"""050 push notifications — the installed app hears when a bot needs the person

Revision ID: 050_push_notifications
Revises: 049_bot_connectors

**`push_subscriptions`** are the browsers and installed apps that asked to be told:
the push service's endpoint and the two keys the browser gave for encrypting to it.
A subscription the push service says is gone (404/410) is deleted when it says so.

**`bot_notifications`** is an outbox. A bot that replies, asks a question, parks an
approval or asks for sign-in details writes a row in the same transaction as the line
in its conversation; the worker's notifier sends what is unsent and stamps it. Written
by the run, sent by a loop, so a push service that is slow or down never holds a run.

**`push_keys`** holds the server's VAPID key pair, made on first use: the public half
goes to browsers, the private half signs every push and is sealed with the credential
cipher like a vault entry.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "050_push_notifications"
down_revision: str | None = "049_bot_connectors"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "push_subscriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("endpoint", sa.Text, nullable=False, unique=True),
        sa.Column("p256dh", sa.Text, nullable=False),
        sa.Column("auth", sa.Text, nullable=False),
        sa.Column("user_agent", sa.Text, nullable=False, server_default=""),
        sa.Column("failures", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_ok_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "bot_notifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("bot_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("body", sa.Text, nullable=False, server_default=""),
        sa.Column("url", sa.Text, nullable=False, server_default="/"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_bot_notifications_unsent",
        "bot_notifications",
        ["created_at"],
        postgresql_where=sa.text("sent_at IS NULL"),
    )
    op.create_table(
        "push_keys",
        sa.Column("name", sa.Text, primary_key=True),
        sa.Column("public_key", sa.Text, nullable=False),
        sa.Column("key_id", sa.Text, nullable=False),
        sa.Column("nonce", sa.LargeBinary, nullable=False),
        sa.Column("ciphertext", sa.LargeBinary, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade() -> None:
    op.drop_table("push_keys")
    op.drop_index("ix_bot_notifications_unsent", table_name="bot_notifications")
    op.drop_table("bot_notifications")
    op.drop_table("push_subscriptions")
