"""016 authority — roles, authority_policies, tool_grants, connections

Revision ID: 016_authority
Revises: 015_metrics

M1's authority model was a frozen dict with one entry. This is the table set that
replaces it, and the shape is chosen so that the two compile-time checks in M2 §3
are cheap.

**Roles are a tree, and the tree is the escalation order.** `parent_role_id` is
what "strictly up-hierarchy" means: an approver is legal for a requester exactly
when it is a proper ancestor. That turns v3 edge case 30 — A approves for B and B
approves for A — from a runtime deadlock into a graph walk at spec-apply time,
which is the whole reason the hierarchy is a column rather than a convention.

`rank` is *not* the hierarchy. It is a human-readable seniority number for
dashboards and error messages; the parent pointer is authoritative. Two columns
that could disagree is a smell, so the acyclicity check asserts they agree
(`parent.rank < child.rank`) rather than trusting either alone.

**Policies overlay, they do not replace.** `scope_type` orders the resolution
chain: `role` is the base, `department` overlays it, `actor` overlays that. The
resolver applies them in `_SCOPE_PRECEDENCE` order and the blast-radius floor
applies last, so a tool may tighten what a policy allows and never loosen it.

**Grants are revocable and the runtime notices.** `revoked_at` rather than a
DELETE, because "who could do what last Tuesday" is a question the denial stream
review in §9 will actually ask. The gateway re-reads grants through a ≤30s cache
(edge case 64), so revocation takes effect inside a run without mutating its frozen
spec.

`actors.role_name` and `actors.department` land here rather than in 001 because
until there is a resolver to read them they would be two columns nothing consults.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "016_authority"
down_revision: str | None = "015_metrics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "roles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("department", sa.Text(), nullable=True),
        # Self-referential: the escalation order. NULL parent is the root of the
        # tree, which is the only place a buck can stop.
        sa.Column("parent_role_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("rank", sa.SmallInteger(), nullable=False, server_default="100"),
        # Who actually answers when this role is named as an approver. NULL means
        # "this role requests approvals but never grants them".
        sa.Column("approver", sa.Text(), nullable=True),
        # Daily cap on approvals routed to this role's approver. Exceeding it fires
        # an alert and defers rather than lengthening the queue — M2 §5, edge 31.
        sa.Column("approver_daily_budget", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("base_authority", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("organization_id", "name", name="uq_role_name"),
        sa.CheckConstraint("approver_daily_budget >= 0", name="ck_role_budget_sign"),
    )
    op.create_foreign_key(
        "fk_role_parent", "roles", "roles", ["parent_role_id"], ["id"], ondelete="SET NULL"
    )
    op.create_index("ix_roles_org", "roles", ["organization_id"])

    op.create_table(
        "authority_policies",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("scope_type", sa.Text(), nullable=False),
        sa.Column("scope_id", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("level", sa.Text(), nullable=False),
        sa.Column("approver_role", sa.Text(), nullable=True),
        sa.Column("max_escalations", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("on_expiry", sa.Text(), nullable=False, server_default="deny"),
        sa.Column("ttl_seconds", sa.Integer(), nullable=False, server_default="86400"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "organization_id", "scope_type", "scope_id", "action", name="uq_authority_scope"
        ),
        sa.CheckConstraint(
            "scope_type IN ('role','department','actor')", name="ck_authority_scope_type"
        ),
        sa.CheckConstraint("level IN ('auto','human','denied')", name="ck_authority_level"),
        sa.CheckConstraint(
            "on_expiry IN ('deny','grant','escalate','fail_run')", name="ck_authority_on_expiry"
        ),
        sa.CheckConstraint("ttl_seconds > 0", name="ck_authority_ttl"),
        sa.CheckConstraint("max_escalations >= 0", name="ck_authority_escalations"),
    )
    op.create_index("ix_authority_org", "authority_policies", ["organization_id", "action"])

    op.create_table(
        "connections",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("scopes", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("status", sa.Text(), nullable=False, server_default="ACTIVE"),
        sa.Column("credential_name", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("organization_id", "name", name="uq_connection_name"),
        sa.CheckConstraint("status IN ('ACTIVE','DISABLED')", name="ck_connection_status"),
    )

    op.create_table(
        "tool_grants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("subject_type", sa.Text(), nullable=False),
        sa.Column("subject_id", sa.Text(), nullable=False),
        sa.Column("tool", sa.Text(), nullable=False),
        sa.Column(
            "connection_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("connections.id"),
            nullable=True,
        ),
        sa.Column("granted_by", sa.Text(), nullable=False, server_default="system"),
        sa.Column(
            "granted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        # Revoked, not deleted: "who could do what last Tuesday" is a question the
        # §9 denial review asks, and a DELETE makes it unanswerable.
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_by", sa.Text(), nullable=True),
        sa.UniqueConstraint(
            "organization_id", "subject_type", "subject_id", "tool", name="uq_tool_grant"
        ),
        sa.CheckConstraint("subject_type IN ('role','actor')", name="ck_grant_subject_type"),
    )
    op.create_index(
        "ix_tool_grants_live",
        "tool_grants",
        ["organization_id", "subject_type", "subject_id"],
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    op.add_column("actors", sa.Column("role_name", sa.Text(), nullable=True))
    op.add_column("actors", sa.Column("department", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("actors", "department")
    op.drop_column("actors", "role_name")
    op.drop_index("ix_tool_grants_live", table_name="tool_grants")
    op.drop_table("tool_grants")
    op.drop_table("connections")
    op.drop_index("ix_authority_org", table_name="authority_policies")
    op.drop_table("authority_policies")
    op.drop_index("ix_roles_org", table_name="roles")
    op.drop_constraint("fk_role_parent", "roles", type_="foreignkey")
    op.drop_table("roles")
