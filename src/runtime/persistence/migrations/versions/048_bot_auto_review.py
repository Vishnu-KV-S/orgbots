"""048 bot auto review — a second model checks a bot's risky steps

Revision ID: 048_bot_auto_review
Revises: 047_bot_groups

One switch per bot, the person's (`domain.review`). Off by default: a review is a model
call on every risky step, and a person who has not asked for it should not pay for it.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "048_bot_auto_review"
down_revision: str | None = "047_bot_groups"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "bots", sa.Column("auto_review", sa.Boolean, nullable=False, server_default=sa.false())
    )


def downgrade() -> None:
    op.drop_column("bots", "auto_review")
