"""019 kill switch — kill_switches

Revision ID: 019_killswitch
Revises: 018_approvals_v2

M0's kill switch was a dataclass on the gateway: a process-local boolean that an
operator could only set by restarting with different code. That is not a stop, it
is a deployment.

Two modes, and `drain` is the default for a reason stated in M2 §7: `halt` mid-effect
creates `INTENT` rows you then reconcile by hand. `halt` exists because sometimes
that is the correct trade — an effect you have to reconcile is better than an effect
that completes — but choosing it should be a deliberate act, so nothing defaults to
it.

`disengaged_at` rather than a DELETE. An incident review asks "when was it on", and
a row that was deleted answers nothing.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "019_killswitch"
down_revision: str | None = "018_approvals_v2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "kill_switches",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("scope_type", sa.Text(), nullable=False),
        # NULL scope_id means "every subject of this type". For scope_type='org'
        # it is always NULL.
        sa.Column("scope_id", sa.Text(), nullable=True),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("engaged_by", sa.Text(), nullable=False, server_default="operator"),
        sa.Column(
            "engaged_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("disengaged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("disengaged_by", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "scope_type IN ('org','tool','actor','connection')", name="ck_kill_scope_type"
        ),
        sa.CheckConstraint("mode IN ('halt','drain')", name="ck_kill_mode"),
        sa.CheckConstraint(
            "(scope_type = 'org' AND scope_id IS NULL) OR "
            "(scope_type <> 'org' AND scope_id IS NOT NULL)",
            name="ck_kill_scope_id",
        ),
    )
    # One live switch per scope. A second one for the same scope in a different mode
    # is an ambiguity nobody would resolve correctly under incident pressure.
    op.create_index(
        "ux_kill_live",
        "kill_switches",
        ["organization_id", "scope_type", "scope_id"],
        unique=True,
        postgresql_where=sa.text("disengaged_at IS NULL AND scope_id IS NOT NULL"),
    )
    op.create_index(
        "ux_kill_live_org",
        "kill_switches",
        ["organization_id", "scope_type"],
        unique=True,
        postgresql_where=sa.text("disengaged_at IS NULL AND scope_id IS NULL"),
    )
    op.create_index(
        "ix_kill_active",
        "kill_switches",
        ["organization_id"],
        postgresql_where=sa.text("disengaged_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_kill_active", table_name="kill_switches")
    op.drop_index("ux_kill_live_org", table_name="kill_switches")
    op.drop_index("ux_kill_live", table_name="kill_switches")
    op.drop_table("kill_switches")
