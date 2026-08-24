"""035 delegation — `runs.agent_path`, `actors.delegation`, the `delegations` table

Revision ID: 035_delegation
Revises: 034_departments

Three additions, and each is here rather than derived for a specific reason.

**`runs.agent_path`** is the cycle check's evidence (M5 §4, check 1). The check itself
runs against the value carried in the parent's frozen `RunSpec` — a run executes under
its spec, not under a query — but a column means A→B→A is answerable in SQL after the
fact, which is what turns "the guard held" from a claim into something an operator can
verify on a Tuesday. `text[]` rather than a delimited string so a department name
containing the delimiter is not a security bug.

**`actors.delegation`** is where M4's `DelegationDoc` finally lands. M4 validated it
and stored it in `spec_documents` and deliberately did **not** put it on `ActorSpec`,
for the same reason it kept `MemoryProfile` off: adding a field to `ActorSpec` changes
every `spec_hash` in the department and fails §8's round-trip. So the limits live on
the actor *row*, are read at admission alongside the active version, and reach the run
through `RunSpec.delegation` — exactly the route `memory_scopes` takes.

**`delegations`** is the spawn record, and it does three jobs one table can do and
three columns cannot:

*Idempotency.* `(parent_run_id, idempotency_key)` is unique, so a replayed node cannot
spawn a second child. This is the M5 equivalent of the duplicate-email bug (§9 risk 2)
and T67 is the test.

*Refusal evidence.* A delegation refused at check 1-6 never creates a run, so without
this table it would leave no trace at all — the same argument that made a refused run
a row in `LIMIT_REACHED` rather than an exception (edge case 21).

*Late attachment.* A child that finishes after its parent is already terminal has a
result and nowhere to put it. §2 says *persist, attach, event, never resume*, and
`result`/`attached_at`/`late` are where the first two of those happen. T59.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "035_delegation"
down_revision: str | None = "034_departments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column(
            "agent_path",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
    )
    # The descendant walk. `ix_runs_root` (002) finds a whole tree; this finds one
    # generation, which is what `max_children` counts and what the cascade recurses
    # over. Partial on the non-terminal statuses because every query that uses it is
    # asking about live work — a finished subtree is large and permanently uninteresting.
    op.create_index(
        "ix_runs_parent_live",
        "runs",
        ["parent_run_id"],
        postgresql_where=sa.text("status IN ('QUEUED','RUNNING','WAITING_CHILD')"),
    )

    op.add_column("actors", sa.Column("delegation", postgresql.JSONB(), nullable=True))

    op.create_table(
        "delegations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("root_run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "parent_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("runs.id"), nullable=False
        ),
        # Nullable, and that is the refusal case: checks 1-6 refuse *before* a run
        # exists, so a refused delegation is a row with no child.
        sa.Column(
            "child_run_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("runs.id"), nullable=True
        ),
        sa.Column("target_actor", sa.Text(), nullable=False),
        sa.Column("node", sa.Text(), nullable=False, server_default=""),
        sa.Column("ordinal", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("refusal_reason", sa.Text(), nullable=True),
        sa.Column("result", postgresql.JSONB(), nullable=True),
        sa.Column("late", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("attached_at", sa.DateTime(timezone=True), nullable=True),
        # The idempotency guard. Not `(parent, node, ordinal)`: the key is already a
        # deterministic function of those three plus the target, and keying on the
        # derived value means the uniqueness constraint and the derivation cannot
        # drift apart. T67.
        sa.UniqueConstraint("parent_run_id", "idempotency_key", name="uq_delegation_key"),
        sa.CheckConstraint(
            "status IN ('SPAWNED','COMPLETED','REFUSED','CANCELLED')", name="ck_delegation_status"
        ),
        sa.CheckConstraint(
            "(status = 'REFUSED') = (child_run_id IS NULL)", name="ck_delegation_refused_no_child"
        ),
    )
    op.create_index("ix_delegations_child", "delegations", ["child_run_id"])
    op.create_index("ix_delegations_root", "delegations", ["root_run_id"])


def downgrade() -> None:
    op.drop_index("ix_delegations_root", table_name="delegations")
    op.drop_index("ix_delegations_child", table_name="delegations")
    op.drop_table("delegations")
    op.drop_column("actors", "delegation")
    op.drop_index("ix_runs_parent_live", table_name="runs")
    op.drop_column("runs", "agent_path")
