"""`analytics@1` — the deterministic actor, and the one the gate is read off.

PR-19 ships this **before** the three LLM actors, and the build order is the point:
if the deterministic path cannot produce a validated artifact and get it evaluated,
the LLM path will not either, and you will have spent two weeks debugging prompts to
discover a task-lifecycle bug.

It is also the control. Zero model calls, enforced by `max_llm_calls = 0` at the
`ModelGateway` and by having no model profiles at all — two independent refusals for
the one actor that must not spend. T24 makes it try, and the gateway rejects.

What it does is read the migration-015 views, shape them into a `MetricsReport@1`,
and submit. Every number in §9 comes out of this actor, which is why nothing in its
provenance is a language model: if the go/no-go decision is going to be argued
about, the argument should be about the SQL, and the SQL is right there.

Two modes:

- `weekly_metrics` — the Friday cron. Computes the week, submits it as a task if
  one was assigned, and otherwise just returns the report so a human can read it.
- `work` — an ordinary assigned task, arriving by inbox message like any other.

The `MetricsReport` schema cross-validates its rates against its counts, so a view
whose arithmetic has drifted fails validation here rather than being rendered on a
dashboard. That check is most of what T26 is buying.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from runtime.domain.enums import TaskStatus
from runtime.domain.ids import OrganizationId, TaskId
from runtime.handlers.registry import HandlerContext, register_handler
from runtime.observability.logging import get_logger
from runtime.org.department import HEAD, week_of

log = get_logger("handlers.analytics")


async def analytics(handler_ctx: HandlerContext, payload: dict[str, Any]) -> dict[str, Any]:
    ctx = handler_ctx.ctx
    org = handler_ctx.org
    organization_id = OrganizationId(ctx.organization_id)

    week = _requested_week(payload)
    metrics = await org.metrics.for_week(organization_id, week)
    report = org.metrics.to_report(metrics)
    payload_json = report.model_dump(mode="json")

    log.info(
        "analytics.computed",
        week=week.isoformat(),
        evaluated_tasks=metrics.evaluated_tasks,
        accepted_tasks=metrics.accepted_tasks,
        coordination_ratio=metrics.coordination_ratio,
        total_spend_cents=metrics.total_spend_cents,
        **ctx.log_fields(),
    )

    task_id = ctx.task_id
    if task_id is None:
        # A cron fire with no task attached. Still a real output — the Friday
        # summary reads it — so it is returned rather than discarded, and the
        # absence of a task is stated rather than implied.
        return {
            "report": payload_json,
            "task_id": None,
            "submitted": False,
            "reason": "no task attached to this run",
            "gate_failures": metrics.gate_failures(),
            "pass_failures": metrics.pass_failures(),
        }

    submitted = await _submit(handler_ctx, task_id, payload_json, organization_id)
    return {
        "report": payload_json,
        "task_id": str(task_id),
        "submitted": submitted.ok,
        "errors": submitted.errors,
        "gate_failures": metrics.gate_failures(),
        "pass_failures": metrics.pass_failures(),
    }


async def _submit(
    handler_ctx: HandlerContext,
    task_id: TaskId,
    report: dict[str, Any],
    organization_id: OrganizationId,
) -> Any:
    """Claim the task, submit against its pinned schema, hand it to the manager.

    The claim is not ceremony. T21 is two workers racing for one task, and this
    actor is reachable from both a cron fire and an inbox message — which is
    precisely the shape that produces two runs for one task.
    """
    ctx = handler_ctx.ctx
    org = handler_ctx.org

    task = await org.tasks.claim(task_id, ctx.worker_id)
    if task is None:
        existing = await org.tasks.get(task_id)
        log.warning(
            "analytics.task_not_claimable",
            task_id=str(task_id),
            status=existing.status.value if existing else "missing",
        )
        return _NotSubmitted(
            reason="another worker holds this task"
            if existing and existing.status is TaskStatus.IN_PROGRESS
            else "task is not claimable"
        )

    return await org.tasks.submit(
        task_id,
        worker_id=ctx.worker_id,
        result=report,
        organization_id=organization_id,
        run_id=ctx.run_id,
        manager_name=HEAD,
    )


class _NotSubmitted:
    """A submit that never happened, shaped like one that did.

    Cheaper than making the caller branch on `None`, and it keeps the failure
    reason in the run's output where an operator will actually see it.
    """

    ok = False

    def __init__(self, reason: str) -> None:
        self.errors = [{"pointer": "/", "message": reason, "kind": "not_claimed"}]


def _requested_week(payload: dict[str, Any]) -> dt.date:
    """Which week to report on.

    Defaults to the week *just finished* rather than the current one. The Friday
    cron fires inside the week it is reporting on, so `week_of(now)` would be
    correct; an operator running this by hand on a Monday almost always means last
    week. The explicit `week` input settles it either way.
    """
    explicit = payload.get("week")
    if explicit:
        return dt.date.fromisoformat(str(explicit))
    return week_of(dt.datetime.now(dt.UTC))


register_handler("analytics@1", analytics)
