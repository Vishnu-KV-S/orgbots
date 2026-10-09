"""054 tag on X — people give their bots tasks by tagging the organization's X account

Revision ID: 054_tag_on_x
Revises: 053_enterprise

**`x_accounts`** — the organization's X account (`@AcmeBots`): its handle and user id,
an app token that reads its mentions, and optionally a user token that posts a short
"on it" reply. Both sealed with the credential cipher. `since_id` is how far the
mentions timeline has been read.

**`x_links`** — a person's X account, proved theirs, and the bot that takes their tags.
`member_id` is null in a runtime without members (the one person). One X account per
person and one person per X account, in an organization.

**`x_link_codes`** — a one-time code the person posts from their X account
(`@AcmeBots link 7KQ2MX`) to prove it is theirs: only that account's owner can post
from it. Expires in an hour.

**`x_mentions`** — every mention seen, by post id, so a post is acted on once however
often the timeline is read, and what became of it.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "054_tag_on_x"
down_revision: str | None = "053_enterprise"
branch_labels = None
depends_on = None


def _uuid(name: str, *args: Any, **kw: Any) -> sa.Column[Any]:
    return sa.Column(name, postgresql.UUID(as_uuid=True), *args, **kw)


def _now(name: str) -> sa.Column[Any]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def upgrade() -> None:
    op.create_table(
        "x_accounts",
        _uuid("organization_id", sa.ForeignKey("organizations.id"), primary_key=True),
        sa.Column("handle", sa.Text, nullable=False),
        sa.Column("x_user_id", sa.Text, nullable=False),
        sa.Column("read_key_id", sa.Text, nullable=False),
        sa.Column("read_nonce", sa.LargeBinary, nullable=False),
        sa.Column("read_ciphertext", sa.LargeBinary, nullable=False),
        sa.Column("post_key_id", sa.Text, nullable=True),
        sa.Column("post_nonce", sa.LargeBinary, nullable=True),
        sa.Column("post_ciphertext", sa.LargeBinary, nullable=True),
        sa.Column("since_id", sa.Text, nullable=True),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("last_error", sa.Text, nullable=False, server_default=""),
        sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True),
        _now("updated_at"),
    )
    op.create_table(
        "x_links",
        _uuid("id", primary_key=True),
        _uuid("organization_id", sa.ForeignKey("organizations.id"), nullable=False),
        _uuid("member_id", sa.ForeignKey("members.id", ondelete="CASCADE"), nullable=True),
        sa.Column("x_user_id", sa.Text, nullable=False),
        sa.Column("handle", sa.Text, nullable=False),
        _uuid("bot_id", sa.ForeignKey("bots.id", ondelete="SET NULL"), nullable=True),
        _now("created_at"),
        sa.UniqueConstraint("organization_id", "x_user_id", name="uq_x_link_account"),
    )
    op.execute(
        "CREATE UNIQUE INDEX ux_x_links_person ON x_links "
        "(organization_id, coalesce(member_id, '00000000-0000-0000-0000-000000000000'))"
    )
    op.create_table(
        "x_link_codes",
        sa.Column("code", sa.Text, primary_key=True),
        _uuid("organization_id", sa.ForeignKey("organizations.id"), nullable=False),
        _uuid("member_id", sa.ForeignKey("members.id", ondelete="CASCADE"), nullable=True),
        _now("created_at"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "x_mentions",
        sa.Column("post_id", sa.Text, primary_key=True),
        _uuid("organization_id", sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("author_id", sa.Text, nullable=False),
        sa.Column("author_handle", sa.Text, nullable=False, server_default=""),
        sa.Column("outcome", sa.Text, nullable=False),
        _uuid("bot_id", nullable=True),
        _uuid("message_id", nullable=True),
        sa.Column("note", sa.Text, nullable=False, server_default=""),
        _now("created_at"),
    )
    op.create_index("ix_x_mentions_org", "x_mentions", ["organization_id", "created_at"])


def downgrade() -> None:
    op.drop_table("x_mentions")
    op.drop_table("x_link_codes")
    op.drop_table("x_links")
    op.drop_table("x_accounts")
