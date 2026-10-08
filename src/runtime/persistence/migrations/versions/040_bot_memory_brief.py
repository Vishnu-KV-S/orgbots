"""040 bot memory and brief — long-term memory, and a primary instruction

Revision ID: 040_bot_memory_brief
Revises: 039_bot_appearance

**`bot_memories`** replaces the one `bots.memory` text field. A memory is a row with a
kind (fact, preference, person, skill, episode), an importance, a pin, where it came
from (the bot itself, its person, the bot that created it), and when it was last
recalled — the inputs `runtime.domain.bot_memory` ranks recall by. Rows written by a
run carry a deterministic id, so a replayed step does not remember twice.

**`bots.brief`** replaces `bots.instructions`: the job brief as JSONB
(`runtime.domain.bot_memory.BotBrief`), with `brief_locked` (only the person may change
it) and `brief_rev`. **`bot_brief_revisions`** keeps every version and who wrote it —
the person, the bot itself, or its parent — so a brief a bot rewrote can be read and
restored.

Existing data moves: each line of `memory` becomes a fact, and `instructions` becomes
the brief's `notes` with `description` as its mission.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "040_bot_memory_brief"
down_revision: str | None = "039_bot_appearance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "bot_memories",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "bot_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("bots.id"), nullable=False
        ),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("importance", sa.SmallInteger, nullable=False, server_default="3"),
        sa.Column("pinned", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("source_kind", sa.Text, nullable=False, server_default="self"),
        sa.Column("source_name", sa.Text, nullable=False, server_default=""),
        sa.Column("recall_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_recalled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "kind IN ('fact','preference','person','skill','episode')", name="ck_bot_memory_kind"
        ),
        sa.CheckConstraint("importance BETWEEN 1 AND 5", name="ck_bot_memory_importance"),
        sa.CheckConstraint(
            "source_kind IN ('self','person','parent','system')", name="ck_bot_memory_source"
        ),
    )
    op.create_index("ix_bot_memories_bot", "bot_memories", ["bot_id", "created_at"])

    op.add_column(
        "bots",
        sa.Column("brief", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    )
    op.add_column(
        "bots", sa.Column("brief_locked", sa.Boolean, nullable=False, server_default=sa.false())
    )
    op.add_column("bots", sa.Column("brief_rev", sa.Integer, nullable=False, server_default="0"))

    op.create_table(
        "bot_brief_revisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "bot_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("bots.id"), nullable=False
        ),
        sa.Column("rev", sa.Integer, nullable=False),
        sa.Column("brief", postgresql.JSONB, nullable=False),
        sa.Column("editor_kind", sa.Text, nullable=False),
        sa.Column("editor_bot_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("editor_name", sa.Text, nullable=False, server_default=""),
        sa.Column("reason", sa.Text, nullable=False, server_default=""),
        sa.Column(
            "changed", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("editor_kind IN ('person','self','parent')", name="ck_bot_brief_editor"),
        sa.UniqueConstraint("bot_id", "rev", name="uq_bot_brief_rev"),
    )

    # Move what there is. Memory lines become facts; instructions become the brief.
    op.execute(
        """
        INSERT INTO bot_memories (id, bot_id, kind, content, source_kind)
        SELECT gen_random_uuid(), b.id, 'fact', regexp_replace(trim(line), '^- ', ''), 'self'
          FROM bots b, regexp_split_to_table(b.memory, E'\\n') AS line
         WHERE trim(line) <> ''
        """
    )
    op.execute(
        """
        UPDATE bots
           SET brief = jsonb_build_object('mission', description, 'notes', instructions),
               brief_rev = 1
         WHERE instructions <> '' OR description <> ''
        """
    )
    op.execute(
        """
        INSERT INTO bot_brief_revisions (id, bot_id, rev, brief, editor_kind, editor_name, reason)
        SELECT gen_random_uuid(), id, 1, brief, 'person', '', 'Carried over from instructions'
          FROM bots WHERE brief_rev = 1
        """
    )
    op.drop_column("bots", "memory")
    op.drop_column("bots", "instructions")


def downgrade() -> None:
    op.add_column("bots", sa.Column("instructions", sa.Text, nullable=False, server_default=""))
    op.add_column("bots", sa.Column("memory", sa.Text, nullable=False, server_default=""))
    op.execute("UPDATE bots SET instructions = coalesce(brief->>'notes', '')")
    op.execute(
        """
        UPDATE bots b SET memory = m.lines
          FROM (SELECT bot_id, string_agg('- ' || content, E'\\n' ORDER BY created_at) AS lines
                  FROM bot_memories GROUP BY bot_id) m
         WHERE m.bot_id = b.id
        """
    )
    op.drop_table("bot_brief_revisions")
    op.drop_column("bots", "brief_rev")
    op.drop_column("bots", "brief_locked")
    op.drop_column("bots", "brief")
    op.drop_index("ix_bot_memories_bot", table_name="bot_memories")
    op.drop_table("bot_memories")
