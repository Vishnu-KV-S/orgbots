"""031 spec docs — the source YAML, kept beside the compiled artifact

Revision ID: 031_spec_docs
Revises: 030_audit_embedding

M4 §9. One table, and the only interesting thing about it is that it stores the
*source* as well as the compiled form.

**Why keep the YAML.** `actor_versions.spec` already holds the compiled artifact and
`spec_hash` already identifies it, so the compiled column here is a convenience. The
`source_yaml` column is not. Six weeks after an apply, "what did the operator actually
write" and "what did the compiler make of it" are two different questions, and a
control plane that can only answer the second one turns every compiler-behaviour
argument into an archaeology exercise against a git history that may not exist — the
file may never have been committed, and `--rename` means the path can move.

**`source_ref` is nullable on purpose.** It is the git sha of the file when there is
one. Requiring it would make `apply` refuse to run from a working tree, which is
exactly where the first apply of any change happens.

**The unique key includes `spec_hash`.** So re-applying an unchanged document is a
no-op rather than a second row, and *changing* a document appends rather than
overwrites: the history of one named document is a select on (kind, name) ordered by
`applied_at`. Versions are never deleted (v3 §14) and this table follows the same
rule as `actor_versions`.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "031_spec_docs"
down_revision: str | None = "030_audit_embedding"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "spec_documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("source_yaml", sa.Text(), nullable=False),
        sa.Column("compiled_spec", postgresql.JSONB(), nullable=False),
        sa.Column("spec_hash", sa.Text(), nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=True),
        sa.Column(
            "applied_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("applied_by", sa.Text(), nullable=False, server_default="operator"),
        sa.UniqueConstraint(
            "organization_id", "kind", "name", "spec_hash", name="uq_spec_document"
        ),
    )
    # The drift query and `spec show` both ask "the latest document of this kind and
    # name", which is this index plus a LIMIT 1.
    op.create_index(
        "ix_spec_documents_latest",
        "spec_documents",
        ["organization_id", "kind", "name", "applied_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_spec_documents_latest", table_name="spec_documents")
    op.drop_table("spec_documents")
