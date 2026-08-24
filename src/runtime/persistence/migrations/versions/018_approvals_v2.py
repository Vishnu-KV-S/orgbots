"""018 approvals v2 — escalation chain, approver budgets, resume tokens

Revision ID: 018_approvals_v2
Revises: 017_budget_tree

Three additions, and each one exists because M1's single gate would have failed a
specific way at scale.

**The chain is frozen onto the row**, and so is the TTL. `escalation_chain` and
`ttl_seconds` are resolved once, from the authority the run was admitted under, and
written here. Reading them live at escalation time would mean an approval that started
under one policy escalates under another — and the person it lands on would have no way
to know which one applied to them.

`ttl_seconds` is a column rather than a derivation from `expires_at - created_at`,
which is the obvious shortcut and is wrong: escalation *rewrites* `expires_at`, so
after the first hop the difference measures how late the first approver was rather
than how long the next one has.

**`deferred_until` is how a daily budget is enforced without lengthening a queue.**
M2 §5: exceeding an approver's daily budget raises an alert and does *not* enqueue.
A 40-item queue produces rubber-stamping, which looks like oversight while being its
absence — so the row exists, the run stays blocked, the alert fires, and the item
becomes visible tomorrow. `pending()` filters on this column; nothing deletes.

**`resume_token` is bound to an interrupt, not to a run.** v3 edge case 28: an
approval must not resume a *different* pause in the same run. The token is random
(never derived, so it cannot be recomputed by anything that knows the approval id)
and `interrupt_id` records what it may resume. Redemption checks both, and
`resume_token_used_at` makes it single-use — a replayed resume is refused rather
than silently re-granted.

`escalation_count` was added in 014 and never incremented. It is incremented now,
which is exactly the retrofit that column was created to avoid.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "018_approvals_v2"
down_revision: str | None = "017_budget_tree"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "approvals",
        sa.Column("escalation_chain", postgresql.JSONB(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "approvals",
        sa.Column("escalation_index", sa.SmallInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "approvals",
        sa.Column("max_escalations", sa.SmallInteger(), nullable=False, server_default="0"),
    )
    op.add_column(
        "approvals",
        sa.Column("ttl_seconds", sa.Integer(), nullable=False, server_default="86400"),
    )
    op.add_column("approvals", sa.Column("deferred_until", sa.DateTime(timezone=True)))
    op.add_column("approvals", sa.Column("resume_token", sa.Text(), nullable=True))
    op.add_column("approvals", sa.Column("interrupt_id", sa.Text(), nullable=True))
    op.add_column("approvals", sa.Column("resume_token_used_at", sa.DateTime(timezone=True)))
    op.add_column("approvals", sa.Column("resume_run_id", postgresql.UUID(as_uuid=True)))

    op.create_unique_constraint("uq_approval_resume_token", "approvals", ["resume_token"])
    op.create_index(
        "ix_approvals_queue",
        "approvals",
        ["organization_id", "approver", "expires_at"],
        postgresql_where=sa.text("status = 'PENDING'"),
    )

    # 014 allowed only deny|grant. Escalation and fail_run are M2 vocabulary.
    op.drop_constraint("ck_approval_on_expiry", "approvals", type_="check")
    op.create_check_constraint(
        "ck_approval_on_expiry",
        "approvals",
        "on_expiry IN ('deny','grant','escalate','fail_run')",
    )
    op.create_check_constraint(
        "ck_approval_escalation",
        "approvals",
        "escalation_index >= 0 AND escalation_index <= max_escalations",
    )

    op.create_table(
        "approver_budgets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("approver", sa.Text(), nullable=False),
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("daily_limit", sa.Integer(), nullable=False),
        # Assignments, not decisions. The thing that causes rubber-stamping is the
        # length of the queue a person is handed, not how many they got through.
        sa.Column("assigned", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("alerted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("organization_id", "approver", "day", name="uq_approver_day"),
        sa.CheckConstraint("assigned >= 0 AND daily_limit >= 0", name="ck_approver_budget_signs"),
    )


def downgrade() -> None:
    op.drop_table("approver_budgets")
    op.drop_constraint("ck_approval_escalation", "approvals", type_="check")
    op.drop_constraint("ck_approval_on_expiry", "approvals", type_="check")
    op.create_check_constraint(
        "ck_approval_on_expiry", "approvals", "on_expiry IN ('deny','grant')"
    )
    op.drop_index("ix_approvals_queue", table_name="approvals")
    op.drop_constraint("uq_approval_resume_token", "approvals", type_="unique")
    for column in (
        "ttl_seconds",
        "resume_run_id",
        "resume_token_used_at",
        "interrupt_id",
        "resume_token",
        "deferred_until",
        "max_escalations",
        "escalation_index",
        "escalation_chain",
    ):
        op.drop_column("approvals", column)
