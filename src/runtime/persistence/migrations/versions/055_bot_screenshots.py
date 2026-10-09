"""055 bot screenshots — what the bot's screen looked like, shown in the chat

Revision ID: 055_bot_screenshots
Revises: 054_tag_on_x

A bot works in a browser the person mostly is not watching, and a sentence is a poor
substitute for the page: "I've added it to the cart" is a claim, a picture of the cart
is evidence. So at the moments a person decides something — an approval, a sign-in
form, a CAPTCHA, a look, or a reply the bot chose to illustrate — the run keeps a
screenshot and the message carries its id.

**Masked like everything a bot sees.** The image is the computer's `bot_screenshot`:
password, code and card fields and anything the vault filled are grey boxes. Stored
pictures are stored data, and the vault's promise — a value exists only as
ciphertext — would otherwise end at the first screenshot of a filled form.

**Bounded.** A JPEG of the viewport is tens of kilobytes; each bot keeps its most
recent `KEEP_PER_BOT` (`runtime.org.bots`) and the rest are deleted as new ones arrive.
A message whose picture has aged out says so in the UI rather than breaking. Ids are
derived from `(run, step, kind)`, so a replayed step stores nothing twice.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "055_bot_screenshots"
down_revision: str | None = "054_tag_on_x"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bot_screenshots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "bot_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("bots.id"), nullable=False
        ),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("page_url", sa.Text, nullable=False, server_default=""),
        sa.Column("media_type", sa.Text, nullable=False, server_default="image/jpeg"),
        sa.Column("data", sa.LargeBinary, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "media_type IN ('image/jpeg','image/png')", name="ck_bot_screenshot_media"
        ),
    )
    op.create_index("ix_bot_screenshots_bot_created", "bot_screenshots", ["bot_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_bot_screenshots_bot_created", table_name="bot_screenshots")
    op.drop_table("bot_screenshots")
