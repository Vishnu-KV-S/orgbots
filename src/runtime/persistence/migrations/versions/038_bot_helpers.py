"""038 bot helpers — a bot can create bots under it

Revision ID: 038_bot_helpers
Revises: 037_bots

`parent_bot_id` makes the bots a tree: a person's bots at the top, and the helpers a
bot created for itself under it. `created_by` says which of the two made a row, so the
UI can say "created by Researcher" and the API can refuse a bot creating helpers past
the depth limit without walking the tree twice.

**Deleting a parent never silently takes its helpers with it.** The person chooses:
delete the helpers too, or keep them and move them up a level. That is why the foreign
key has no `ON DELETE CASCADE` — bots are soft-deleted anyway, and the choice is made
in `BotManager.delete`, not by the database.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "038_bot_helpers"
down_revision: str | None = "037_bots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "bots",
        sa.Column(
            "parent_bot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("bots.id", name="fk_bot_parent"),
            nullable=True,
        ),
    )
    op.add_column(
        "bots",
        sa.Column("created_by", sa.Text, nullable=False, server_default="person"),
    )
    op.create_check_constraint("ck_bot_created_by", "bots", "created_by IN ('person','bot')")
    op.create_check_constraint(
        "ck_bot_not_own_parent", "bots", "parent_bot_id IS NULL OR parent_bot_id <> id"
    )
    op.create_index(
        "ix_bots_parent_live",
        "bots",
        ["parent_bot_id"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_bots_parent_live", table_name="bots")
    op.drop_constraint("ck_bot_not_own_parent", "bots", type_="check")
    op.drop_constraint("ck_bot_created_by", "bots", type_="check")
    op.drop_column("bots", "created_by")
    op.drop_column("bots", "parent_bot_id")
