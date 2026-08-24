"""015 metrics — the four metric views, plus the fact view they rest on

Revision ID: 015_metrics
Revises: 014_approvals

This migration lands before any prompt tuning (§8) and it is the one T26 exists to
protect. If the SQL here is wrong, every number in §9 is wrong, the go/no-go
decision is made on fiction, and nothing else in the system would catch it.

Three deliberate departures from the §7 sketch, each because the sketch would have
flattered the result:

**Rejection rate is "was it ever bounced", not "what was its final outcome".** A
task reworked twice and then accepted has `outcome = 'ACCEPTED'`; counting only the
final outcome would score that week as zero rejections. `v_metric_rejection_rate`
counts a task as bounced if it ever received a REJECTED or REWORK_REQUIRED verdict.
Rework is the cost the metric is trying to expose, so hiding it behind a later
success defeats the point.

**The coordination ratio is computed over model spend only** (`kind = 'model'`),
with tool spend reported alongside rather than folded in. A tool call is made
*during* work but carries no `work_class`, so including it in the denominator would
push cost into "overhead" that is nothing of the kind. The question the ratio
answers is "what share of the thinking is the organization talking to itself".

**Plain views, not materialized.** §4 says materialized; a materialized view needs
a refresh job, and a refresh job is a thing that can be stale on the morning
somebody reads the dashboard and makes a decision. At M1 volumes the cost of a live
view is nil, and a number that is always current is worth more than one that is
fast.

Weeks are UTC and start on Monday (`date_trunc('week', … AT TIME ZONE 'UTC')`),
which lines up with the Monday-plan / Friday-summary loop. Tasks are bucketed by
when they were *created* — the week they were planned — and spend by when it was
*incurred*.

`AUTO_ACCEPTED` appears in exactly one place: its own share. It is absent from every
acceptance numerator and from the cost-per-accepted denominator, because an
unexamined task is not evidence that the organization produced acceptable work.
"""

from __future__ import annotations

from alembic import op

revision: str = "015_metrics"
down_revision: str | None = "014_approvals"
branch_labels = None
depends_on = None

WEEK = "date_trunc('week', {ts} AT TIME ZONE 'UTC')::date"


VIEWS: list[tuple[str, str]] = [
    (
        "v_task_facts",
        f"""
        SELECT
            t.id                                    AS task_id,
            t.organization_id,
            t.assignee_name,
            t.output_schema_ref,
            t.correlation_id,
            {WEEK.format(ts="t.created_at")}        AS week_start,
            t.status,
            t.outcome,
            t.outcome_reason,
            t.human_touched,
            t.rework_count,
            t.schema_failures,
            t.created_at,
            t.submitted_at,
            t.evaluated_at,
            (t.submitted_at IS NOT NULL)            AS was_submitted,
            (t.outcome IN ('ACCEPTED','ACCEPTED_WITH_EDITS'))  AS is_accepted,
            (t.outcome = 'AUTO_ACCEPTED')                      AS is_auto_accepted,
            (t.outcome = 'REJECTED')                           AS is_rejected,
            COALESCE(b.bounced, false)              AS was_bounced,
            COALESCE(c.cost_cents, 0)               AS task_cost_cents
        FROM tasks t
        LEFT JOIN (
            SELECT r.task_id, sum(u.cost_cents) AS cost_cents
              FROM usage_ledger u
              JOIN runs r ON r.id = u.run_id
             WHERE r.task_id IS NOT NULL
             GROUP BY r.task_id
        ) c ON c.task_id = t.id
        LEFT JOIN (
            -- "Was this ever sent back?" — independent of where it ended up.
            SELECT e.task_id, true AS bounced
              FROM task_evaluations e
             WHERE e.evaluator_kind = 'manager'
               AND e.outcome IN ('REJECTED','REWORK_REQUIRED')
             GROUP BY e.task_id
        ) b ON b.task_id = t.id
        """,
    ),
    (
        "v_weekly_spend",
        f"""
        SELECT
            {WEEK.format(ts="u.created_at")}        AS week_start,
            u.organization_id,
            sum(u.cost_cents)                                                   AS total_cost_cents,
            sum(u.cost_cents) FILTER (WHERE u.kind = 'model')                   AS model_cost_cents,
            sum(u.cost_cents) FILTER (WHERE u.kind <> 'model')                  AS tool_cost_cents,
            sum(u.cost_cents) FILTER (WHERE u.work_class = 'work')              AS work_cost_cents,
            sum(u.cost_cents) FILTER (WHERE u.work_class = 'coordination')      AS coordination_cost_cents,
            sum(u.cost_cents) FILTER (WHERE u.work_class = 'evaluation')        AS evaluation_cost_cents,
            sum(u.cost_cents) FILTER (WHERE u.work_class = 'summarization')     AS summarization_cost_cents,
            sum(u.input_tokens)                                                 AS input_tokens,
            sum(u.output_tokens)                                                AS output_tokens,
            count(*) FILTER (WHERE u.kind = 'model')                            AS model_calls
        FROM usage_ledger u
        GROUP BY 1, 2
        """,
    ),
    (
        # --- METRIC 1 -------------------------------------------------------------
        # Total spend divided by outcomes a human would sign off on. The numerator is
        # *all* spend, not just spend attributable to accepted tasks: the question is
        # "what does one accepted outcome cost", and the failed attempts, the
        # planning and the evaluation are part of that cost.
        "v_metric_cost_per_accepted",
        """
        -- COALESCE on both join keys, not `s.week_start`. This is a FULL JOIN, so a
        -- week with accepted tasks and no spend yet — or spend and no tasks — has a
        -- NULL on one side, and reading the key from that side silently drops the
        -- row. That failure mode is quiet and it points the wrong way: a week of
        -- accepted work would report zero accepted.
        SELECT
            COALESCE(s.week_start, a.week_start)                AS week_start,
            COALESCE(s.organization_id, a.organization_id)      AS organization_id,
            COALESCE(s.total_cost_cents, 0)                     AS total_cost_cents,
            COALESCE(a.accepted_tasks, 0)                       AS accepted_tasks,
            CASE WHEN COALESCE(a.accepted_tasks, 0) > 0
                 THEN (COALESCE(s.total_cost_cents, 0)::numeric / a.accepted_tasks)
                 ELSE NULL END                                  AS cost_per_accepted_cents
        FROM v_weekly_spend s
        FULL JOIN (
            SELECT week_start, organization_id,
                   count(*) FILTER (WHERE is_accepted) AS accepted_tasks
              FROM v_task_facts
             GROUP BY 1, 2
        ) a ON a.week_start = s.week_start AND a.organization_id = s.organization_id
        """,
    ),
    (
        # --- METRIC 2 -------------------------------------------------------------
        "v_metric_rejection_rate",
        """
        SELECT
            f.week_start,
            f.organization_id,
            count(*) FILTER (WHERE f.was_submitted)                     AS submitted_tasks,
            count(*) FILTER (WHERE f.was_submitted AND f.was_bounced)   AS bounced_tasks,
            count(*) FILTER (WHERE f.is_rejected)                       AS rejected_tasks,
            (count(*) FILTER (WHERE f.was_submitted AND f.was_bounced))::numeric
                / nullif(count(*) FILTER (WHERE f.was_submitted), 0)    AS rejection_rate
        FROM v_task_facts f
        GROUP BY 1, 2
        """,
    ),
    (
        # --- METRIC 3 -------------------------------------------------------------
        # Denominator is *evaluated* tasks. A task still in flight is not evidence
        # either way, and including it would make the rate drift down simply because
        # the week is young.
        "v_metric_unassisted_completion",
        """
        SELECT
            f.week_start,
            f.organization_id,
            count(*) FILTER (WHERE f.outcome IS NOT NULL)               AS evaluated_tasks,
            count(*) FILTER (WHERE f.outcome = 'ACCEPTED'
                               AND NOT f.human_touched)                 AS unassisted_tasks,
            (count(*) FILTER (WHERE f.outcome = 'ACCEPTED' AND NOT f.human_touched))::numeric
                / nullif(count(*) FILTER (WHERE f.outcome IS NOT NULL), 0)
                                                                        AS unassisted_rate
        FROM v_task_facts f
        GROUP BY 1, 2
        """,
    ),
    (
        # --- METRIC 4 -------------------------------------------------------------
        # `overhead_ratio` is 1 - work_share rather than a sum of the named non-work
        # classes. A call tagged with some other class, or with none, therefore
        # counts as overhead — mis-tagging makes the number worse, never better.
        "v_metric_coordination_ratio",
        """
        SELECT
            s.week_start,
            s.organization_id,
            s.model_cost_cents,
            s.tool_cost_cents,
            s.coordination_cost_cents,
            s.evaluation_cost_cents,
            s.summarization_cost_cents,
            s.work_cost_cents,
            s.coordination_cost_cents::numeric / nullif(s.model_cost_cents, 0)
                                                                AS coordination_ratio,
            1 - (COALESCE(s.work_cost_cents, 0)::numeric / nullif(s.model_cost_cents, 0))
                                                                AS overhead_ratio
        FROM v_weekly_spend s
        """,
    ),
    (
        # --- The rest of the dashboard, non-negotiably (§7) -------------------------
        # `human_agreement` and `false_accept_rate` are computed only over tasks the
        # human actually sampled. A week with no sample yields NULL, not 1.0 —
        # "we did not look" must not render as "we agreed".
        "v_metric_dashboard",
        """
        SELECT
            f.week_start,
            f.organization_id,
            count(*)                                                    AS tasks,
            count(*) FILTER (WHERE f.outcome IS NOT NULL)               AS evaluated_tasks,
            count(*) FILTER (WHERE f.is_auto_accepted)                  AS auto_accepted_tasks,
            (count(*) FILTER (WHERE f.is_auto_accepted))::numeric
                / nullif(count(*) FILTER (WHERE f.outcome IS NOT NULL), 0)
                                                                        AS auto_accepted_share,
            avg(m.edit_distance) FILTER (WHERE m.edit_distance IS NOT NULL)
                                                                        AS mean_edit_distance,
            count(*) FILTER (WHERE h.task_id IS NOT NULL)               AS human_sampled_tasks,
            count(*) FILTER (WHERE h.task_id IS NOT NULL
                               AND h.human_accepted = m.manager_accepted)
                                                                        AS agreements,
            (count(*) FILTER (WHERE h.task_id IS NOT NULL
                                AND h.human_accepted = m.manager_accepted))::numeric
                / nullif(count(*) FILTER (WHERE h.task_id IS NOT NULL), 0)
                                                                        AS human_agreement,
            count(*) FILTER (WHERE h.task_id IS NOT NULL
                               AND m.manager_accepted AND NOT h.human_accepted)
                                                                        AS false_accepts,
            (count(*) FILTER (WHERE h.task_id IS NOT NULL
                                AND m.manager_accepted AND NOT h.human_accepted))::numeric
                / nullif(count(*) FILTER (WHERE h.task_id IS NOT NULL), 0)
                                                                        AS false_accept_rate
        FROM v_task_facts f
        LEFT JOIN (
            SELECT DISTINCT ON (task_id) task_id,
                   outcome IN ('ACCEPTED','ACCEPTED_WITH_EDITS') AS manager_accepted,
                   edit_distance
              FROM task_evaluations
             WHERE evaluator_kind = 'manager'
             ORDER BY task_id, attempt DESC, created_at DESC
        ) m ON m.task_id = f.task_id
        LEFT JOIN (
            SELECT DISTINCT ON (task_id) task_id,
                   outcome IN ('ACCEPTED','ACCEPTED_WITH_EDITS') AS human_accepted
              FROM task_evaluations
             WHERE evaluator_kind = 'human'
             ORDER BY task_id, created_at DESC
        ) h ON h.task_id = f.task_id
        GROUP BY 1, 2
        """,
    ),
]


def upgrade() -> None:
    for name, body in VIEWS:
        op.execute(f"CREATE VIEW {name} AS {body}")


def downgrade() -> None:
    for name, _ in reversed(VIEWS):
        op.execute(f"DROP VIEW IF EXISTS {name}")
