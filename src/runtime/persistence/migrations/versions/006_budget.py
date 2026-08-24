"""006 budget — budget_pools, budget_reservations, usage_ledger

Revision ID: 006_budget
Revises: 005_artifacts

`ck_budget_invariant` is I8 enforced by the database. A bug in reservation logic
becomes a failed transaction rather than a silent overspend.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "006_budget"
down_revision: str | None = "005_artifacts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "budget_pools",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scope_type", sa.Text(), nullable=False),
        sa.Column("scope_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("period", sa.Text(), nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("limit_cents", sa.BigInteger(), nullable=False),
        sa.Column("committed_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("reserved_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("allocated_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.CheckConstraint(
            "committed_cents + reserved_cents <= limit_cents", name="ck_budget_invariant"
        ),
        sa.CheckConstraint("committed_cents >= 0 AND reserved_cents >= 0", name="ck_budget_signs"),
        sa.UniqueConstraint(
            "scope_type", "scope_id", "period", "period_start", name="uq_budget_pool_scope"
        ),
    )

    op.create_table(
        "budget_reservations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "pool_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("budget_pools.id"),
            nullable=False,
        ),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("amount_cents", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("status IN ('HELD','RELEASED','EXPIRED')", name="ck_reservation_status"),
    )
    op.create_index(
        "ix_res_sweep",
        "budget_reservations",
        ["expires_at"],
        postgresql_where=sa.text("status = 'HELD'"),
    )
    op.create_index("ix_res_run", "budget_reservations", ["run_id"])

    op.create_table(
        "usage_ledger",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("root_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("pool_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reservation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("work_class", sa.Text(), nullable=True),
        sa.Column("call_site", sa.Text(), nullable=True),
        sa.Column("provider", sa.Text(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cost_cents", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("cached", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_usage_run", "usage_ledger", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_usage_run", table_name="usage_ledger")
    op.drop_table("usage_ledger")
    op.drop_index("ix_res_run", table_name="budget_reservations")
    op.drop_index("ix_res_sweep", table_name="budget_reservations")
    op.drop_table("budget_reservations")
    op.drop_table("budget_pools")
