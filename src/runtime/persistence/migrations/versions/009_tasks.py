"""009 tasks — tasks, and `runs.task_id`

Revision ID: 009_tasks
Revises: 008_goals

Two things happen here and the second is easy to miss.

**`tasks`** carries the lifecycle, the pinned schemas, the outcome, the counters
that cap rework and schema failures, and its own lease. The lease is not a copy of
the run lease: a run executes for minutes, a task is owned for as long as an
assignee is working on it, and T21 is about two workers claiming one *task*.
`version` is the optimistic-concurrency column that makes that claim a single
conditional UPDATE.

**`runs.task_id`** is the column every metric in §7 is computed through. Without it
"what did this task cost" requires parsing run inputs, and the coordination-ratio
diagnosis in §10 ("find the top three COORDINATION calls by cost") has nothing to
group by. It is nullable because the weekly cron runs and the dispatcher's own runs
belong to no task — and that nullability is itself load-bearing: cost with no task
is exactly the overhead the gate is trying to measure.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "009_tasks"
down_revision: str | None = "008_goals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column(
            "project_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("projects.id"), nullable=True
        ),
        sa.Column(
            "goal_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("goals.id"), nullable=True
        ),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parent_task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("objective", sa.Text(), nullable=False),
        sa.Column("acceptance_criteria", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("created_by_actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("assignee_actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("assignee_name", sa.Text(), nullable=True),
        # Pinned schemas. `input_schema_ref` is nullable in M1 — typed chaining
        # between tasks is M2 — but the column exists now so adding it later is a
        # backfill rather than a migration on a hot table.
        sa.Column("input_schema_ref", sa.Text(), nullable=True),
        sa.Column("output_schema_ref", sa.Text(), nullable=False),
        sa.Column("input", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("output", postgresql.JSONB(), nullable=True),
        sa.Column("output_artifact_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("outcome_reason", sa.Text(), nullable=True),
        sa.Column("rework_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("schema_failures", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_schema_errors", postgresql.JSONB(), nullable=True),
        sa.Column("human_touched", sa.Boolean(), nullable=False, server_default="false"),
        # Task lease. Separate from the run lease: T21 is two workers claiming one
        # task, which is a different race from two workers claiming one run.
        sa.Column("lease_worker", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("eval_deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "status IN ('DRAFT','ASSIGNED','IN_PROGRESS','SUBMITTED','CLOSED','CANCELLED')",
            name="ck_task_status",
        ),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ('ACCEPTED','ACCEPTED_WITH_EDITS',"
            "'REWORK_REQUIRED','REJECTED','AUTO_ACCEPTED')",
            name="ck_task_outcome",
        ),
        # The caps are invariants, not application conventions. A bug that tries to
        # write a fourth rework fails the transaction. §2 sets the cap at 2.
        sa.CheckConstraint("rework_count >= 0 AND rework_count <= 2", name="ck_task_rework_cap"),
        sa.CheckConstraint(
            "schema_failures >= 0 AND schema_failures <= 3", name="ck_task_schema_cap"
        ),
    )
    op.create_index("ix_tasks_status", "tasks", ["status", "assignee_name"])
    op.create_index("ix_tasks_correlation", "tasks", ["correlation_id"])
    op.create_index(
        "ix_tasks_eval_due",
        "tasks",
        ["eval_deadline"],
        postgresql_where=sa.text("status = 'SUBMITTED' AND outcome IS NULL"),
    )
    op.create_index("ix_tasks_created", "tasks", ["organization_id", "created_at"])

    op.add_column("runs", sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.create_index(
        "ix_runs_task", "runs", ["task_id"], postgresql_where=sa.text("task_id IS NOT NULL")
    )


def downgrade() -> None:
    op.drop_index("ix_runs_task", table_name="runs")
    op.drop_column("runs", "task_id")
    op.drop_index("ix_tasks_created", table_name="tasks")
    op.drop_index("ix_tasks_eval_due", table_name="tasks")
    op.drop_index("ix_tasks_correlation", table_name="tasks")
    op.drop_index("ix_tasks_status", table_name="tasks")
    op.drop_table("tasks")
