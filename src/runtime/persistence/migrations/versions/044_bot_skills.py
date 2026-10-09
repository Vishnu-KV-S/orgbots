"""044 bot skills — the organization's library of how-tos, and demonstrations

Revision ID: 044_bot_skills
Revises: 043_bot_routines

**`bot_skills`** is one library per organization, not per bot: a procedure one bot
learned is one every bot can follow. A name is unique per organization without regard
to case among live skills (`ux_bot_skills_live_name`), because people and bots call a
skill by it — `/weekly-vendor-check`. `status` is `draft` until a person has read it
(a demonstration's write-up always starts there) and `ready` after; only ready skills
are listed to bots. `version` counts every change; `source` says where it came from.

**`bot_recordings`** is a demonstration: the goal the person stated and the steps the
computer recorded while they did the task on a bot's screen — clicks, typing (secrets
masked by the computer, never kept), keys, scrolls and pages. A recording is written
when it stops; while it runs, the steps live in the computer process.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "044_bot_skills"
down_revision: str | None = "043_bot_routines"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bot_skills",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("title", sa.Text, nullable=False, server_default=""),
        sa.Column("when_to_use", sa.Text, nullable=False, server_default=""),
        sa.Column("inputs", sa.Text, nullable=False, server_default=""),
        sa.Column(
            "steps", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("checks", sa.Text, nullable=False, server_default=""),
        sa.Column("output", sa.Text, nullable=False, server_default=""),
        sa.Column("approvals", sa.Text, nullable=False, server_default=""),
        sa.Column("status", sa.Text, nullable=False, server_default="ready"),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("source_bot_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("recording_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("updated_by_kind", sa.Text, nullable=False, server_default="person"),
        sa.Column("updated_by_name", sa.Text, nullable=False, server_default=""),
        sa.Column("use_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('draft','ready')", name="ck_bot_skill_status"),
        sa.CheckConstraint(
            "source IN ('person','bot','demonstration','marketplace')", name="ck_bot_skill_source"
        ),
        sa.CheckConstraint(
            "updated_by_kind IN ('person','bot')", name="ck_bot_skill_updated_by"
        ),
    )
    op.create_index(
        "ux_bot_skills_live_name",
        "bot_skills",
        ["organization_id", sa.text("lower(name)")],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    op.create_table(
        "bot_recordings",
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
        sa.Column("goal", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False, server_default="recording"),
        sa.Column(
            "steps", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("skill_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('recording','stopped','cancelled')", name="ck_bot_recording_status"
        ),
    )
    op.create_index(
        "ux_bot_recordings_live",
        "bot_recordings",
        ["bot_id"],
        unique=True,
        postgresql_where=sa.text("status = 'recording'"),
    )


def downgrade() -> None:
    op.drop_index("ux_bot_recordings_live", table_name="bot_recordings")
    op.drop_table("bot_recordings")
    op.drop_index("ux_bot_skills_live_name", table_name="bot_skills")
    op.drop_table("bot_skills")
