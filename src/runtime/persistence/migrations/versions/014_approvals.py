"""014 approvals — approvals

Revision ID: 014_approvals
Revises: 013_triggers

Minimal by design: one gate, one approver, a TTL, `on_expiry = deny`, and no
escalation chain. §2 puts the full authority model in M2.

`escalation_count` exists as a column and is never incremented in M1. That is a
deliberate choice rather than an oversight — the alternative is an `ALTER TABLE` on
the approvals table at exactly the moment M2 is adding escalation logic to it, and
a zero-valued column costs nothing.

`uq_approval_subject` makes requesting an approval idempotent: the same action on
the same subject is one approval, so a re-run of the gate does not create a second
pending request for a human to answer twice.

Decisions are recorded once. A second decision does not overwrite the first — the
conditional UPDATE requires `status = 'PENDING'` and fails silently, and the caller
records the loser in `audit_log`. First writer wins, second is remembered. T23.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "014_approvals"
down_revision: str | None = "013_triggers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "approvals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("subject_type", sa.Text(), nullable=False),
        sa.Column("subject_id", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("detail", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("requested_by_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("requested_by_actor", sa.Text(), nullable=True),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("approver", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="PENDING"),
        sa.Column("on_expiry", sa.Text(), nullable=False, server_default="deny"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_by", sa.Text(), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        # M2. Never incremented in M1; present so adding escalation is logic, not DDL.
        sa.Column("escalation_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("subject_type", "subject_id", "action", name="uq_approval_subject"),
        sa.CheckConstraint(
            "status IN ('PENDING','GRANTED','DENIED','EXPIRED')", name="ck_approval_status"
        ),
        sa.CheckConstraint("on_expiry IN ('deny','grant')", name="ck_approval_on_expiry"),
    )
    op.create_index(
        "ix_approvals_pending",
        "approvals",
        ["expires_at"],
        postgresql_where=sa.text("status = 'PENDING'"),
    )
    op.create_index("ix_approvals_subject", "approvals", ["subject_type", "subject_id"])


def downgrade() -> None:
    op.drop_index("ix_approvals_subject", table_name="approvals")
    op.drop_index("ix_approvals_pending", table_name="approvals")
    op.drop_table("approvals")
