"""013 triggers — triggers, trigger_fires

Revision ID: 013_triggers
Revises: 012_sessions

`trigger_fires` is the interesting table and its primary key is the whole design.

    trigger_key = H(trigger_id, scheduled_for_utc)

Three separate hazards collapse into one primary-key conflict:

- **Racing schedulers.** Two processes evaluate the same cron minute; both compute
  the same key; one INSERT wins and the other gets a conflict and does nothing.
  T18.
- **Redelivery.** A scheduler that crashes after firing and before recording... has
  nothing to re-record, because the row is written *before* the run is started and
  carries the run id afterwards.
- **Catch-up.** After 48 hours down, the scheduler enumerates every occurrence it
  missed and, under `skip`, inserts a row for each with `skipped = true` and starts
  a run only for the most recent. The skipped rows are why "we missed 47 plans" is
  answerable rather than merely absent. T19.

The table is therefore both the dedupe table and the catch-up ledger, which is why
it has no surrogate id: the natural key *is* the identity.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "013_triggers"
down_revision: str | None = "012_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "triggers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("actor_name", sa.Text(), nullable=False),
        sa.Column("cron", sa.Text(), nullable=False),
        sa.Column("timezone", sa.Text(), nullable=False, server_default="UTC"),
        sa.Column("input", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("catchup_policy", sa.Text(), nullable=False, server_default="skip"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("last_evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("organization_id", "key", name="uq_trigger_key"),
        sa.CheckConstraint("catchup_policy IN ('skip','all')", name="ck_trigger_catchup"),
    )
    op.create_index(
        "ix_triggers_active", "triggers", ["active"], postgresql_where=sa.text("active")
    )

    op.create_table(
        "trigger_fires",
        sa.Column("trigger_key", sa.Text(), primary_key=True),
        sa.Column(
            "trigger_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("triggers.id"),
            nullable=False,
        ),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "fired_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("skipped", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index("ix_trigger_fires_trigger", "trigger_fires", ["trigger_id", "scheduled_for"])


def downgrade() -> None:
    op.drop_index("ix_trigger_fires_trigger", table_name="trigger_fires")
    op.drop_table("trigger_fires")
    op.drop_index("ix_triggers_active", table_name="triggers")
    op.drop_table("triggers")
