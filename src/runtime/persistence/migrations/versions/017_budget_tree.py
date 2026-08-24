"""017 budget tree — pool hierarchy, allocations, priority

Revision ID: 017_budget_tree
Revises: 016_authority

M0 shipped one pool per org with `committed + reserved <= limit` as a CHECK
constraint. This makes it a chain, and the CHECK constraint is left exactly where
it is: I8 now holds *per level* because every row still carries it, and a chain
reservation that would break any level aborts the whole statement.

`depth` is denormalised on purpose. It is the primary key of the lock order —
`ORDER BY depth ASC, id ASC` — and computing it from a recursive walk inside the
locking statement would mean the order depended on a subquery the planner is free
to reorder. A column is a fact; a derived value inside a lock is a hope.

`parent_id` has no cycle constraint at the database level because Postgres cannot
express one. `BudgetRepository.attach_parent` walks the chain before writing, and
`ck_budget_depth` bounds the damage if that check is ever bypassed: a cycle would
have to violate monotonic depth to form.

**Allocations are advisory** (v3 §8). A `budget_allocations` row says "this run
intends to spend up to this much"; it does not hold headroom. That is what makes
oversubscription to `limit x (1 + K)` safe — the hard reservation path is still
bounded by `limit`, so admitting 1.3x of intent cannot produce 1.3x of spend. T31
is the test that says so.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "017_budget_tree"
down_revision: str | None = "016_authority"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "budget_pools", sa.Column("parent_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.add_column(
        "budget_pools",
        sa.Column("depth", sa.SmallInteger(), nullable=False, server_default="0"),
    )
    op.create_foreign_key("fk_pool_parent", "budget_pools", "budget_pools", ["parent_id"], ["id"])
    op.create_check_constraint("ck_budget_depth", "budget_pools", "depth >= 0 AND depth < 16")
    # A root pool has no parent and a child must not claim to be a root. Catches the
    # commonest hand-written-INSERT mistake before it corrupts the lock order.
    op.create_check_constraint(
        "ck_budget_root",
        "budget_pools",
        "(depth = 0 AND parent_id IS NULL) OR (depth > 0 AND parent_id IS NOT NULL)",
    )
    op.create_index("ix_budget_pool_parent", "budget_pools", ["parent_id"])

    op.create_table(
        "budget_allocations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "pool_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("budget_pools.id"),
            nullable=False,
        ),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ceiling_cents", sa.BigInteger(), nullable=False),
        sa.Column("priority", sa.Text(), nullable=False, server_default="NORMAL"),
        sa.Column("status", sa.Text(), nullable=False, server_default="LIVE"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('LIVE','CLOSED')", name="ck_alloc_status"),
        sa.CheckConstraint("priority IN ('LOW','NORMAL','CRITICAL')", name="ck_alloc_priority"),
        sa.CheckConstraint("ceiling_cents >= 0", name="ck_alloc_ceiling_sign"),
        # One live allocation per run. A second would double-count the run's intent
        # against the soft ceiling and make admission monotonically stricter with
        # every retry.
        sa.UniqueConstraint("run_id", name="uq_alloc_run"),
    )
    op.create_index(
        "ix_alloc_live",
        "budget_allocations",
        ["pool_id"],
        postgresql_where=sa.text("status = 'LIVE'"),
    )

    # Every existing pool is a root. Explicit rather than relying on the defaults,
    # so the state after migration is stated rather than inferred.
    op.execute("UPDATE budget_pools SET depth = 0, parent_id = NULL WHERE parent_id IS NULL")


def downgrade() -> None:
    op.drop_index("ix_alloc_live", table_name="budget_allocations")
    op.drop_table("budget_allocations")
    op.drop_index("ix_budget_pool_parent", table_name="budget_pools")
    op.drop_constraint("ck_budget_root", "budget_pools", type_="check")
    op.drop_constraint("ck_budget_depth", "budget_pools", type_="check")
    op.drop_constraint("fk_pool_parent", "budget_pools", type_="foreignkey")
    op.drop_column("budget_pools", "depth")
    op.drop_column("budget_pools", "parent_id")
