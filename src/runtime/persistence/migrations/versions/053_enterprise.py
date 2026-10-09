"""053 enterprise — policies, team secrets, the audit trail, SCIM and telemetry export

Revision ID: 053_enterprise
Revises: 052_members

**`org_policies`** — what an organization's admins decided for everyone
(`domain.policies`): the network its bots' browsers and commands may reach (`open`, or
an `allowlist` of hosts), whether Auto Review is required, whether members may publish
template links, and whether members may connect apps. One row per organization; none
means the defaults, which are today's behaviour.

**`team_secrets`** — values a bot's sandboxed commands read as environment variables (an
API token for a CLI, say). Sealed with the credential cipher; the value is never
returned once saved, and never reaches a prompt.

**`org_audit_events`** — the control plane's trail: who invited whom, changed a role,
set up single sign-on, changed a policy or a secret, shared or deleted a bot, made a
template link, connected an app. `actor_member_id` is null for the runtime itself, the
CLI, or SCIM. `exported_at` is the OpenTelemetry exporter's mark.

**`scim_tokens`** — the bearer token an identity provider provisions users with. Only a
SHA-256 is stored; the token is shown once.

**`otel_configs`** — where an organization's events are exported (an OTLP/HTTP
endpoint), its headers sealed like a secret, and whether to include members' emails.
**`otel_cursors`** — how far the exporter has read the gateway's decision log
(`audit_logs`) for an organization, so every tool call is exported once.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "053_enterprise"
down_revision: str | None = "052_members"
branch_labels = None
depends_on = None


def _org(**kw: Any) -> sa.Column[Any]:
    return sa.Column(
        "organization_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("organizations.id"),
        nullable=False,
        **kw,
    )


def _now(name: str) -> sa.Column[Any]:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def upgrade() -> None:
    op.create_table(
        "org_policies",
        _org(primary_key=True),
        sa.Column("network", sa.Text, nullable=False, server_default="open"),
        sa.Column("allowed_hosts", postgresql.ARRAY(sa.Text), nullable=False, server_default="{}"),
        sa.Column("require_review", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("template_links", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("members_add_apps", sa.Boolean, nullable=False, server_default=sa.false()),
        _now("updated_at"),
        sa.CheckConstraint("network IN ('open','allowlist')", name="ck_policy_network"),
    )
    op.create_table(
        "team_secrets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        _org(),
        sa.Column("name", sa.Text, nullable=False),
        sa.Column("key_id", sa.Text, nullable=False),
        sa.Column("nonce", sa.LargeBinary, nullable=False),
        sa.Column("ciphertext", sa.LargeBinary, nullable=False),
        sa.Column("bytes", sa.Integer, nullable=False),
        sa.Column("updated_by", postgresql.UUID(as_uuid=True), nullable=True),
        _now("created_at"),
        _now("updated_at"),
        sa.UniqueConstraint("organization_id", "name", name="uq_team_secret_name"),
    )
    op.create_table(
        "org_audit_events",
        sa.Column("id", sa.BigInteger, sa.Identity(), primary_key=True),
        _org(),
        _now("occurred_at"),
        sa.Column("actor_member_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor", sa.Text, nullable=False, server_default=""),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("target", sa.Text, nullable=False, server_default=""),
        sa.Column("detail", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("exported_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_org_audit_events_org_time", "org_audit_events", ["organization_id", "occurred_at"]
    )
    op.execute(
        "CREATE INDEX ix_org_audit_events_unexported ON org_audit_events (id) "
        "WHERE exported_at IS NULL"
    )
    op.create_table(
        "scim_tokens",
        _org(primary_key=True),
        sa.Column("token_hash", sa.LargeBinary, nullable=False, unique=True),
        sa.Column("hint", sa.Text, nullable=False, server_default=""),
        _now("created_at"),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "otel_configs",
        _org(primary_key=True),
        sa.Column("endpoint", sa.Text, nullable=False),
        sa.Column("headers_key_id", sa.Text, nullable=True),
        sa.Column("headers_nonce", sa.LargeBinary, nullable=True),
        sa.Column("headers_ciphertext", sa.LargeBinary, nullable=True),
        sa.Column("include_email", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("include_actions", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("last_error", sa.Text, nullable=False, server_default=""),
        sa.Column("last_sent_at", sa.DateTime(timezone=True), nullable=True),
        _now("updated_at"),
    )
    op.create_table(
        "otel_cursors",
        _org(primary_key=True),
        sa.Column("audit_log_id", sa.BigInteger, nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_table("otel_cursors")
    op.drop_table("otel_configs")
    op.drop_table("scim_tokens")
    op.drop_table("org_audit_events")
    op.drop_table("team_secrets")
    op.drop_table("org_policies")
