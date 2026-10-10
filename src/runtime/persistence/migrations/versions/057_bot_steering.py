"""057 bot steering — a message sent while the bot works joins the work

Revision ID: 057_bot_steering
Revises: 056_bot_checks

Until now every message from the person superseded the turn in progress: the running
run was told to stop and a fresh one began with nothing but the conversation — not the
plan, the notes or a word of what the bot had been doing. "I mean on Claude", sent while
the bot was searching Google, reached a bot that saw a Google results page and carried
on with it.

Now a message that arrives while a plain turn is working is *steered* into it: recorded,
and read by the running turn at its next step with everything it knows. One column says
which turn accepts that — `steer_turn`, set to the turn's number while it runs and
cleared, under the bot row's lock, when it ends. The lock is what makes it safe: a
message either finds the turn still open (and the turn's end will see the message) or
finds it closed (and starts a turn of its own), never neither.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "057_bot_steering"
down_revision: str | None = "056_bot_checks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bots", sa.Column("steer_turn", sa.Integer, nullable=True))


def downgrade() -> None:
    op.drop_column("bots", "steer_turn")
