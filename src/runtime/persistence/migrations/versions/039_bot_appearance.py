"""039 bot appearance — what a bot's 3D body looks like

Revision ID: 039_bot_appearance
Revises: 038_bot_helpers

One JSONB column of presentation: body shape, colours, eye style, accessory, finish.
Validated by `runtime.domain.bots.BotAppearance` on the way in; an empty object means
"derive one from the bot's id", which is how a helper a bot created gets a distinct,
stable look without anybody choosing one.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "039_bot_appearance"
down_revision: str | None = "038_bot_helpers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "bots",
        sa.Column(
            "appearance",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("bots", "appearance")
