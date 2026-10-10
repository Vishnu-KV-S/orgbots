"""056 bot check-ins — a bot wakes itself later to look again

Revision ID: 056_bot_checks
Revises: 055_bot_screenshots

"Message him and reply when he answers" is not one turn. Until now a bot that sent the
message could only end its turn and ask its person to say "continue" later — the reply
came, and nobody looked. A check-in is the bot's own `check_back`: a wake of itself,
queued for a time, carrying the task it wrote for its later self.

It rides the queue a bot's other wakes ride (`bot_wakes`): started only when the bot is
free, claimed once, and dropped with the reason after waiting too long. Two columns:
`due_at`, before which the runner leaves it alone, and `task`. For a check, `hops`
counts the check-ins in a row, which `MAX_CHECKS` bounds — a watch that never sees
anything happen ends, and says so.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "056_bot_checks"
down_revision: str | None = "055_bot_screenshots"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bot_wakes", sa.Column("due_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("bot_wakes", sa.Column("task", sa.Text, nullable=False, server_default=""))
    op.drop_constraint("ck_bot_wake_kind", "bot_wakes", type_="check")
    op.create_check_constraint(
        "ck_bot_wake_kind", "bot_wakes", "kind IN ('group','message','check')"
    )


def downgrade() -> None:
    op.execute("DELETE FROM bot_wakes WHERE kind = 'check'")
    op.drop_constraint("ck_bot_wake_kind", "bot_wakes", type_="check")
    op.create_check_constraint("ck_bot_wake_kind", "bot_wakes", "kind IN ('group','message')")
    op.drop_column("bot_wakes", "task")
    op.drop_column("bot_wakes", "due_at")
