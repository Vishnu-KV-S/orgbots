"""036 kill switch, department scope — `ck_kill_scope_type` gains 'department'

Revision ID: 036_kill_department
Revises: 035_delegation

One value, one constraint, and three indexes that are already correct. The three
sentences below are here so that nobody "fixes" them later:

**`ck_kill_scope_id` needs nothing.** It is a biconditional — `org` implies a NULL
`scope_id` and anything else implies a non-NULL one — and a department switch names
a department, so it satisfies the non-org branch as written.

**`ux_kill_live` needs nothing.** It is keyed `(organization_id, scope_type, scope_id)`
under `disengaged_at IS NULL AND scope_id IS NOT NULL`, so department rows fall under
it and get the one-live-switch-per-scope guarantee for free. Its sibling
`ux_kill_live_org` covers the `scope_id IS NULL` case, which a department switch is
never in.

**`ck_kill_mode` is untouched.** `drain` and `halt` mean the same thing at department
scope as everywhere else; the scope says *who* is stopped, the mode says *how hard*.

The constraint is dropped and recreated rather than altered because Postgres has no
`ALTER CONSTRAINT` for a CHECK expression. The downgrade refuses to run while a
department switch is live rather than silently violating the narrower constraint it
is putting back.
"""

from __future__ import annotations

from alembic import op

revision: str = "036_kill_department"
down_revision: str | None = "035_delegation"
branch_labels = None
depends_on = None

_SCOPES_AFTER = "'org','tool','actor','connection','department'"
_SCOPES_BEFORE = "'org','tool','actor','connection'"


def upgrade() -> None:
    op.drop_constraint("ck_kill_scope_type", "kill_switches", type_="check")
    op.create_check_constraint(
        "ck_kill_scope_type", "kill_switches", f"scope_type IN ({_SCOPES_AFTER})"
    )


def downgrade() -> None:
    # A live department switch is a *stop somebody engaged*. Disengaging it here to
    # make the constraint fit would be a migration that quietly restarted a
    # department, so the downgrade fails loudly instead and says what to do.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM kill_switches WHERE scope_type = 'department') THEN
                RAISE EXCEPTION
                    'kill_switches still has department-scoped rows. Disengage them '
                    '(runtime.cli killswitch --disengage --scope department --scope-id NAME) '
                    'and delete the historical rows before downgrading past 036.';
            END IF;
        END $$;
        """
    )
    op.drop_constraint("ck_kill_scope_type", "kill_switches", type_="check")
    op.create_check_constraint(
        "ck_kill_scope_type", "kill_switches", f"scope_type IN ({_SCOPES_BEFORE})"
    )
