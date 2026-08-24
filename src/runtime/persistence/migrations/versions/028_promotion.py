"""028 promotion — memory_promotions

Revision ID: 028_promotion
Revises: 027_procedures

§8: *"Promotion is a queue an actor proposes into, not a side effect of writing. Every
promotion writes a `memory_promotions` row with reviewer and rationale, so a bad
company-scoped fact is traceable to a decision."*

The table is small and three of its constraints are doing the actual work.

**`ck_promotion_one_rung`** encodes `PROMOTION_PATH` in the database: private →
department, department → company, and nothing else. Private → company in one step is
not representable. Two rungs mean two reviews, and the second reviewer is answering a
different question from the first — *"should everyone see this"* rather than *"is this
worth more than one actor knowing"*.

**`ck_promotion_decided_has_reviewer`** is the anti-auto-promotion constraint. An
APPROVED row with no reviewer cannot be inserted. T53 asserts that no code path
produces one; this is what stops the code path that gets written next year, by someone
who has not read §8, from working.

**`uq_promotion_open`** stops the queue from filling with the same proposal. An actor
whose retrieval keeps surfacing the same private fact will keep proposing it; the
partial unique index makes the second proposal a no-op while the first is undecided,
and permits a fresh proposal after a rejection — because a fact rejected in March may
genuinely deserve a second look in September, and a permanent block would be a decision
nobody made.

`reviewer_kind` distinguishes the cheap-model classifier from the human sample. Both
land in one table for the same reason M1's evaluations do: the confusion matrix between
them is a self-join, not a reconciliation between two stores. When the classifier starts
approving things the humans reject, that is a number here.

`work_class=MEMORY` spend for the review calls is accounted in `usage_ledger` like every
other model call — §8 asks for review to have "its own budget line" and that is what a
work class *is* in this codebase, so there is no second ledger here either.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "028_promotion"
down_revision: str | None = "027_procedures"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "memory_promotions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("memory_id", sa.Text(), nullable=False),
        sa.Column("from_scope", sa.Text(), nullable=False),
        sa.Column("to_scope", sa.Text(), nullable=False),
        sa.Column("to_scope_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("proposed_by", sa.Text(), nullable=False),
        sa.Column("proposed_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "proposed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("status", sa.Text(), nullable=False, server_default="PROPOSED"),
        sa.Column("reviewer", sa.Text(), nullable=True),
        sa.Column("reviewer_kind", sa.Text(), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column(
            "criteria", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        # The five §8 criteria as booleans, so "why was this approved" survives the
        # rationale prose going stale: durable, non_obvious, generalises, novel,
        # not_quarantined. A rationale is what a reviewer thought; these are what they
        # checked, and only the second kind can be aggregated.
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("promoted_memory_id", sa.Text(), nullable=True),
        sa.Column("review_cost_cents", sa.Integer(), nullable=False, server_default="0"),
        sa.CheckConstraint(
            "status IN ('PROPOSED','APPROVED','REJECTED','WITHDRAWN')", name="ck_promotion_status"
        ),
        sa.CheckConstraint(
            "reviewer_kind IS NULL OR reviewer_kind IN ('model','human')",
            name="ck_promotion_reviewer_kind",
        ),
        sa.CheckConstraint(
            "(from_scope = 'private' AND to_scope = 'department') OR "
            "(from_scope = 'department' AND to_scope = 'company')",
            name="ck_promotion_one_rung",
        ),
        sa.CheckConstraint(
            "status IN ('PROPOSED','WITHDRAWN') OR "
            "(reviewer IS NOT NULL AND reviewer_kind IS NOT NULL AND decided_at IS NOT NULL)",
            name="ck_promotion_decided_has_reviewer",
        ),
        sa.CheckConstraint(
            "status <> 'APPROVED' OR promoted_memory_id IS NOT NULL",
            name="ck_promotion_approved_has_result",
        ),
    )
    op.create_index(
        "uq_promotion_open",
        "memory_promotions",
        ["organization_id", "memory_id", "to_scope"],
        unique=True,
        postgresql_where=sa.text("status = 'PROPOSED'"),
    )
    op.create_index(
        "ix_promotion_queue",
        "memory_promotions",
        ["organization_id", "proposed_at"],
        postgresql_where=sa.text("status = 'PROPOSED'"),
    )
    op.create_index("ix_promotion_memory", "memory_promotions", ["memory_id"])


def downgrade() -> None:
    op.drop_index("ix_promotion_memory", table_name="memory_promotions")
    op.drop_index("ix_promotion_queue", table_name="memory_promotions")
    op.drop_index("uq_promotion_open", table_name="memory_promotions")
    op.drop_table("memory_promotions")
