"""010 evaluations — task_evaluations

Revision ID: 010_evaluations
Revises: 009_tasks

One table, two kinds of row, and that is the entire design.

The manager's verdict and the human's 20% sample land here as separate rows on the
same task, distinguished only by `evaluator_kind`. §8.2 calls the confusion matrix
"real"; this is what makes it real — it is a self-join, not a reconciliation
between the production store and a spreadsheet somebody keeps.

`uq_evaluation_once` allows exactly one row per (task, evaluator_kind, attempt).
The manager gets one verdict per rework attempt; the human gets one review. A
second write on the same key conflicts rather than appending, which is what stops a
retried evaluation run from doubling the denominator of every acceptance metric.

`edit_distance` is stored, not derived at query time. It is computed once from the
submitted and edited artifacts; recomputing it in SQL would mean the dashboard's
"mean edit distance trend" changed retroactively whenever the distance function did.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "010_evaluations"
down_revision: str | None = "009_tasks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_evaluations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column(
            "task_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("tasks.id"), nullable=False
        ),
        # The task's `rework_count` at the moment of judgement. Part of the unique
        # key so a task that comes back after rework gets a second manager verdict
        # rather than conflicting with its first one.
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("evaluator_kind", sa.Text(), nullable=False),
        sa.Column("evaluator_actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("evaluator_ref", sa.Text(), nullable=True),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("rubric", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("reasoning", sa.Text(), nullable=False, server_default=""),
        sa.Column("rework_instructions", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("edit_distance", sa.Integer(), nullable=True),
        sa.Column("cost_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "evaluator_kind IN ('manager','human','system')", name="ck_evaluator_kind"
        ),
        sa.CheckConstraint(
            "outcome IN ('ACCEPTED','ACCEPTED_WITH_EDITS','REWORK_REQUIRED',"
            "'REJECTED','AUTO_ACCEPTED')",
            name="ck_evaluation_outcome",
        ),
        sa.UniqueConstraint("task_id", "evaluator_kind", "attempt", name="uq_evaluation_once"),
    )
    op.create_index("ix_evaluations_task", "task_evaluations", ["task_id"])
    op.create_index("ix_evaluations_kind", "task_evaluations", ["evaluator_kind", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_evaluations_kind", table_name="task_evaluations")
    op.drop_index("ix_evaluations_task", table_name="task_evaluations")
    op.drop_table("task_evaluations")
