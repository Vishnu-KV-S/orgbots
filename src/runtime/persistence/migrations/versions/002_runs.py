"""002 runs — runs, run_specs

Revision ID: 002_runs
Revises: 001_foundations
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "002_runs"
down_revision: str | None = "001_foundations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("root_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("parent_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "actor_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("actors.id"), nullable=False
        ),
        sa.Column(
            "actor_version_id",
            sa.BigInteger(),
            sa.ForeignKey("actor_versions.id"),
            nullable=False,
        ),
        sa.Column("session_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("thread_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("status_reason", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("priority", sa.SmallInteger(), nullable=False, server_default="50"),
        sa.Column("worker_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fence", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("lease_expiries", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("depth", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        # Load-bearing for cron dedupe, stream redelivery and API retry at once.
        # Not partial, not nullable — see the M0 plan, §2.
        sa.UniqueConstraint("organization_id", "idempotency_key", name="uq_run_idem"),
    )
    op.create_index(
        "ix_runs_claimable",
        "runs",
        ["status", "lease_until"],
        postgresql_where=sa.text("status IN ('QUEUED','RUNNING')"),
    )
    op.create_index("ix_runs_root", "runs", ["root_run_id"])

    op.create_table(
        "run_specs",
        sa.Column(
            "run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("runs.id"), primary_key=True
        ),
        sa.Column("spec", postgresql.JSONB(), nullable=False),
        sa.Column("spec_hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_run_specs_hash", "run_specs", ["spec_hash"])


def downgrade() -> None:
    op.drop_index("ix_run_specs_hash", table_name="run_specs")
    op.drop_table("run_specs")
    op.drop_index("ix_runs_root", table_name="runs")
    op.drop_index("ix_runs_claimable", table_name="runs")
    op.drop_table("runs")
