"""046 bot rule deny — "never allow" beside "ask first" and "allow"

Revision ID: 046_bot_rule_deny
Revises: 045_team_file_blobs

A bot's rules said "ask first" or "allow automatically". Running commands on the
person's own machine needs the third answer GrokBot gives — never — and it is useful
for any action: never submit on a site, never type on another. A `deny` rule refuses
the step outright, before any other rule is consulted (`domain.bots.needs_approval`),
and the bot is told it is not allowed rather than asked to wait for anyone.
"""

from __future__ import annotations

from alembic import op

revision: str = "046_bot_rule_deny"
down_revision: str | None = "045_team_file_blobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_bot_rule_decision", "bot_rules", type_="check")
    op.create_check_constraint(
        "ck_bot_rule_decision", "bot_rules", "decision IN ('ask','allow','deny')"
    )


def downgrade() -> None:
    op.execute("DELETE FROM bot_rules WHERE decision = 'deny'")
    op.drop_constraint("ck_bot_rule_decision", "bot_rules", type_="check")
    op.create_check_constraint("ck_bot_rule_decision", "bot_rules", "decision IN ('ask','allow')")
