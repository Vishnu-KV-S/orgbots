"""022 credentials — encrypted credential storage and rotation metadata

Revision ID: 022_credentials
Revises: 021_ratelimits

Credentials move out of the environment. The reason is not that env vars are
insecure — they are about as secure as this table is — but that they cannot be
*rotated* while a process is running, cannot be scoped per connection, and leave no
record of who read them when. All three of those are M2 requirements and none of
them is achievable without a row.

**Versions are rows, not updates.** A rotation inserts version *n+1* as ACTIVE and
marks *n* RETIRED. Nothing is overwritten, so a call in flight against the old
version is diagnosable rather than mysterious, and rolling back a bad rotation is an
UPDATE of two status columns. `uq_credential_active` — a partial unique index —
makes "two active versions of the same credential" unrepresentable, which is the
failure mode where half the fleet uses each.

**The ciphertext is AES-256-GCM and the key is not in here.** `key_id` names which
key encrypted this row so a key rotation is separable from a credential rotation;
the key material comes from the process environment. That is the boundary this table
is honest about: an attacker with the database has ciphertext, an attacker with the
database *and* the process environment has secrets. Moving the key into a KMS is the
next step and it changes only `CredentialCipher`.

`last_fetched_at` is deliberately not a hot-path write. The gateway fetches per call
(no pinning — that is what makes T37 pass) and touching a counter on every fetch
would add a write to the hottest read in the pipeline. It is stamped by rotation and
by the operator CLI, which is where "is this credential still in use" gets asked.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "022_credentials"
down_revision: str | None = "021_ratelimits"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "credentials",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.Text(), nullable=False, server_default="ACTIVE"),
        sa.Column("key_id", sa.Text(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        # Enough to identify a key in a log line without being enough to use it.
        sa.Column("fingerprint", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rotated_by", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("organization_id", "name", "version", name="uq_credential_version"),
        sa.CheckConstraint("status IN ('ACTIVE','RETIRED')", name="ck_credential_status"),
        sa.CheckConstraint("version > 0", name="ck_credential_version"),
    )
    op.create_index(
        "uq_credential_active",
        "credentials",
        ["organization_id", "name"],
        unique=True,
        postgresql_where=sa.text("status = 'ACTIVE'"),
    )


def downgrade() -> None:
    op.drop_index("uq_credential_active", table_name="credentials")
    op.drop_table("credentials")
