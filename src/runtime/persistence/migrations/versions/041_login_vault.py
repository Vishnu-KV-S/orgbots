"""041 login vault — sign-in details a person gives a bot without the bot seeing them

Revision ID: 041_login_vault
Revises: 040_bot_memory_brief

Until now a bot signed in by *typing* a password, which meant the password had been
in its prompt — in the conversation the person typed it into, and again in the
approval row the type action waited in. Both are gone with this.

**`vault_entries`** holds what a person entered, AES-256-GCM encrypted under the same
keys as `credentials` (022) and bound by AAD to its organization, its row and its
site, so a ciphertext moved to another row or another host does not open. Two kinds:

- `saved` — a login kept for next time: who you are and the password, never a code.
  `auto_use` is the person's "fill this in without asking me".
- `once` — what a person typed for one form and did not save, a code included.
  `expires_at` is ten minutes out: long enough for a two-page sign-in, short enough
  that "do not save" means it. Scoped to the bot it was given to.

`kinds` is the plaintext list of what an entry can answer (`email`, `password`, …),
so a run decides whether the vault can fill a form without decrypting anything. It
names no values. `label` is a hint like `a••@example.com`, the same idea as a
credential's fingerprint.

**`bot_credential_requests`** is the card in the chat: the fields the runtime read
off the page, the host, and what it ended as. It holds no value at any point — a
submission writes a vault entry and records its id here.

The message role `credentials` is that card in the transcript.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "041_login_vault"
down_revision: str | None = "040_bot_memory_brief"
branch_labels = None
depends_on = None

_ROLES_BEFORE = "role IN ('user','bot','activity','approval','system','error')"
_ROLES_AFTER = "role IN ('user','bot','activity','approval','system','error','credentials')"


def upgrade() -> None:
    op.create_table(
        "vault_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("host", sa.Text, nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("bot_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("bots.id"), nullable=True),
        sa.Column("label", sa.Text, nullable=False, server_default=""),
        sa.Column(
            "kinds",
            postgresql.ARRAY(sa.Text),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column("key_id", sa.Text, nullable=False),
        sa.Column("nonce", sa.LargeBinary, nullable=False),
        sa.Column("ciphertext", sa.LargeBinary, nullable=False),
        sa.Column("auto_use", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("use_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("kind IN ('saved','once')", name="ck_vault_entry_kind"),
        sa.CheckConstraint(
            "kind = 'saved' OR (expires_at IS NOT NULL AND bot_id IS NOT NULL)",
            name="ck_vault_once_is_scoped",
        ),
    )
    op.create_index("ix_vault_entries_org_host", "vault_entries", ["organization_id", "host"])

    op.create_table(
        "bot_credential_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "bot_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("bots.id"), nullable=False
        ),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("host", sa.Text, nullable=False),
        sa.Column("page_url", sa.Text, nullable=False, server_default=""),
        sa.Column("purpose", sa.Text, nullable=False),
        sa.Column(
            "fields", postgresql.JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("status", sa.Text, nullable=False, server_default="pending"),
        sa.Column(
            "entry_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=False,
            server_default=sa.text("'{}'::uuid[]"),
        ),
        sa.Column("saved", sa.Boolean, nullable=False, server_default=sa.false()),
        # The turn's plan and notes, so the run that fills the form picks the task up
        # where the run that asked left it. Never a value: those are in the vault.
        sa.Column(
            "working", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "purpose IN ('sign_in','sign_up','verify')", name="ck_bot_creds_purpose"
        ),
        sa.CheckConstraint(
            "status IN ('pending','submitted','cancelled','expired')", name="ck_bot_creds_status"
        ),
    )
    op.create_index(
        "ix_bot_creds_live",
        "bot_credential_requests",
        ["bot_id"],
        postgresql_where=sa.text("status = 'pending'"),
    )

    op.drop_constraint("ck_bot_message_role", "bot_messages", type_="check")
    op.create_check_constraint("ck_bot_message_role", "bot_messages", _ROLES_AFTER)


def downgrade() -> None:
    op.execute("DELETE FROM bot_messages WHERE role = 'credentials'")
    op.drop_constraint("ck_bot_message_role", "bot_messages", type_="check")
    op.create_check_constraint("ck_bot_message_role", "bot_messages", _ROLES_BEFORE)
    op.drop_index("ix_bot_creds_live", table_name="bot_credential_requests")
    op.drop_table("bot_credential_requests")
    op.drop_index("ix_vault_entries_org_host", table_name="vault_entries")
    op.drop_table("vault_entries")
