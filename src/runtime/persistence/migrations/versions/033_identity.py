"""033 identity — `actors.uid`, `actors.reports_to`, `actors.active`

Revision ID: 033_identity
Revises: 032_apply_history

Three columns, and each answers one of M4's edge cases.

**`uid`** is §4 option (a), added now and set by nobody. M4 chose option (b) — a
rename is refused at plan time unless `--rename old=new` is passed — precisely
because (a) *"adds a field nobody sets until they need it, and by then it's too
late"*. The column exists so that a future decision to start setting it is a change
to the compiler rather than a migration under a live org, and it is nullable and
unique-when-present so that the two schemes can coexist.

**`reports_to`** is the reporting edge M4 §3 puts in the document format. It is a
*name*, not an id, for the same reason `metadata.name` is the identity key: the
compiler resolves the whole reporting graph in memory and checks it for cycles before
anything is written, so a foreign key here would buy referential integrity we already
have and cost us the two-phase apply's ability to write actors in any order.

**`active`** is edge case 78. An actor removed from the YAML is deactivated, never
deleted: a hard delete breaks a resumed run's `resolve_active`, and versions are never
deleted (v3 §14). Defaulting to true means every existing row is active, which is what
it was before this migration existed.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "033_identity"
down_revision: str | None = "032_apply_history"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("actors", sa.Column("uid", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column("actors", sa.Column("reports_to", sa.Text(), nullable=True))
    op.add_column(
        "actors", sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true())
    )
    # Unique *where present*: two actors may both have no uid, which is the state
    # every row is in today and the state M4 leaves them in.
    op.create_index(
        "uq_actor_uid",
        "actors",
        ["organization_id", "uid"],
        unique=True,
        postgresql_where=sa.text("uid IS NOT NULL"),
    )
    # An actor may not report to itself. The full acyclicity check is a graph walk in
    # the compiler; this is the one case a CHECK can express, and it is the one a
    # hand-written UPDATE is most likely to produce.
    op.create_check_constraint(
        "ck_actor_reports_to_self", "actors", "reports_to IS NULL OR reports_to <> name"
    )


def downgrade() -> None:
    op.drop_constraint("ck_actor_reports_to_self", "actors", type_="check")
    op.drop_index("uq_actor_uid", table_name="actors")
    op.drop_column("actors", "active")
    op.drop_column("actors", "reports_to")
    op.drop_column("actors", "uid")
