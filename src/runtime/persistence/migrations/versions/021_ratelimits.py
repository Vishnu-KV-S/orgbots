"""021 rate limits — rate_limit_policies

Revision ID: 021_ratelimits
Revises: 020_audit

Three scopes, because they fail differently and a single limit cannot express all
three:

- **connection** — the provider's actual limit for this credential. Exceeding it
  gets the key throttled or banned, and the blast radius is every run that shares it.
- **provider** — the aggregate across connections. Two connections each inside their
  own limit can still put a provider-wide quota over.
- **actor** — a runaway-loop guard that is independent of budget. A cheap tool called
  ten thousand times costs almost nothing and is still an incident.

The policy lives here; the counter lives in Redis. That split is deliberate and it
respects I10: a `FLUSHALL` loses the current window's consumption, which means at
worst one window runs permissive. Nothing that cannot be rebuilt from Postgres is
stored there — the *policy* is the durable half.

`fail_open` is per policy rather than global. For an actor loop guard, failing open
during a Redis outage is right (the budget still bounds it). For a connection whose
provider bans on abuse, it is not, and that policy sets `fail_open = false` and
accepts that a Redis outage stops those calls.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "021_ratelimits"
down_revision: str | None = "020_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rate_limit_policies",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("scope_type", sa.Text(), nullable=False),
        sa.Column("scope_id", sa.Text(), nullable=False),
        sa.Column("limit_per_window", sa.Integer(), nullable=False),
        sa.Column("window_seconds", sa.Integer(), nullable=False, server_default="60"),
        # Burst is the bucket's capacity; limit_per_window is its refill rate. A
        # burst below the rate would make the bucket unable to hold one window's
        # worth of tokens, which is a limit that silently under-delivers.
        sa.Column("burst", sa.Integer(), nullable=False),
        sa.Column("fail_open", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "organization_id", "scope_type", "scope_id", name="uq_rate_limit_scope"
        ),
        sa.CheckConstraint(
            "scope_type IN ('connection','provider','actor')", name="ck_rate_scope_type"
        ),
        sa.CheckConstraint("limit_per_window > 0 AND window_seconds > 0", name="ck_rate_positive"),
        sa.CheckConstraint("burst >= limit_per_window", name="ck_rate_burst"),
    )


def downgrade() -> None:
    op.drop_table("rate_limit_policies")
