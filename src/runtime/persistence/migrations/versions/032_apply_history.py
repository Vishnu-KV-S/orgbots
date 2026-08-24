"""032 apply history — plans, and what each apply actually did

Revision ID: 032_apply_history
Revises: 031_spec_docs

M4 §9. Two tables and one column carries the whole design: `plan_hash`.

**Plan/apply skew** (edge case 76). You run `plan`, read the diff, and go and think
about it. Somebody else applies something. You run `apply`. Without a recorded hash
the applier would happily apply *your* diff to *their* state, and the result is a
change nobody reviewed. So the applier recomputes the plan from live state and
refuses if the hash moved, and a plan older than 15 minutes is `STALE` regardless of
whether anything moved — because a plan that is right by luck is not a plan that was
checked.

**`apply_events` is per changed object, not per apply.** "Which apply changed this
actor's authority" is the question an incident asks, and one row per apply with a
JSON blob in it answers that question by grep. The row is written inside the same
transaction as the change it describes, so a rolled-back apply leaves no events —
which is the property that makes this table trustworthy rather than merely present.

`status` is text rather than an enum for the same reason every other closed
vocabulary here is: the application is the authority and adding a value must not
need a migration.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "032_apply_history"
down_revision: str | None = "031_spec_docs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "apply_plans",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        # **No foreign key**, unlike every other `organization_id` in this schema, and
        # the exception is load-bearing: a plan can be saved for an organization that
        # does not exist yet. That is the *first* apply — the one most worth reviewing
        # before running — and an FK here would make `plan --save` fail exactly then.
        # `apply_events` keeps its FK, because an event is only ever written after the
        # apply has created the organization.
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("plan", postgresql.JSONB(), nullable=False),
        sa.Column("plan_hash", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False, server_default="operator"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="PENDING"),
        sa.CheckConstraint(
            "status IN ('PENDING','APPLIED','STALE','ABANDONED')", name="ck_apply_plan_status"
        ),
    )
    op.create_index(
        "ix_apply_plans_pending",
        "apply_plans",
        ["organization_id", "created_at"],
        postgresql_where=sa.text("status = 'PENDING'"),
    )

    op.create_table(
        "apply_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "plan_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("apply_plans.id"), nullable=True
        ),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("detail", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("applied_by", sa.Text(), nullable=False, server_default="operator"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "action IN ('create','update','deactivate','unchanged','rename')",
            name="ck_apply_event_action",
        ),
    )
    op.create_index("ix_apply_events_object", "apply_events", ["organization_id", "kind", "name"])


def downgrade() -> None:
    op.drop_index("ix_apply_events_object", table_name="apply_events")
    op.drop_table("apply_events")
    op.drop_index("ix_apply_plans_pending", table_name="apply_plans")
    op.drop_table("apply_plans")
