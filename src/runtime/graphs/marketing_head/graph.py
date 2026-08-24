"""`marketing_head@1` — plan, evaluate, gate, summarise.

    START → route ─┬→ plan      → finish → END   COORDINATION
                   ├→ evaluate  → finish → END   EVALUATION
                   ├→ gate      → finish → END   (no model call at all)
                   └→ summarize → finish → END   SUMMARIZATION

One actor, four entry points, three work classes. That concentration is the design:
almost the entire overhead side of the coordination ratio lives in this file, so
"what does management cost" is answerable by reading one module and one `call_site`
breakdown.

**The evaluation call is deliberately isolated.** §10's remedy for a rubber-stamp
evaluator is precise and it is implemented literally here: `_evaluate` builds a
context containing *only* the submitted artifact and the rubric. It does not carry
the session summary, the recent messages, or any trace of the conversation in which
this same actor assigned the work. An evaluator that can see itself deciding the
task was a good idea is not an independent judge of whether it was done well.

**The gate makes no model call.** Deciding whether an accepted `ContentDraft` exists
is a query; asking for approval is a write. Both are rules. §3 lists the steps that
must cost nothing and this is one of them — if the coordination ratio still comes
out high, that has to be because planning and evaluation are expensive, not because
the plumbing was quietly billed.

**Planning is one call, weekly.** §10: *"If planning is the cost, decompose weekly
instead of daily."* M1 starts where that advice ends.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from runtime.domain.enums import TaskStatus, WorkClass
from runtime.domain.errors import ApprovalDenied, ApprovalPending
from runtime.domain.hashing import canonical_json
from runtime.domain.ids import CorrelationId, OrganizationId, TaskId
from runtime.domain.outputs import (
    CONTENT_DRAFT_V1,
    EVALUATION_VERDICT_V1,
    WEEKLY_PLAN_V1,
    WEEKLY_SUMMARY_V1,
    EvaluationVerdict,
    WeeklyPlan,
)
from runtime.gateway.tools import ToolCall
from runtime.graphs.common.context import AssembledContext, actor_spec_block, assemble
from runtime.graphs.common.state import last, summarise
from runtime.graphs.common.structured import call_structured
from runtime.graphs.common.summarize import maybe_summarize
from runtime.graphs.registry import GRAPH_KEY, register_graph
from runtime.observability.logging import get_logger
from runtime.org.department import (
    ASSIGNEES,
    HEAD,
    SCHEMA_FOR_ASSIGNEE,
    correlation_for_week,
    week_of,
)
from runtime.org.rubrics import RUBRIC_VERSION, rubric_for

log = get_logger("graphs.marketing_head")

PLAN_SYSTEM = """You run a four-person marketing department: a researcher, a content
writer, and a deterministic analytics worker that computes metrics from the database.

Once a week you decide what the three of them will do. Three things make a plan good:

1. Each task names an outcome, not an activity. "Find out how the four largest
   competitors describe their pricing, and which of them do not publish it" is a
   task. "Research competitors" is a wish.
2. The objective carries enough context that the assignee does not have to guess.
   Most rejected work in this department is not wrong — it is right about a
   different question. You are the reason for that when it happens.
3. Acceptance criteria are checkable by someone who was not in this conversation."""

PLAN_INSTRUCTION = """Produce this week's plan: three to six tasks.

Assign each to `research`, `content`, or `analytics`, and pin each to the output
schema that assignee produces:

  research  → CompetitorReport@1
  content   → ContentDraft@1
  analytics → MetricsReport@1

If a content task depends on a research task, put the research task's title in the
content task's `task_input` under `depends_on_title` — the runtime resolves it to
the upstream artifact.

Do not plan work you cannot state the acceptance criteria for."""

EVAL_SYSTEM = """You are reviewing one piece of finished work against a written
rubric.

You did not commission this work and you have no context on it beyond what is below.
That is deliberate: you are here to judge what was produced, not to remember what
was intended.

Judge every rubric criterion explicitly, including the ones the work passes. If you
find yourself accepting work while noting an unmet criterion, the outcome is
ACCEPTED_WITH_EDITS or REWORK_REQUIRED — not ACCEPTED. Twenty per cent of your
acceptances are re-reviewed by a human against this same rubric, and the number
being tracked is how often you accepted something they rejected."""

SUMMARY_SYSTEM = """You write the weekly summary a human actually reads.

They were not here. They need to know what happened, what it cost, and what needs
them. Report what did not ship as plainly as what did — a summary that lists only
successes is advocacy, and it is how a department drifts for a month before anybody
notices."""


def _reduce(_current: Any, incoming: Any) -> Any:
    return last(_current, incoming)


class HeadState(TypedDict, total=False):
    input: Annotated[dict[str, Any], _reduce]
    mode: Annotated[str, _reduce]
    output: Annotated[dict[str, Any], _reduce]


# --- routing -----------------------------------------------------------------------


async def _route(state: HeadState, config: RunnableConfig) -> dict[str, Any]:
    node = config["configurable"][GRAPH_KEY]
    payload = state.get("input", {}) or dict(node.ctx.spec.input)
    mode = str(payload.get("mode") or "weekly_plan")
    return {"mode": mode, "input": payload}


def _branch(state: HeadState) -> str:
    mode = state.get("mode", "weekly_plan")
    if mode in {"evaluate", "task.submitted"}:
        return "evaluate"
    if mode == "publish_gate":
        return "gate"
    if mode == "weekly_summary":
        return "summarize"
    return "plan"


# --- COORDINATION: the Monday plan -------------------------------------------------


async def _plan(state: HeadState, config: RunnableConfig) -> dict[str, Any]:
    node = config["configurable"][GRAPH_KEY]
    ctx = node.ctx
    org_id = OrganizationId(ctx.organization_id)
    now = dt.datetime.now(dt.UTC)
    week = week_of(now)
    correlation = CorrelationId(correlation_for_week(org_id, week))

    charter = await node.org.goals.charter(org_id)
    goal, project = charter.goal, charter.project

    open_tasks = await node.org.tasks.open_tasks(org_id)
    messages = await node.org.inbox.recent_for(org_id, HEAD)
    session = await node.org.sessions.open(org_id, HEAD, correlation_id=correlation, now=now)
    last_week = await node.org.metrics.for_week(org_id, week - dt.timedelta(days=7))

    # M3. The plan node is the one place in the department where memory should pay for
    # itself most obviously: §1's hypotheses are all about the head re-deriving the same
    # thing week after week. Retrieved against the goal, not against last week's
    # numbers — the numbers are already in `task_input` and are this week's.
    plan_recall = await node.recall(
        "plan",
        " ".join(
            filter(None, [goal.statement if goal else None, project.name if project else None])
        )[:1_000],
        call_site="head.weekly_plan",
    )

    context = assemble(
        system_prompt=PLAN_SYSTEM,
        spec=ctx.spec,
        task_input={
            "week_of": week.isoformat(),
            "goal": goal.statement if goal else None,
            "project": project.name if project else None,
            "assignees": list(ASSIGNEES),
            "schema_per_assignee": SCHEMA_FOR_ASSIGNEE,
            "last_week": {
                "accepted": last_week.accepted_tasks,
                "bounced": last_week.bounced_tasks,
                "auto_accepted": last_week.auto_accepted_tasks,
                "spend_cents": last_week.total_spend_cents,
                "coordination_ratio": last_week.coordination_ratio,
            },
            "still_open": [
                {"title": t.title, "assignee": t.assignee_name, "status": t.status.value}
                for t in open_tasks[:12]
            ],
        },
        session_summary=session.summary,
        recent_messages=messages,
        instruction=PLAN_INSTRUCTION,
        memory=plan_recall.block,
    )

    with ctx.node("plan"):
        result = await call_structured(
            ctx,
            node.models,
            context,
            WEEKLY_PLAN_V1,
            work_class=WorkClass.COORDINATION,
            call_site="head.weekly_plan",
        )
    plan: WeeklyPlan = result.value  # type: ignore[assignment]

    created = await _materialise(node, plan, org_id, correlation, goal, project, now)
    log.info(
        "head.planned",
        week=week.isoformat(),
        tasks=len(created),
        cost_cents=result.cost_cents,
        **ctx.log_fields(),
    )
    return {
        "output": {
            "mode": "weekly_plan",
            "week_of": week.isoformat(),
            "correlation_id": str(correlation),
            "tasks": created,
            "cost_cents": result.cost_cents,
        }
    }


async def _materialise(
    node: Any,
    plan: WeeklyPlan,
    org_id: OrganizationId,
    correlation: CorrelationId,
    goal: Any,
    project: Any,
    now: dt.datetime,
) -> list[dict[str, Any]]:
    """Turn a plan into task rows. Zero model calls, and dependencies resolved by
    title in a second pass — the head names them, the runtime wires them."""
    created: list[dict[str, Any]] = []
    by_title: dict[str, TaskId] = {}

    for planned in plan.tasks:
        if planned.assignee not in ASSIGNEES:
            # The schema cannot check this (it does not know the department), so it
            # is checked here rather than becoming a task nobody can be assigned.
            log.warning("head.unknown_assignee", assignee=planned.assignee, title=planned.title)
            continue
        expected = SCHEMA_FOR_ASSIGNEE[planned.assignee]
        schema_ref = planned.output_schema_ref
        if schema_ref != expected:
            log.warning(
                "head.schema_corrected",
                assignee=planned.assignee,
                asked_for=schema_ref,
                corrected_to=expected,
            )
            schema_ref = expected

        task_id, was_new = await node.org.tasks.create_and_assign(
            organization_id=org_id,
            correlation_id=correlation,
            title=planned.title,
            objective=planned.objective,
            acceptance_criteria=list(planned.acceptance_criteria),
            assignee_name=planned.assignee,
            output_schema_ref=schema_ref,
            task_input=dict(planned.task_input),
            created_by_actor_id=node.ctx.actor_id,
            goal_id=goal.id if goal else None,
            project_id=project.id if project else None,
            due_at=now + dt.timedelta(days=planned.due_offset_days),
            sender=HEAD,
            now=now,
        )
        by_title[planned.title] = task_id
        created.append(
            {
                "task_id": str(task_id),
                "title": planned.title,
                "assignee": planned.assignee,
                "schema": schema_ref,
                "created": was_new,
            }
        )

    await _wire_dependencies(node, plan, by_title)
    return created


async def _wire_dependencies(node: Any, plan: WeeklyPlan, by_title: dict[str, TaskId]) -> None:
    """Resolve `depends_on_title` to a real task id, in a second pass.

    Two passes because a plan may name a dependency that appears later in the list,
    and a single pass would resolve it to nothing half the time depending on the
    order the model happened to emit.
    """
    for planned in plan.tasks:
        depends = planned.task_input.get("depends_on_title")
        if not depends:
            continue
        upstream = by_title.get(str(depends))
        downstream = by_title.get(planned.title)
        if upstream is None or downstream is None:
            log.warning("head.unresolved_dependency", title=planned.title, depends_on=depends)
            continue
        await node.org.tasks.set_dependency(downstream, upstream)


# --- EVALUATION: judging one submitted task ----------------------------------------


async def _evaluate(state: HeadState, config: RunnableConfig) -> dict[str, Any]:
    node = config["configurable"][GRAPH_KEY]
    ctx = node.ctx
    org_id = OrganizationId(ctx.organization_id)
    payload = state.get("input", {})

    raw_task_id = payload.get("task_id") or (str(ctx.task_id) if ctx.task_id else None)
    if raw_task_id is None:
        return {"output": {"mode": "evaluate", "evaluated": False, "reason": "no task_id"}}

    task = await node.org.tasks.get(TaskId(_uuid(str(raw_task_id))))
    if task is None:
        return {"output": {"mode": "evaluate", "evaluated": False, "reason": "task not found"}}
    if task.status is not TaskStatus.SUBMITTED:
        # Already judged, or auto-accepted by the sweep while this run queued. Not
        # an error — it is the ordinary outcome of a race the sweep is meant to win.
        return {
            "output": {
                "mode": "evaluate",
                "evaluated": False,
                "task_id": str(task.id),
                "reason": f"task is {task.status.value}, not SUBMITTED",
                "outcome": task.outcome.value if task.outcome else None,
            }
        }

    rubric = rubric_for(task.output_schema_ref)
    context = _isolated_evaluation_context(ctx, task, rubric)

    with ctx.node("evaluate"):
        result = await call_structured(
            ctx,
            node.models,
            context,
            EVALUATION_VERDICT_V1,
            work_class=WorkClass.EVALUATION,
            call_site="head.evaluate",
        )
    verdict: EvaluationVerdict = result.value  # type: ignore[assignment]

    applied = await node.org.evaluation.apply_verdict(
        task,
        verdict,
        organization_id=org_id,
        manager_name=HEAD,
        evaluator_actor_id=ctx.actor_id,
        run_id=ctx.run_id,
        cost_cents=result.cost_cents,
    )
    log.info(
        "head.evaluated",
        task_id=str(task.id),
        outcome=applied.outcome.value,
        reworked=applied.reworked,
        edit_distance=applied.edit_distance,
        cost_cents=result.cost_cents,
        **ctx.log_fields(),
    )
    return {
        "output": {
            "mode": "evaluate",
            "evaluated": applied.recorded,
            "task_id": str(task.id),
            "outcome": applied.outcome.value,
            "reworked": applied.reworked,
            "closed": applied.closed,
            "edit_distance": applied.edit_distance,
            "rubric_version": RUBRIC_VERSION,
            "cost_cents": result.cost_cents,
        }
    }


def _isolated_evaluation_context(ctx: Any, task: Any, rubric: Any) -> AssembledContext:
    """The artifact and the rubric. Nothing else.

    §10, on a high false-accept rate: *"the evaluation call needs to be a separate
    model call with only the artifact and the rubric in context — not a continuation
    of the conversation where it assigned the work."*

    So this does **not** call `assemble()`. The fixed template would helpfully add
    the session summary and the last six messages — which include this actor
    deciding the task was a good idea — and that is exactly the contamination the
    rule exists to remove. Written by hand so the omission is visible rather than
    being an argument somebody could pass differently later.
    """
    system = f"{EVAL_SYSTEM}\n\n{actor_spec_block(ctx.spec)}"
    prompt = (
        f"{rubric.as_prompt()}\n\n"
        "## What was asked for\n"
        f"Title: {task.title}\n"
        f"Objective: {task.objective}\n"
        "Acceptance criteria:\n"
        + "\n".join(f"- {c}" for c in task.acceptance_criteria)
        + f"\n\n## What was produced (attempt {task.rework_count + 1}, "
        f"schema {task.output_schema_ref})\n```json\n"
        + summarise(task.output or {}, 24_000)
        + "\n```\n\n"
        "Judge it. If the outcome is ACCEPTED_WITH_EDITS you must return the "
        "corrected object in `edited_output`, conforming to the same schema — it is "
        "revalidated before it is stored. If REWORK_REQUIRED, say precisely what to "
        "change."
    )
    return AssembledContext(
        system=system,
        prompt=prompt,
        parts={"system": len(system), "rubric_and_artifact": len(prompt)},
    )


# --- the publish gate: no model call ------------------------------------------------


async def _gate(state: HeadState, config: RunnableConfig) -> dict[str, Any]:
    """Thursday. Try to publish accepted content; record what the gate says.

    `ApprovalPending` is caught and reported rather than failing the run. A run that
    ends FAILED because a human has not answered would make "how often does the gate
    block us" indistinguishable from "how often does the runtime break", and §2
    wants the first one counted.
    """
    node = config["configurable"][GRAPH_KEY]
    ctx = node.ctx
    org_id = OrganizationId(ctx.organization_id)
    week = week_of()
    correlation = correlation_for_week(org_id, week)

    candidates = [
        t
        for t in await node.org.tasks.for_correlation(CorrelationId(correlation))
        if t.output_schema_ref == CONTENT_DRAFT_V1
        and t.outcome is not None
        and t.outcome.is_accepted
        and t.output
    ]
    if not candidates:
        return {
            "output": {
                "mode": "publish_gate",
                "candidates": 0,
                "reason": "no accepted ContentDraft this week",
            }
        }

    results: list[dict[str, Any]] = []
    for i, task in enumerate(candidates[:2]):
        draft = task.output or {}
        with ctx.node("gate", iteration=i):
            try:
                outcome = await node.gateway.execute(
                    ctx,
                    ToolCall(
                        tool="publish.external@1",
                        args={
                            "title": str(draft.get("title", task.title))[:200],
                            "body_markdown": str(draft.get("body_markdown", "")),
                            "channel": str(draft.get("channel", "blog")),
                            "task_id": str(task.id),
                        },
                    ),
                )
            except ApprovalPending as exc:
                results.append({"task_id": str(task.id), "published": False, "waiting": str(exc)})
                continue
            except ApprovalDenied as exc:
                results.append({"task_id": str(task.id), "published": False, "denied": str(exc)})
                continue
        results.append(
            {
                "task_id": str(task.id),
                "published": True,
                "target": outcome.value.get("target"),
                "url": outcome.value.get("url"),
            }
        )

    log.info(
        "head.gate",
        candidates=len(candidates),
        published=sum(1 for r in results if r.get("published")),
        waiting=sum(1 for r in results if r.get("waiting")),
        **ctx.log_fields(),
    )
    return {"output": {"mode": "publish_gate", "candidates": len(candidates), "results": results}}


# --- SUMMARIZATION: the Friday artifact a human reads -------------------------------


async def _summarize_week(state: HeadState, config: RunnableConfig) -> dict[str, Any]:
    node = config["configurable"][GRAPH_KEY]
    ctx = node.ctx
    org_id = OrganizationId(ctx.organization_id)
    week = week_of()
    correlation = CorrelationId(correlation_for_week(org_id, week))

    tasks = await node.org.tasks.for_correlation(correlation)
    metrics = await node.org.metrics.for_week(org_id, week)
    session = await node.org.sessions.open(org_id, HEAD, correlation_id=correlation)
    messages = await node.org.inbox.recent_for(org_id, HEAD)
    pending = await node.org.approvals.pending(org_id)

    context = assemble(
        system_prompt=SUMMARY_SYSTEM,
        spec=ctx.spec,
        task_input={
            "week_of": week.isoformat(),
            "tasks": [
                {
                    "title": t.title,
                    "assignee": t.assignee_name,
                    "status": t.status.value,
                    "outcome": t.outcome.value if t.outcome else None,
                    "reason": t.outcome_reason,
                    "rework_count": t.rework_count,
                    "schema_failures": t.schema_failures,
                }
                for t in tasks
            ],
            "metrics": {
                "accepted": metrics.accepted_tasks,
                "submitted": metrics.submitted_tasks,
                "bounced": metrics.bounced_tasks,
                "auto_accepted": metrics.auto_accepted_tasks,
                "spend_cents": metrics.total_spend_cents,
                "cost_per_accepted_cents": metrics.cost_per_accepted_cents,
                "coordination_ratio": metrics.coordination_ratio,
                "overhead_ratio": metrics.overhead_ratio,
                "gate_failures": metrics.gate_failures(),
                "pass_failures": metrics.pass_failures(),
            },
            "approvals_waiting": [
                {"action": a.action, "subject": a.subject_id, "expires": a.expires_at.isoformat()}
                for a in pending
            ],
        },
        session_summary=session.summary,
        recent_messages=messages,
        instruction=(
            "Write the weekly summary. Engage with the numbers above, including the "
            "bad ones — a stop threshold that was tripped is the most important "
            "sentence in the document. Anything in `approvals_waiting` is a decision "
            "somebody has to make; say so."
        ),
    )

    with ctx.node("weekly_summary"):
        result = await call_structured(
            ctx,
            node.models,
            context,
            WEEKLY_SUMMARY_V1,
            work_class=WorkClass.SUMMARIZATION,
            call_site="head.weekly_summary",
        )
    payload = result.value.model_dump(mode="json")

    ref = await node.artifacts.put(
        canonical_json(payload).encode("utf-8"),
        organization_id=org_id,
        run_id=ctx.run_id,
        kind=f"task_output:{WEEKLY_SUMMARY_V1}",
        content_type="application/json",
    )
    await node.org.inbox.send(
        organization_id=org_id,
        kind="note",
        recipient="operator",
        correlation_id=correlation,
        key=f"weekly-summary:{week.isoformat()}",
        subject=str(payload.get("headline", "weekly summary")),
        body={"artifact_id": str(ref.artifact_id), "week_of": week.isoformat()},
        sender=HEAD,
        artifact_id=ref.artifact_id,
        hop_count=0,
    )
    log.info(
        "head.summarized",
        week=week.isoformat(),
        artifact_id=str(ref.artifact_id),
        cost_cents=result.cost_cents,
        **ctx.log_fields(),
    )
    return {
        "output": {
            "mode": "weekly_summary",
            "week_of": week.isoformat(),
            "summary": payload,
            "artifact_id": str(ref.artifact_id),
            "cost_cents": result.cost_cents,
        }
    }


# --- the session summary every LLM actor ends with ---------------------------------


async def _finish(state: HeadState, config: RunnableConfig) -> dict[str, Any]:
    node = config["configurable"][GRAPH_KEY]
    ctx = node.ctx
    org_id = OrganizationId(ctx.organization_id)
    session = await node.org.sessions.open(org_id, HEAD)
    messages = await node.org.inbox.recent_for(org_id, HEAD, limit=40)
    outcome = await maybe_summarize(
        ctx,
        node.models,
        node.org.sessions,
        session,
        messages,
        previous_summary=session.summary,
    )
    output = dict(state.get("output", {}))
    output["session_summary"] = outcome
    return {"output": output}


def _uuid(value: str) -> Any:
    import uuid

    return uuid.UUID(value)


def build() -> StateGraph[HeadState, Any, Any, Any]:
    graph: StateGraph[HeadState, Any, Any, Any] = StateGraph(HeadState)
    graph.add_node("route", _route)
    graph.add_node("plan", _plan)
    graph.add_node("evaluate", _evaluate)
    graph.add_node("gate", _gate)
    graph.add_node("summarize", _summarize_week)
    graph.add_node("finish", _finish)
    graph.add_edge(START, "route")
    graph.add_conditional_edges(
        "route",
        _branch,
        {"plan": "plan", "evaluate": "evaluate", "gate": "gate", "summarize": "summarize"},
    )
    for node_name in ("plan", "evaluate", "gate", "summarize"):
        graph.add_edge(node_name, "finish")
    graph.add_edge("finish", END)
    return graph


register_graph("marketing_head@1", build)
