"""`content@1` — draft → `ContentDraft@1`. WORK class.

    START → load → draft → submit → summarize → END

Shorter than `research` because it is the actor that consumes another actor's
output rather than going out to get its own. That makes it the one that exercises
**artifact flow between tasks**, which is a scope item in its own right (§2): the
head's plan puts a `source_task_id` in this task's input, `load` resolves it to the
upstream task's output artifact, and `draft` reads the bytes inside the node.

Same discipline as `research`: the resolved report reaches state as an
`ArtifactRefView` and is loaded where it is used. A `CompetitorReport` is ~8 KB;
carried in state across four supersteps it would be 32 KB of checkpoint per run, per
replay, for bytes that already have a digest.

The one thing worth watching in this actor is `word_count`. `ContentDraft`
cross-validates the declared count against the body it ships with, so a draft whose
metadata disagrees with its own text fails validation. That is deliberate and it is
the cheapest possible lie to catch — an agent that will misreport a number it could
have counted will misreport others.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from runtime.domain.enums import WorkClass
from runtime.domain.errors import OutputSchemaViolation, TaskStateError, UpstreamNotReady
from runtime.domain.ids import OrganizationId, TaskId
from runtime.domain.outputs import CONTENT_DRAFT_V1
from runtime.graphs.common.context import assemble
from runtime.graphs.common.state import ArtifactRefView, last, summarise
from runtime.graphs.common.structured import call_structured, record_schema_failure
from runtime.graphs.common.summarize import maybe_summarize
from runtime.graphs.registry import GRAPH_KEY, register_graph
from runtime.observability.logging import get_logger
from runtime.org.department import CONTENT, HEAD

log = get_logger("graphs.content")

SYSTEM = """You are a content writer in a small marketing department.

You write one substantive piece at a time, from evidence somebody else gathered.

1. Every factual claim in your draft is either in the research you were given, or it
   is not in the draft. You have no other sources and no independent knowledge you
   are entitled to use here.
2. Write for a specific reader doing a specific job. "Businesses looking to leverage
   AI" is not an audience.
3. `word_count` must be the number of words in `body_markdown`. Count it. A draft
   whose own metadata is wrong will be rejected before anybody reads the prose."""

INSTRUCTION = """Write the draft now.

Ground every claim in the research below and cite it — each entry in `claims` points
at an index in your `sources` list, and those sources come from the research, not
from memory.

The body is markdown. Structure it with the sections you declare. Aim for the length
the task asks for; if it does not ask, 700-1000 words."""


def _reduce(_current: Any, incoming: Any) -> Any:
    return last(_current, incoming)


class ContentState(TypedDict, total=False):
    input: Annotated[dict[str, Any], _reduce]
    task: Annotated[dict[str, Any] | None, _reduce]
    source_ref: Annotated[dict[str, Any] | None, _reduce]
    """An `ArtifactRefView` for the upstream report, never the report."""
    draft: Annotated[dict[str, Any] | None, _reduce]
    output: Annotated[dict[str, Any], _reduce]


async def _load(state: ContentState, config: RunnableConfig) -> dict[str, Any]:
    """Read the task and resolve the upstream artifact it references.

    Deterministic. Following a `source_task_id` to an artifact id is a join, not a
    judgement, and §3 counts it among the steps that cost nothing.
    """
    node = config["configurable"][GRAPH_KEY]
    ctx = node.ctx

    task: dict[str, Any] | None = None
    source_ref: dict[str, Any] | None = None

    if ctx.task_id is not None:
        row = await node.org.tasks.get(ctx.task_id)
        if row is not None:
            task = {
                "task_id": str(row.id),
                "title": row.title,
                "objective": row.objective,
                "acceptance_criteria": row.acceptance_criteria,
                "input": row.input,
                "attempt": row.rework_count,
                "schema_failures": row.schema_failures,
                "last_schema_errors": row.last_schema_errors,
            }
            upstream_id = row.input.get("source_task_id") or state.get("input", {}).get(
                "source_task_id"
            )
            if upstream_id:
                upstream = await node.org.tasks.get(TaskId(_uuid(str(upstream_id))))
                if upstream is not None and upstream.output_artifact_id is not None:
                    source_ref = ArtifactRefView(
                        artifact_id=str(upstream.output_artifact_id),
                        sha256="",
                        size_bytes=0,
                        kind=f"task_output:{upstream.output_schema_ref}",
                        summary=summarise(upstream.output or {}),
                    ).to_json()
                elif upstream is not None:
                    # The upstream task exists but produced nothing. Worth a log
                    # line: this is what a dependency that was rejected looks like
                    # from downstream, and it is otherwise silent.
                    log.warning(
                        "content.upstream_empty",
                        upstream_task=str(upstream.id),
                        upstream_status=upstream.status.value,
                        upstream_outcome=upstream.outcome.value if upstream.outcome else None,
                    )
    return {"task": task, "source_ref": source_ref}


async def _draft(state: ContentState, config: RunnableConfig) -> dict[str, Any]:
    node = config["configurable"][GRAPH_KEY]
    ctx = node.ctx
    org_id = OrganizationId(ctx.organization_id)
    task = state.get("task") or {}

    research: dict[str, Any] | None = None
    ref = state.get("source_ref")
    declared = task.get("input", {}).get("source_task_id")
    if ref is not None and declared:
        row = await node.org.tasks.get(TaskId(_uuid(str(declared))))
        # Read the task's stored output rather than the artifact bytes: they are
        # the same JSON, and the row is one query instead of an object-store round
        # trip plus a digest verification.
        research = row.output if row is not None else None

    if declared and research is None:
        # The task names a dependency and the dependency produced nothing. Stop
        # here rather than prompting for a draft that cannot be both honest and
        # valid — see `UpstreamNotReady`. The `research_block` fallback below is
        # for a task that never declared a source at all, which is a different
        # situation: nobody promised that one any evidence.
        raise UpstreamNotReady(
            f"task {task.get('task_id')} declares source_task_id={declared}, which has "
            "produced no output; refusing to draft an unsourced piece against a schema "
            "that requires sources"
        )

    session = await node.org.sessions.open(org_id, CONTENT)
    messages = await node.org.inbox.recent_for(org_id, CONTENT)

    prior_errors = task.get("last_schema_errors")
    # `attempt` is `rework_count`, which only a *manager* bounce increments. A
    # schema failure increments `schema_failures` instead, so gating on `attempt`
    # alone hid the correction from the one case it was written for.
    correction = (
        "\n\nYour previous submission was rejected by the schema validator:\n"
        + summarise(prior_errors, 800)
        if prior_errors and (task.get("attempt") or task.get("schema_failures"))
        else ""
    )
    research_block = (
        "## Research you are writing from\n```json\n" + summarise(research, 12_000) + "\n```"
        if research
        else (
            "## Research\n\nNone was attached to this task. Say so in the draft "
            "rather than inventing sources — a piece that admits it is unsourced is "
            "recoverable, one that fabricates citations is not."
        )
    )

    # M3. What the department already knows about this audience and channel — house
    # style, what landed last time, which claims were cut in review. Not the research:
    # that is attached to the task and is right there in `research_block`.
    plan = await node.recall(
        "draft",
        " ".join(
            str(x)
            for x in (
                task.get("title"),
                task.get("objective"),
                task.get("input", {}).get("channel", "blog"),
                task.get("input", {}).get("audience"),
            )
            if x
        )[:1_000],
        call_site="content.draft",
    )

    context = assemble(
        system_prompt=SYSTEM,
        spec=ctx.spec,
        task_input={
            "title": task.get("title"),
            "objective": task.get("objective"),
            "acceptance_criteria": task.get("acceptance_criteria", []),
            "channel": task.get("input", {}).get("channel", "blog"),
            "audience": task.get("input", {}).get("audience"),
            "as_of": dt.datetime.now(dt.UTC).date().isoformat(),
        },
        session_summary=session.summary,
        recent_messages=messages,
        artifacts=[ArtifactRefView.from_json(ref)] if ref else None,
        instruction=f"{INSTRUCTION}{correction}\n\n{research_block}",
        memory=plan.block,
    )

    try:
        with ctx.node("draft"):
            result = await call_structured(
                ctx,
                node.models,
                context,
                CONTENT_DRAFT_V1,
                work_class=WorkClass.WORK,
                call_site="content.draft",
                max_output_tokens=16_000,
            )
    except OutputSchemaViolation as exc:
        await record_schema_failure(node, ctx, exc, manager_name=HEAD)
        raise
    return {"draft": result.value.model_dump(mode="json")}


async def _submit(state: ContentState, config: RunnableConfig) -> dict[str, Any]:
    node = config["configurable"][GRAPH_KEY]
    ctx = node.ctx
    draft = state.get("draft")
    if draft is None or ctx.task_id is None:
        return {"output": {"draft": draft, "submitted": False, "task_id": None}}

    claimed = await node.org.tasks.claim(ctx.task_id, ctx.worker_id)
    if claimed is None:
        # Fail, do not shrug. This branch used to return `submitted: False` and let
        # the run end SUCCESS, so a finished draft that could not be stored looked
        # exactly like a delivered one — the department reported a good week and the
        # task sat there with a NULL output.
        log.warning("content.task_not_claimable", task_id=str(ctx.task_id))
        raise TaskStateError(
            f"task {ctx.task_id} could not be claimed for submission; the draft this "
            "run produced has nowhere to go"
        )

    outcome = await node.org.tasks.submit(
        ctx.task_id,
        worker_id=ctx.worker_id,
        result=draft,
        organization_id=OrganizationId(ctx.organization_id),
        run_id=ctx.run_id,
        manager_name=HEAD,
    )
    return {
        "output": {
            "task_id": str(ctx.task_id),
            "submitted": outcome.ok,
            "closed": outcome.closed,
            "schema_failures": outcome.schema_failures,
            "errors": outcome.errors[:5],
            "artifact_id": str(outcome.artifact_id) if outcome.artifact_id else None,
        }
    }


async def _summarize(state: ContentState, config: RunnableConfig) -> dict[str, Any]:
    node = config["configurable"][GRAPH_KEY]
    ctx = node.ctx
    org_id = OrganizationId(ctx.organization_id)
    session = await node.org.sessions.open(org_id, CONTENT)
    messages = await node.org.inbox.recent_for(org_id, CONTENT, limit=40)
    outcome = await maybe_summarize(
        ctx,
        node.models,
        node.org.sessions,
        session,
        messages,
        previous_summary=session.summary,
    )
    output = dict(state.get("output", {}))
    output["summary"] = outcome
    return {"output": output}


def _uuid(value: str) -> Any:
    import uuid

    return uuid.UUID(value)


def build() -> StateGraph[ContentState, Any, Any, Any]:
    graph: StateGraph[ContentState, Any, Any, Any] = StateGraph(ContentState)
    graph.add_node("load", _load)
    graph.add_node("draft", _draft)
    graph.add_node("submit", _submit)
    graph.add_node("summarize", _summarize)
    graph.add_edge(START, "load")
    graph.add_edge("load", "draft")
    graph.add_edge("draft", "submit")
    graph.add_edge("submit", "summarize")
    graph.add_edge("summarize", END)
    return graph


register_graph("content@1", build)
