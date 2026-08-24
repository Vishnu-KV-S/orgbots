"""029 context — context_traces and the grading views

Revision ID: 029_context
Revises: 028_promotion

§4: *"`context_traces` is the shadow-mode mechanism and, later, the evidence behind
every eval. **Write it before you write retrieval.**"*

That instruction is an ordering claim and it is right. Retrieval that logs nothing is
retrieval you cannot grade, and §5's whole argument is that grading has to come first:
*"Injecting first and measuring later means quality and cost move together and you can
attribute neither."* So this migration lands before `ContextPlanner` exists, and PR-29
adds a planner that writes here from its first line.

**Every retrieval writes a row, shadow or live** (T54). The `shadow_mode` boolean is
what separates the two, and it is on the row rather than inferred from
`injected_count = 0` — because "we retrieved nothing" and "we retrieved and chose not
to inject" are different events and the second one is the interesting one.

**`retrieved` holds the rejected candidates too.** A trace with only the winners can
answer "was what we injected any good" (eval 2) but not "was the right memory available
and passed over" (eval 1 failing while eval 2 looks fine). Those two failures want
opposite responses — one is a rerank problem, the other is a store problem — so the
trace carries enough to tell them apart.

**The grading columns live on the trace, not in a side table.** §5's harness grades a
sample by hand: *would this memory have helped, been neutral, or been harmful in this
specific call?* Putting the verdict next to the evidence means a grade cannot outlive
the retrieval it describes, and a re-grade after a rerank change is an UPDATE rather
than a reconciliation.

`would_have_injected_tokens` is eval 8's numerator during shadow mode: *"Measure it in
shadow mode (compute the tokens you would have injected), then set a budget."* It is
computed and stored even when nothing is injected, which is the only way that sentence
is executable.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "029_context"
down_revision: str | None = "028_promotion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "context_traces",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id"),
            nullable=False,
        ),
        sa.Column("run_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("actor_name", sa.Text(), nullable=False),
        sa.Column("node", sa.Text(), nullable=False),
        sa.Column("call_site", sa.Text(), nullable=True),
        sa.Column("query_text", sa.Text(), nullable=True),
        sa.Column(
            "retrieved", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("retrieved_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("injected_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("injected_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("would_have_injected_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "scopes", postgresql.ARRAY(sa.Text()), nullable=False, server_default=sa.text("'{}'")
        ),
        sa.Column("embedding_version", sa.Text(), nullable=True),
        sa.Column("latency_ms", sa.Float(), nullable=True),
        sa.Column("shadow_mode", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        # --- grading (§5, and evals 1-3) ---
        sa.Column("grade", sa.Text(), nullable=False, server_default="ungraded"),
        sa.Column("graded_by", sa.Text(), nullable=True),
        sa.Column("graded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("grade_note", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "grade IN ('helpful','neutral','harmful','ungraded')", name="ck_trace_grade"
        ),
        sa.CheckConstraint(
            "grade = 'ungraded' OR graded_by IS NOT NULL", name="ck_trace_graded_by"
        ),
        # Shadow mode injects nothing. Not an assertion about intent — an assertion
        # about the prompt: §5's guarantee is "zero risk to output quality, zero token
        # cost", and a shadow trace with a non-zero injected count means that
        # guarantee was broken somewhere and nobody would otherwise notice.
        sa.CheckConstraint(
            "NOT shadow_mode OR (injected_count = 0 AND injected_tokens = 0)",
            name="ck_trace_shadow_injects_nothing",
        ),
        sa.CheckConstraint("injected_count <= retrieved_count", name="ck_trace_injected_subset"),
    )
    op.create_index("ix_trace_run", "context_traces", ["run_id"])
    op.create_index("ix_trace_org_time", "context_traces", ["organization_id", "created_at"])
    # The grading harness's work queue: ungraded traces that actually retrieved
    # something, newest first. Partial, because a graded trace is never in the queue
    # again and the queue is read far more often than it is written to.
    op.create_index(
        "ix_trace_ungraded",
        "context_traces",
        ["organization_id", "created_at"],
        postgresql_where=sa.text("grade = 'ungraded' AND retrieved_count > 0"),
    )

    # --- the eval views ---------------------------------------------------------
    # Plain views, not materialized, for the reason ARCHITECTURE.md §7 already gives
    # for the M1 metric views: a refresh job can be stale on the morning somebody
    # reads the dashboard and makes a decision.

    op.execute(
        """
        CREATE VIEW v_memory_grades AS
        SELECT organization_id,
               date_trunc('week', created_at)::date          AS week,
               shadow_mode,
               count(*)                                       AS graded,
               count(*) FILTER (WHERE grade = 'helpful')       AS helpful,
               count(*) FILTER (WHERE grade = 'neutral')       AS neutral,
               count(*) FILTER (WHERE grade = 'harmful')       AS harmful,
               -- §5's bar is stated over helpful-or-neutral together, so the view
               -- computes that combination rather than leaving every reader to add
               -- two columns and one of them to forget.
               round(
                   (count(*) FILTER (WHERE grade IN ('helpful','neutral')))::numeric
                   / nullif(count(*), 0) * 100, 1
               )                                              AS helpful_or_neutral_pct,
               round(
                   (count(*) FILTER (WHERE grade = 'harmful'))::numeric
                   / nullif(count(*), 0) * 100, 1
               )                                              AS harmful_pct
          FROM context_traces
         WHERE grade <> 'ungraded'
         GROUP BY organization_id, week, shadow_mode
        """
    )

    op.execute(
        """
        CREATE VIEW v_memory_token_effect AS
        SELECT organization_id,
               date_trunc('week', created_at)::date           AS week,
               shadow_mode,
               count(*)                                        AS retrievals,
               -- Eval 8. In shadow mode the cost is hypothetical and lives in
               -- `would_have_injected_tokens`; live, it is real and lives in
               -- `injected_tokens`. `coalesce` over the two would hide which regime
               -- produced the number, so both are reported and `shadow_mode` groups.
               round(avg(injected_tokens)::numeric, 1)         AS avg_injected_tokens,
               round(avg(would_have_injected_tokens)::numeric, 1) AS avg_would_have_tokens,
               max(injected_tokens)                            AS max_injected_tokens,
               round(avg(retrieved_count)::numeric, 2)         AS avg_retrieved,
               round(avg(injected_count)::numeric, 2)          AS avg_injected,
               round(avg(latency_ms)::numeric, 1)              AS avg_latency_ms
          FROM context_traces
         GROUP BY organization_id, week, shadow_mode
        """
    )

    op.execute(
        """
        CREATE VIEW v_memory_inventory AS
        SELECT organization_id,
               scope,
               trust,
               status,
               count(*)                                        AS memories,
               count(*) FILTER (WHERE access_count > 0)         AS ever_read,
               round(avg(access_count)::numeric, 2)             AS avg_access_count,
               min(created_at)                                  AS oldest,
               max(created_at)                                  AS newest
          FROM memory_metadata
         GROUP BY organization_id, scope, trust, status
        """
    )
    # §13 risk 2: "Precision decays as the store grows, and nothing prevents it."
    # `v_memory_inventory` is the growth curve, and `ever_read` is the early warning:
    # a store where most memories have never been retrieved is a store that is
    # accumulating rather than remembering.


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS v_memory_inventory")
    op.execute("DROP VIEW IF EXISTS v_memory_token_effect")
    op.execute("DROP VIEW IF EXISTS v_memory_grades")
    op.drop_index("ix_trace_ungraded", table_name="context_traces")
    op.drop_index("ix_trace_org_time", table_name="context_traces")
    op.drop_index("ix_trace_run", table_name="context_traces")
    op.drop_table("context_traces")
