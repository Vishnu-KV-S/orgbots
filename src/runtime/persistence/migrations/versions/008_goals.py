"""008 goals — goals, projects

Revision ID: 008_goals
Revises: 007_lg_schema

The smallest thing that makes a task answerable to something. M1 runs one goal and
one project, so neither table earns much structure; what they earn is a foreign key,
so "why was this task created" has an answer that is a join rather than a guess.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "008_goals"
down_revision: str | None = "007_lg_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "goals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("horizon", sa.Text(), nullable=False, server_default="quarter"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("organization_id", "name", name="uq_goal_name"),
    )

    op.create_table(
        "projects",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column(
            "goal_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("goals.id"), nullable=False
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("owner_actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("organization_id", "name", name="uq_project_name"),
    )
    op.create_index("ix_projects_goal", "projects", ["goal_id"])


def downgrade() -> None:
    op.drop_index("ix_projects_goal", table_name="projects")
    op.drop_table("projects")
    op.drop_table("goals")
