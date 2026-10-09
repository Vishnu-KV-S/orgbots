"""043 bot routines — work a bot does on a schedule or when an event arrives

Revision ID: 043_bot_routines
Revises: 042_team_files

**`bot_routines`** is the standing instruction and its trigger: a 5-field cron and an
IANA timezone (`kind = 'schedule'`), or a webhook source with a match (`'event'`).
`next_fire_at` is a cursor the runner reads with an index, so a tick costs one query
however many routines exist; `last_evaluated_at` is where the next evaluation starts,
so a worker that was down enumerates what it missed (and records it as missed rather
than running it). An event routine's URL carries `token`; a GitHub or Slack signing
secret is kept as ciphertext, like the vault's, under the credential cipher.

**`bot_routine_runs`** is every firing — and the queue. A firing is written `queued`
first, keyed by `domain.routines.fire_id` (the occurrence, or the event's delivery id),
so two runners or a retried webhook insert the same row and the second does nothing.
The runner starts a queued firing when its bot is free, and marks it `started`,
`refused`, or `skipped` (it waited too long, or the routine went away). Pruned per
routine to the last `RUNS_KEPT`.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "043_bot_routines"
down_revision: str | None = "042_team_files"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bot_routines",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column(
            "bot_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("bots.id"), nullable=False
        ),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("instruction", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("cron", sa.Text, nullable=True),
        sa.Column("timezone", sa.Text, nullable=False, server_default="UTC"),
        sa.Column("source", sa.Text, nullable=True),
        sa.Column("match", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("token", sa.Text, nullable=True),
        sa.Column("secret_key_id", sa.Text, nullable=True),
        sa.Column("secret_nonce", sa.LargeBinary, nullable=True),
        sa.Column("secret_ciphertext", sa.LargeBinary, nullable=True),
        sa.Column("inputs", sa.Text, nullable=False, server_default=""),
        sa.Column("output", sa.Text, nullable=False, server_default=""),
        sa.Column("approval", sa.Text, nullable=False, server_default="default"),
        sa.Column("when_missing", sa.Text, nullable=False, server_default=""),
        sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_by_kind", sa.Text, nullable=False),
        sa.Column("created_by_bot_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("next_fire_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_fired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fire_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind IN ('schedule','event')", name="ck_bot_routine_kind"),
        sa.CheckConstraint(
            "(kind = 'schedule' AND cron IS NOT NULL) OR "
            "(kind = 'event' AND source IN ('webhook','github','slack') AND token IS NOT NULL)",
            name="ck_bot_routine_trigger",
        ),
        sa.CheckConstraint("approval IN ('default','drafts')", name="ck_bot_routine_approval"),
        sa.CheckConstraint("created_by_kind IN ('person','bot')", name="ck_bot_routine_created_by"),
    )
    op.create_index(
        "ux_bot_routines_live_name",
        "bot_routines",
        ["bot_id", sa.text("lower(name)")],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_bot_routines_due",
        "bot_routines",
        ["next_fire_at"],
        postgresql_where=sa.text("deleted_at IS NULL AND active AND kind = 'schedule'"),
    )

    op.create_table(
        "bot_routine_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "routine_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("bot_routines.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("bot_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("trigger", sa.Text, nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text, nullable=False, server_default="queued"),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("detail", sa.Text, nullable=False, server_default=""),
        sa.Column("event", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "trigger IN ('schedule','event','test')", name="ck_bot_routine_run_trigger"
        ),
        sa.CheckConstraint(
            "status IN ('queued','started','refused','skipped','missed')",
            name="ck_bot_routine_run_status",
        ),
    )
    op.create_index(
        "ix_bot_routine_runs_queued",
        "bot_routine_runs",
        ["created_at"],
        postgresql_where=sa.text("status = 'queued'"),
    )
    op.create_index("ix_bot_routine_runs_routine", "bot_routine_runs", ["routine_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_bot_routine_runs_routine", table_name="bot_routine_runs")
    op.drop_index("ix_bot_routine_runs_queued", table_name="bot_routine_runs")
    op.drop_table("bot_routine_runs")
    op.drop_index("ix_bot_routines_due", table_name="bot_routines")
    op.drop_index("ux_bot_routines_live_name", table_name="bot_routines")
    op.drop_table("bot_routines")
