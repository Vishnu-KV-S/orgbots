"""052 members — the people of an organization, how they sign in, and whose bots are whose

Revision ID: 052_members
Revises: 051_bot_template_shares

Until now the runtime trusted whoever reached it: the browser named an organization in
a header and was that organization. With `RUNTIME_AUTH_MODE=members` it knows people
(`domain.members`), and this is what it keeps about them.

**`members`** — a person in an organization, with a role (`owner`, `admin`, `member`).
An email is unique per organization, compared without case. A member is never deleted,
only suspended: their name stays on what they said and did. `external_id` is the
identity provider's id, for SCIM.

**`member_sessions`** — a signed-in browser. Only a SHA-256 of the cookie's token is
stored, so a read of this table signs nobody in.

**`member_links`** — one-time sign-in links: an invitation (a new member, with the role
they will have) or a sign-in for an existing member, made by an admin or the CLI. Also
stored as a hash; used once, and expiring.

**`sso_configs`** — an organization's OpenID Connect identity provider: the issuer, the
client, the client secret sealed with the credential cipher, and the email domains that
sign in through it. **`oidc_states`** are sign-ins in flight: the `state`, the `nonce`
and the PKCE verifier between leaving for the provider and coming back.

**Bots get an owner and a visibility.** A member's bot is `private` to them until they
share it with the `team`. A bot that existed before members did has no owner and is
everyone's — backfilled to `team`, which is what it was. A helper gets its team's owner
and visibility when it is made (the repository's insert), and a change of visibility
is applied to the whole team, so a team bot's helpers are never private to someone.

`bot_groups.owner_member_id`, `push_subscriptions.member_id` and
`bot_notifications.member_id` say whose a group, a device and a notification are.
`vault_entries.profile` says which browser profile a saved login belongs to — a
member's own, or a team bot's (`domain.members.computer_profile`); `''` is the single
profile of a runtime without members.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "052_members"
down_revision: str | None = "051_bot_template_shares"
branch_labels = None
depends_on = None


def _uuid(name: str, *args: Any, **kw: Any) -> sa.Column[Any]:
    return sa.Column(name, postgresql.UUID(as_uuid=True), *args, **kw)


def _now(name: str) -> sa.Column[Any]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def upgrade() -> None:
    op.create_table(
        "members",
        _uuid("id", primary_key=True),
        _uuid("organization_id", sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("email", sa.Text, nullable=False),
        sa.Column("name", sa.Text, nullable=False, server_default=""),
        sa.Column("role", sa.Text, nullable=False, server_default="member"),
        sa.Column("status", sa.Text, nullable=False, server_default="active"),
        sa.Column("external_id", sa.Text, nullable=True),
        _now("created_at"),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("role IN ('owner','admin','member')", name="ck_member_role"),
        sa.CheckConstraint("status IN ('active','suspended')", name="ck_member_status"),
    )
    op.execute(
        "CREATE UNIQUE INDEX ux_members_org_email ON members (organization_id, lower(email))"
    )
    op.create_table(
        "member_sessions",
        _uuid("id", primary_key=True),
        _uuid("member_id", sa.ForeignKey("members.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_hash", sa.LargeBinary, nullable=False, unique=True),
        sa.Column("user_agent", sa.Text, nullable=False, server_default=""),
        _now("created_at"),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_member_sessions_member", "member_sessions", ["member_id"])
    op.create_table(
        "member_links",
        _uuid("id", primary_key=True),
        _uuid("organization_id", sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("kind", sa.Text, nullable=False),
        sa.Column("email", sa.Text, nullable=False),
        sa.Column("role", sa.Text, nullable=False, server_default="member"),
        _uuid("member_id", sa.ForeignKey("members.id"), nullable=True),
        _uuid("made_by", sa.ForeignKey("members.id"), nullable=True),
        sa.Column("token_hash", sa.LargeBinary, nullable=False, unique=True),
        _now("created_at"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind IN ('invite','sign_in')", name="ck_member_link_kind"),
        sa.CheckConstraint("role IN ('owner','admin','member')", name="ck_member_link_role"),
    )
    op.create_table(
        "sso_configs",
        _uuid("organization_id", sa.ForeignKey("organizations.id"), primary_key=True),
        sa.Column("issuer", sa.Text, nullable=False),
        sa.Column("client_id", sa.Text, nullable=False),
        sa.Column("secret_key_id", sa.Text, nullable=True),
        sa.Column("secret_nonce", sa.LargeBinary, nullable=True),
        sa.Column("secret_ciphertext", sa.LargeBinary, nullable=True),
        sa.Column("domains", postgresql.ARRAY(sa.Text), nullable=False, server_default="{}"),
        sa.Column("auto_join", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        _now("updated_at"),
    )
    op.create_table(
        "oidc_states",
        sa.Column("state", sa.Text, primary_key=True),
        _uuid("organization_id", sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("nonce", sa.Text, nullable=False),
        sa.Column("verifier", sa.Text, nullable=False),
        sa.Column("return_to", sa.Text, nullable=False, server_default="/"),
        _now("created_at"),
    )

    op.add_column("bots", _uuid("owner_member_id", sa.ForeignKey("members.id"), nullable=True))
    op.add_column(
        "bots", sa.Column("visibility", sa.Text, nullable=False, server_default="private")
    )
    op.execute("UPDATE bots SET visibility = 'team'")
    op.create_check_constraint("ck_bot_visibility", "bots", "visibility IN ('private','team')")
    op.add_column(
        "bot_groups", _uuid("owner_member_id", sa.ForeignKey("members.id"), nullable=True)
    )
    op.add_column(
        "push_subscriptions",
        _uuid("member_id", sa.ForeignKey("members.id", ondelete="CASCADE"), nullable=True),
    )
    op.add_column("bot_notifications", _uuid("member_id", nullable=True))
    op.add_column("vault_entries", sa.Column("profile", sa.Text, nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("vault_entries", "profile")
    op.drop_column("bot_notifications", "member_id")
    op.drop_column("push_subscriptions", "member_id")
    op.drop_column("bot_groups", "owner_member_id")
    op.drop_constraint("ck_bot_visibility", "bots")
    op.drop_column("bots", "visibility")
    op.drop_column("bots", "owner_member_id")
    op.drop_table("oidc_states")
    op.drop_table("sso_configs")
    op.drop_table("member_links")
    op.drop_table("member_sessions")
    op.drop_table("members")
