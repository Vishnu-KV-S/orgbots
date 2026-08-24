"""004 effects — effect_intents, sideeffect_fixture, audit_log

Revision ID: 004_effects
Revises: 003_outbox

`audit_log` lands here rather than in its own revision: the tool gateway writes an
audit row in the same pipeline that writes the effect intent, so shipping the two
separately would leave a version of the schema where the gateway cannot run.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "004_effects"
down_revision: str | None = "003_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "effect_intents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("logical_call_id", sa.Text(), nullable=False),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("root_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("fence", sa.BigInteger(), nullable=False),
        sa.Column("node", sa.Text(), nullable=False),
        sa.Column("checkpoint_ns", sa.Text(), nullable=False, server_default=""),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("args_hash", sa.Text(), nullable=False),
        sa.Column("tool_name", sa.Text(), nullable=False),
        sa.Column("tool_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("recovery_policy", sa.Text(), nullable=False),
        sa.Column("blast_radius", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=True),
        sa.Column("marker", sa.Text(), nullable=True),
        sa.Column("provider_ref", sa.Text(), nullable=True),
        sa.Column("result_ref", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("result_inline", postgresql.JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        # The exactly-once mechanism. Everything else in effects/ is policy about
        # what to do when this constraint says "already here".
        sa.UniqueConstraint("logical_call_id", name="uq_effect_logical_call"),
        sa.CheckConstraint(
            "status IN ('INTENT','COMMITTED','FAILED','ORPHANED','COMPENSATED')",
            name="ck_effect_status",
        ),
    )
    op.create_index("ix_effects_run", "effect_intents", ["run_id"])
    op.create_index(
        "ix_effects_open",
        "effect_intents",
        ["status"],
        postgresql_where=sa.text("status = 'INTENT'"),
    )

    op.create_table(
        "sideeffect_fixture",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("marker", sa.Text(), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("logical_call_id", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        # Unique so a double-fire surfaces as an integrity error rather than a
        # second row a sloppy assertion might miss.
        sa.UniqueConstraint("marker", name="uq_sideeffect_marker"),
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("root_run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("fence", sa.BigInteger(), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target", sa.Text(), nullable=True),
        sa.Column("severity", sa.Text(), nullable=False, server_default="normal"),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("detail", postgresql.JSONB(), nullable=False),
        sa.Column("trace_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_audit_run", "audit_log", ["run_id", "id"])


def downgrade() -> None:
    op.drop_index("ix_audit_run", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_table("sideeffect_fixture")
    op.drop_index("ix_effects_open", table_name="effect_intents")
    op.drop_index("ix_effects_run", table_name="effect_intents")
    op.drop_table("effect_intents")
