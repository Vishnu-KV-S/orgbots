"""042 team files — a shared drive for each team of bots

Revision ID: 042_team_files
Revises: 041_login_vault

**`bots.team_id`** says which team a bot is on. A bot a person creates starts a team
(its own id); a helper joins its parent's. It is written once, at creation, and never
follows `parent_bot_id` afterwards: deleting a team's lead moves its helpers up a level
(`BotManager.delete`), and they keep the team — and its files — rather than each
becoming a team of one that can no longer see what they built together. Existing bots
are backfilled to the root of their tree, which is what the rule would have given them.

**`team_files`** is the drive: text at a path, per team. The path is matched without
regard to case (`ux_team_files_live_path`, live files only), so a deleted file frees its
path. `version` counts every change; a writer that read version 3 and finds version 4 has
been overtaken by a teammate. `chars` is kept beside the content so that listing a drive
does not read every file in it. `locked` is the person's: a locked file is one no bot
may change.

**`team_file_revisions`** is every change, with the content as it was after it, who made
it and from which run — the history a person restores from. A revision a run writes has
a derived id (`domain.files.file_op_id`), so a replayed step finds its change already
made instead of making it again. Pruned per file to `MAX_REVISIONS`.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "042_team_files"
down_revision: str | None = "041_login_vault"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("bots", sa.Column("team_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.execute(
        """
        WITH RECURSIVE up AS (
            SELECT id AS bot_id, id AS ancestor, parent_bot_id, 0 AS depth FROM bots
            UNION ALL
            SELECT up.bot_id, b.id, b.parent_bot_id, up.depth + 1
              FROM bots b JOIN up ON b.id = up.parent_bot_id
             WHERE up.depth < 16
        ), root AS (
            SELECT DISTINCT ON (bot_id) bot_id, ancestor FROM up ORDER BY bot_id, depth DESC
        )
        UPDATE bots SET team_id = root.ancestor FROM root WHERE bots.id = root.bot_id
        """
    )
    op.alter_column("bots", "team_id", nullable=False)
    op.create_index(
        "ix_bots_team_live", "bots", ["team_id"], postgresql_where=sa.text("deleted_at IS NULL")
    )

    op.create_table(
        "team_files",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("team_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("path", sa.Text, nullable=False),
        sa.Column("content", sa.Text, nullable=False, server_default=""),
        sa.Column("chars", sa.Integer, nullable=False, server_default="0"),
        sa.Column("version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("locked", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("created_by_kind", sa.Text, nullable=False),
        sa.Column("created_by_bot_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_by_name", sa.Text, nullable=False, server_default=""),
        sa.Column("updated_by_kind", sa.Text, nullable=False),
        sa.Column("updated_by_bot_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_by_name", sa.Text, nullable=False, server_default=""),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("path LIKE '/%'", name="ck_team_file_path"),
        sa.CheckConstraint("chars = char_length(content)", name="ck_team_file_chars"),
        sa.CheckConstraint(
            "created_by_kind IN ('person','bot') AND updated_by_kind IN ('person','bot')",
            name="ck_team_file_editor",
        ),
    )
    op.create_index(
        "ux_team_files_live_path",
        "team_files",
        ["team_id", sa.text("lower(path)")],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    op.create_table(
        "team_file_revisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "file_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("team_files.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.Integer, nullable=False),
        sa.Column("op", sa.Text, nullable=False),
        sa.Column("path", sa.Text, nullable=False),
        sa.Column("content", sa.Text, nullable=False, server_default=""),
        sa.Column("editor_kind", sa.Text, nullable=False),
        sa.Column("editor_bot_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("editor_name", sa.Text, nullable=False, server_default=""),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("note", sa.Text, nullable=False, server_default=""),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "op IN ('create','write','append','edit','move','delete','restore')",
            name="ck_team_file_rev_op",
        ),
        sa.UniqueConstraint("file_id", "version", name="uq_team_file_rev"),
    )


def downgrade() -> None:
    op.drop_table("team_file_revisions")
    op.drop_index("ux_team_files_live_path", table_name="team_files")
    op.drop_table("team_files")
    op.drop_index("ix_bots_team_live", table_name="bots")
    op.drop_column("bots", "team_id")
