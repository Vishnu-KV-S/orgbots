"""049 bot connectors — MCP servers a person connected for their bots

Revision ID: 049_bot_connectors
Revises: 048_bot_auto_review

**`bot_connectors`** is one organization's apps: an MCP server's URL, how to
authenticate to it (`auth_kind`, and a header name for `header`), the token sealed like
a vault entry (the API seals it, the gateway opens it for a call, nothing else reads
it), and the tools the server listed when it was connected — kept so a bot's prompt can
name them without a round trip. `status` and `last_error` say whether the last
connection worked. A name is unique per organization among live connectors, because a
bot calls one by it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "049_bot_connectors"
down_revision: str | None = "048_bot_auto_review"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bot_connectors",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("title", sa.Text, nullable=False, server_default=""),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("auth_kind", sa.Text, nullable=False, server_default="none"),
        sa.Column("header_name", sa.Text, nullable=False, server_default=""),
        sa.Column("secret_key_id", sa.Text, nullable=True),
        sa.Column("secret_nonce", sa.LargeBinary, nullable=True),
        sa.Column("secret_ciphertext", sa.LargeBinary, nullable=True),
        sa.Column("tools", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("server_name", sa.Text, nullable=False, server_default=""),
        sa.Column("status", sa.Text, nullable=False, server_default="ok"),
        sa.Column("last_error", sa.Text, nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("catalog_key", sa.Text, nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("auth_kind IN ('none','bearer','header')", name="ck_connector_auth"),
        sa.CheckConstraint("status IN ('ok','error')", name="ck_connector_status"),
    )
    op.create_index(
        "ux_bot_connectors_live_name",
        "bot_connectors",
        ["organization_id", "name"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ux_bot_connectors_live_name", table_name="bot_connectors")
    op.drop_table("bot_connectors")
