"""007 lg schema — CREATE SCHEMA lg

Revision ID: 007_lg_schema
Revises: 006_budget

The LangGraph checkpointer owns everything inside `lg`; `PostgresSaver.setup()`
creates and migrates its own tables. This revision creates the namespace and
nothing else, so that a framework upgrade that changes checkpoint tables is not
also an Alembic conflict.

`downgrade` drops the schema with CASCADE, which discards checkpoints. That is
correct for a schema this migration created, and it is why T0 asserts
reversibility rather than data preservation across a full downgrade.
"""

from __future__ import annotations

from alembic import op

revision: str = "007_lg_schema"
down_revision: str | None = "006_budget"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS lg")


def downgrade() -> None:
    op.execute("DROP SCHEMA IF EXISTS lg CASCADE")
