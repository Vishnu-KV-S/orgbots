"""030 audit embedding — let `audit_logs.gateway` name the embedding gateway

Revision ID: 030_audit_embedding
Revises: 029_context

**Not in M3 §4's migration list, and it is here because a test found it.**
`test_m3_isolation.py` failed on the first run with:

    new row for relation "audit_logs_202608" violates check constraint "ck_audit_gateway"

M2's §2 decision audit is a closed vocabulary — `gateway IN ('tool','model','run',
'approval')` — and closed was the right call: it is what stops a typo'd gateway name
becoming a silent hole in the denial stream. M3 adds a fourth gateway, and the
constraint does exactly what it was built to do.

So the correct response is to widen the vocabulary *deliberately*, in a migration, and
not to quietly stop writing an audit row for embeddings — which is the shortcut that
would have made §13 risk 4 ("consolidation cost is invisible until you look") come true
in the most literal possible way. Embedding calls are the highest-volume external calls
M3 makes; they are the last ones that should be missing from the audit table.

`ALTER TABLE ... DROP CONSTRAINT` then `ADD` on a partitioned parent cascades to every
partition, so this is one statement per direction and no partition-by-partition loop.
The `NOT VALID` / `VALIDATE` dance is unnecessary here because the new set is a strict
superset of the old one: no existing row can violate it.
"""

from __future__ import annotations

from alembic import op

revision: str = "030_audit_embedding"
down_revision: str | None = "029_context"
branch_labels = None
depends_on = None

OLD = "gateway IN ('tool','model','run','approval')"
NEW = "gateway IN ('tool','model','run','approval','embedding')"


def upgrade() -> None:
    op.execute("ALTER TABLE audit_logs DROP CONSTRAINT ck_audit_gateway")
    op.execute(f"ALTER TABLE audit_logs ADD CONSTRAINT ck_audit_gateway CHECK ({NEW})")


def downgrade() -> None:
    # Rows written by the embedding gateway would violate the narrower constraint, so
    # they are deleted first. That is destructive and it is the honest behaviour for a
    # downgrade past the migration that made them representable — the alternative is an
    # `ADD CONSTRAINT` that fails on a database that has been running M3, which is a
    # downgrade that does not work rather than one that costs something.
    op.execute("DELETE FROM audit_logs WHERE gateway = 'embedding'")
    op.execute("ALTER TABLE audit_logs DROP CONSTRAINT ck_audit_gateway")
    op.execute(f"ALTER TABLE audit_logs ADD CONSTRAINT ck_audit_gateway CHECK ({OLD})")
