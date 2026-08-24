"""`research@1` — search, fetch, synthesise → `CompetitorReport@1`. WORK class.

    START → load → search → fetch → synthesize → submit → summarize → END

The graph is linear and boring on purpose. What is not boring is where the bytes go.

**Evidence lives in an artifact, not in state.** `fetch` pulls up to eight pages,
writes all of them into a single `research_evidence` artifact, and puts an
`ArtifactRefView` in state. `synthesize` loads it back inside the node. The M0 retro
measured why: LangGraph checkpoints the whole state at every superstep, so eight
pages carried in state would be re-serialised at every node, on every replay,
forever — for bytes already sitting in the object store with a digest on them.
`test_m1_state_discipline.py` asserts the checkpoints stay under 8 KB.

**The loops pass `iteration=`.** `web.search@1` and `web.fetch@1` are called in a
loop, and two iterations of one node that share a `checkpoint_ns` collide on one
`logical_call_id` — the second silently receives the first one's result. The M0
retro flagged this as the footgun M1 would hit in week one and added
`ctx.node(..., iteration=)` so the loop body has nothing to get wrong.

**A failed fetch is data, not an exception.** A 404 on one of eight sources is
normal; failing the run over it would turn a partial report into no report. The
failures are collected and handed to `synthesize`, which is required to declare them
in the report's `gaps` field — so "we could not establish pricing" has somewhere to
go other than a fabricated pricing note.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Annotated, Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from runtime.domain.enums import WorkClass
from runtime.domain.errors import (
    OutputSchemaViolation,
    SpecError,
    TaskStateError,
    TransientFault,
)
from runtime.domain.hashing import canonical_json
from runtime.domain.ids import OrganizationId, TaskId
from runtime.domain.outputs import COMPETITOR_REPORT_V1
from runtime.gateway.tools import ToolCall
from runtime.graphs.common.context import assemble
from runtime.graphs.common.state import ArtifactRefView, last, summarise
from runtime.graphs.common.structured import call_structured, record_schema_failure
from runtime.graphs.common.summarize import maybe_summarize
from runtime.graphs.registry import GRAPH_KEY, register_graph
from runtime.observability.logging import get_logger
from runtime.org.department import HEAD, RESEARCH

log = get_logger("graphs.research")

MAX_QUERIES = 3
MAX_FETCHES = 8
MAX_PAGE_CHARS = 12_000
"""Per-page cap on what goes into the evidence artifact. Eight pages at 12 KB is
~96 KB of evidence, which is a comfortable prompt and an unremarkable artifact."""

SYSTEM = """You are a competitive researcher in a small marketing department.

You produce evidence-backed reports. Three rules, in order of importance:

1. Every claim you make is traceable to a source you actually read. You will be
   given the text of the pages that were fetched; if something is not in them, you
   do not know it.
2. What you could not establish goes in `gaps`. An honest gap is worth more than a
   confident guess, and a fabricated detail is the single most expensive mistake
   available to you — it looks like work and it survives review.
3. Recommendations are for a marketing team deciding what to say next week. "Monitor
   the competitive landscape" is not a recommendation. "Lead with X because none of
   the four competitors mention it" is."""

SEARCH_SYSTEM = """You are a competitive researcher in a small marketing department.

You produce evidence-backed reports. Three rules, in order of importance:

1. Every claim you make is traceable to a source you actually retrieved. Nobody has
   fetched pages for you on this deployment — you have your own web search and it is
   the only evidence you will get. Search before you write, and if you did not read
   it, you do not know it.
2. What you could not establish goes in `gaps`. An honest gap is worth more than a
   confident guess, and a fabricated detail is the single most expensive mistake
   available to you — it looks like work and it survives review.
3. Recommendations are for a marketing team deciding what to say next week. "Monitor
   the competitive landscape" is not a recommendation. "Lead with X because none of
   the four competitors mention it" is."""
"""`SYSTEM` with rule 1 pointed at the retrieval route that actually exists here.

A second constant rather than a formatted one: rule 1 is what the report's honesty
rests on, and a rule about not inventing sources that reads differently depending on a
boolean is worth avoiding. The duplication costs an edit in two places if rules 2 and 3
change.

Without it, `SYSTEM` and the situation contradict each other — it promises fetched
pages that are not there — and a model *following* rule 1 correctly answers in prose
that it cannot proceed. That reaches `call_structured` as `not_json`, twice, and
surfaces as a schema failure rather than as the prompt bug it is."""

INSTRUCTION = """Write the competitor report now.

Use only the evidence below. Cite by index into `sources` — the indices must point
at sources you list. Name only competitors you actually found evidence for; two is
a real report, eight invented ones is not.

If the evidence is too thin to support two competitors and two themes, say so in
`gaps` and report what you do have. Do not pad."""

SEARCH_INSTRUCTION = """Write the competitor report now.

No pages were fetched for you: `web.search@1` is not configured on this deployment.
You have the provider's own web search instead — use it, and cite only what you
actually retrieve. Put every URL you read into `sources` and cite by index into it.

The rules do not change because the retrieval route did. Name only competitors you
found evidence for, two is a real report and eight invented ones is not, and what you
could not establish goes in `gaps`. Do not pad.

When you have finished searching, answer with the JSON object the system prompt
describes and nothing else — no preamble, no "based on my searches", no prose around
it. A provider that searches server-side answers conversationally by default, and the
schema instruction is thousands of tokens of search results away by the time you
reply, so this is repeated here where it is the last thing you read."""


def _reduce(_current: Any, incoming: Any) -> Any:
    return last(_current, incoming)


def _provider_side_search(ctx: Any) -> bool:
    """Whether this actor's WORK call carries the provider's own search.

    `synthesize` is the WORK call, so a profile with `web_search` set means evidence
    has a second route that does not pass through `web.search@1` — which is the one
    condition under which an unconfigured search *tool* costs coverage rather than
    correctness. The grant is not checked here because it does not need to be: the
    model gateway offers a provider-side tool only to an actor holding
    `web.search@1`, so a profile that asks without the grant gets nothing and this
    returning True would merely produce the empty report it already produces.
    """
    profile = ctx.spec.spec.model_profiles.profiles.get(WorkClass.WORK)
    return bool(profile is not None and profile.web_search)


class ResearchState(TypedDict, total=False):
    input: Annotated[dict[str, Any], _reduce]
    task: Annotated[dict[str, Any] | None, _reduce]
    queries: Annotated[list[str], _reduce]
    hits: Annotated[list[dict[str, str]], _reduce]
    evidence: Annotated[dict[str, Any] | None, _reduce]
    """An `ArtifactRefView`, never the evidence itself."""
    gaps: Annotated[list[str], _reduce]
    report: Annotated[dict[str, Any] | None, _reduce]
    output: Annotated[dict[str, Any], _reduce]


async def _load(state: ResearchState, config: RunnableConfig) -> dict[str, Any]:
    """Read the task and work out what to search for.

    Query construction is deterministic — no model call. §3 lists the free steps and
    this is one of them: turning a task objective into three search strings is a
    rule, and paying COORDINATION-class money for it would put the plumbing in the
    ratio the plumbing is supposed to exonerate.
    """
    node = config["configurable"][GRAPH_KEY]
    ctx = node.ctx
    payload = state.get("input", {})

    task = None
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

    subject = str(
        (task or {}).get("input", {}).get("subject")
        or payload.get("subject")
        or (task or {}).get("title")
        or "the agent infrastructure category"
    )
    explicit = (task or {}).get("input", {}).get("queries") or payload.get("queries")
    queries = (
        [str(q) for q in explicit][:MAX_QUERIES]
        if explicit
        else [
            f"{subject} competitors",
            f"{subject} pricing",
            f"{subject} positioning 2026",
        ][:MAX_QUERIES]
    )
    return {"task": task, "queries": queries, "gaps": []}


async def _search(state: ResearchState, config: RunnableConfig) -> dict[str, Any]:
    node = config["configurable"][GRAPH_KEY]
    ctx = node.ctx
    hits: list[dict[str, str]] = []
    gaps = list(state.get("gaps", []))
    seen: set[str] = set()

    for i, query in enumerate(state.get("queries", [])):
        # `iteration=i` — see the module docstring. Two loop turns sharing a
        # namespace collide on one journal key.
        with ctx.node("search", iteration=i):
            try:
                result = await node.gateway.execute(
                    ctx,
                    ToolCall(tool="web.search@1", args={"query": query, "max_results": 5}),
                )
            except TransientFault as exc:
                gaps.append(f"search for {query!r} was unavailable: {exc}")
                continue
            except SpecError as exc:
                # The tool is not configured on this deployment — no endpoint, or no
                # credential for one. Normally fatal, and deliberately so: a report
                # with no sources is the failure this graph exists to prevent. An
                # actor whose WORK profile carries provider-side search reaches
                # `synthesize` with a live retrieval path, so the missing tool costs
                # the fetched-pages record rather than the evidence itself. Every
                # remaining query would fail identically, hence break.
                if not _provider_side_search(ctx):
                    raise
                gaps.append(
                    f"the web.search@1 tool is not configured ({exc.__class__.__name__}), "
                    "so no pages were fetched into the evidence record; what this report "
                    "cites was retrieved by the model provider's own search."
                )
                break
        for hit in result.value.get("results", []):
            url = hit.get("url", "")
            if url and url not in seen:
                seen.add(url)
                hits.append(
                    {
                        "url": url,
                        "title": hit.get("title", ""),
                        "snippet": hit.get("snippet", ""),
                    }
                )
    if not hits and not _provider_side_search(ctx):
        # Suppressed when the provider searches for us: "no results" would read as a
        # dry search rather than a search that never ran, and the gap above already
        # says which it was.
        gaps.append("No search results were returned for any query.")
    return {"hits": hits[:MAX_FETCHES], "gaps": gaps}


async def _fetch(state: ResearchState, config: RunnableConfig) -> dict[str, Any]:
    """Fetch each hit and write one evidence artifact.

    One artifact rather than one per page: the synthesize node needs all of them
    together, and eight round trips to the object store to reassemble what was
    written as eight pieces is work for nothing.
    """
    node = config["configurable"][GRAPH_KEY]
    ctx = node.ctx
    gaps = list(state.get("gaps", []))
    pages: list[dict[str, Any]] = []

    for i, hit in enumerate(state.get("hits", [])):
        with ctx.node("fetch", iteration=i):
            try:
                result = await node.gateway.execute(
                    ctx,
                    ToolCall(tool="web.fetch@1", args={"url": hit["url"], "timeout_s": 15.0}),
                )
            except Exception as exc:
                gaps.append(f"could not fetch {hit['url']}: {type(exc).__name__}")
                continue
        value = result.value
        if value.get("truncated") or value.get("artifact"):
            # The gateway externalised an oversized body. The excerpt is in the
            # artifact, not here; record the source and move on rather than
            # reaching back for bytes we do not need in full.
            gaps.append(f"{hit['url']} was too large to read in full")
        body = str(value.get("body", ""))[:MAX_PAGE_CHARS]
        if not body.strip():
            gaps.append(f"{hit['url']} returned no readable text")
            continue
        pages.append(
            {
                "url": value.get("url", hit["url"]),
                "title": hit.get("title", ""),
                "status_code": value.get("status_code"),
                "text": body,
                "retrieved_at": dt.datetime.now(dt.UTC).date().isoformat(),
            }
        )

    if not pages:
        return {"evidence": None, "gaps": gaps}

    encoded = canonical_json({"pages": pages}).encode("utf-8")
    ref = await node.artifacts.put(
        encoded,
        organization_id=ctx.organization_id,
        run_id=ctx.run_id,
        kind="research_evidence",
        content_type="application/json",
    )
    await node.artifacts.link(
        ref.artifact_id,
        source_type="run",
        source_id=str(ctx.run_id),
        relation="evidence",
    )
    view = ArtifactRefView(
        artifact_id=str(ref.artifact_id),
        sha256=ref.sha256,
        size_bytes=ref.size_bytes,
        kind="research_evidence",
        summary=summarise([p["url"] for p in pages]),
    )
    log.info(
        "research.evidence",
        pages=len(pages),
        bytes=ref.size_bytes,
        gaps=len(gaps),
        **ctx.log_fields(),
    )
    return {"evidence": view.to_json(), "gaps": gaps}


def _memory_query(task: dict[str, Any], gaps: list[str]) -> str:
    """What this node is about to do, in the actor's own terms.

    The task's subject and objective, not the evidence. The evidence is what the run
    just fetched — retrieving memories similar to it would surface last month's version
    of the same pages, which is the least useful thing memory can offer here. What is
    worth recalling is what the department already concluded about *this subject*, and
    the known gaps, because a gap that recurs is exactly the thing somebody may have
    already resolved.
    """
    parts = [
        str(task.get("title") or ""),
        str(task.get("objective") or ""),
        str((task.get("input") or {}).get("subject") or ""),
        *gaps[:3],
    ]
    return " ".join(p for p in parts if p).strip()[:1_000]


async def _load_session_and_messages(node: Any, ctx: Any) -> tuple[Any, Any]:
    org_id = OrganizationId(ctx.organization_id)
    session = await node.org.sessions.open(org_id, RESEARCH, now=dt.datetime.now(dt.UTC))
    messages = await node.org.inbox.recent_for(org_id, RESEARCH)
    return session, messages


async def _synthesize(state: ResearchState, config: RunnableConfig) -> dict[str, Any]:
    node = config["configurable"][GRAPH_KEY]
    ctx = node.ctx
    task = state.get("task") or {}
    gaps = list(state.get("gaps", []))

    evidence_ref = state.get("evidence")
    pages: list[dict[str, Any]] = []
    if evidence_ref is not None:
        # Loaded here, inside the node, and never returned into state.
        loaded = await ArtifactRefView.from_json(evidence_ref).load(node.artifacts)
        pages = loaded.get("pages", [])

    # M3. Retrieval runs *alongside* the session and inbox reads rather than before
    # them (§7's last line), so the memory round trip disappears into a wait this node
    # was making anyway instead of adding to it. `recall` returns an empty plan when
    # memory is off or in shadow mode, so there is no branch below.
    plan, (session, messages) = await asyncio.gather(
        node.recall(
            "synthesize",
            _memory_query(task, gaps),
            call_site="research.synthesize",
        ),
        _load_session_and_messages(node, ctx),
    )

    # No fetched pages and a WORK profile that searches: the evidence arrives inside
    # the model call rather than before it, so the instruction that says "use only
    # the evidence below" would be pointing at nothing and the honest answer to it is
    # an empty report.
    searching = not pages and _provider_side_search(ctx)
    evidence_block = "\n\n".join(
        f"### Source {i}: {p['title'] or p['url']}\n"
        f"URL: {p['url']}\nRetrieved: {p['retrieved_at']}\n\n{p['text']}"
        for i, p in enumerate(pages)
    )
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

    context = assemble(
        system_prompt=SEARCH_SYSTEM if searching else SYSTEM,
        spec=ctx.spec,
        task_input={
            "title": task.get("title"),
            "objective": task.get("objective"),
            "acceptance_criteria": task.get("acceptance_criteria", []),
            "known_gaps": gaps,
            "as_of": dt.datetime.now(dt.UTC).date().isoformat(),
        },
        session_summary=session.summary,
        recent_messages=messages,
        artifacts=[ArtifactRefView.from_json(evidence_ref)] if evidence_ref else None,
        instruction=(
            f"{SEARCH_INSTRUCTION}{correction}"
            if searching
            else f"{INSTRUCTION}{correction}\n\n## Evidence\n\n{evidence_block}"
        ),
        memory=plan.block,
    )

    try:
        with ctx.node("synthesize"):
            result = await call_structured(
                ctx,
                node.models,
                context,
                COMPETITOR_REPORT_V1,
                work_class=WorkClass.WORK,
                call_site="research.synthesize",
            )
    except OutputSchemaViolation as exc:
        await record_schema_failure(node, ctx, exc, manager_name=HEAD)
        raise
    return {"report": result.value.model_dump(mode="json"), "gaps": gaps}


async def _submit(state: ResearchState, config: RunnableConfig) -> dict[str, Any]:
    node = config["configurable"][GRAPH_KEY]
    ctx = node.ctx
    report = state.get("report")
    if report is None or ctx.task_id is None:
        return {"output": {"report": report, "submitted": False, "task_id": None}}

    task_id: TaskId = ctx.task_id
    claimed = await node.org.tasks.claim(task_id, ctx.worker_id)
    if claimed is None:
        # Fail, do not shrug. This branch used to return `submitted: False` and let
        # the run end SUCCESS: two runs on 2026-08-24 reported success and left no
        # `CompetitorReport` anywhere, having spent ~45k input tokens each. A report
        # that cannot be stored is a failed run, not a quiet one.
        log.warning("research.task_not_claimable", task_id=str(task_id))
        raise TaskStateError(
            f"task {task_id} could not be claimed for submission; the report this run "
            "produced has nowhere to go"
        )

    outcome = await node.org.tasks.submit(
        task_id,
        worker_id=ctx.worker_id,
        result=report,
        organization_id=OrganizationId(ctx.organization_id),
        run_id=ctx.run_id,
        manager_name=HEAD,
    )
    return {
        "output": {
            "task_id": str(task_id),
            "submitted": outcome.ok,
            "closed": outcome.closed,
            "schema_failures": outcome.schema_failures,
            "errors": outcome.errors[:5],
            "artifact_id": str(outcome.artifact_id) if outcome.artifact_id else None,
            "gaps": state.get("gaps", []),
        }
    }


async def _summarize(state: ResearchState, config: RunnableConfig) -> dict[str, Any]:
    node = config["configurable"][GRAPH_KEY]
    ctx = node.ctx
    org_id = OrganizationId(ctx.organization_id)
    session = await node.org.sessions.open(org_id, RESEARCH)
    messages = await node.org.inbox.recent_for(org_id, RESEARCH, limit=40)
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


def build() -> StateGraph[ResearchState, Any, Any, Any]:
    graph: StateGraph[ResearchState, Any, Any, Any] = StateGraph(ResearchState)
    graph.add_node("load", _load)
    graph.add_node("search", _search)
    graph.add_node("fetch", _fetch)
    graph.add_node("synthesize", _synthesize)
    graph.add_node("submit", _submit)
    graph.add_node("summarize", _summarize)
    graph.add_edge(START, "load")
    graph.add_edge("load", "search")
    graph.add_edge("search", "fetch")
    graph.add_edge("fetch", "synthesize")
    graph.add_edge("synthesize", "submit")
    graph.add_edge("submit", "summarize")
    graph.add_edge("summarize", END)
    return graph


# One entry point. The graph is linear — load, search, fetch, synthesize, submit —
# and does an assigned task; it never reads `mode`. `work` is named anyway because it
# is the mode the dispatcher stamps on a `task.assigned` message, so it is the answer
# to "what do I put in `input.mode` to make this actor run".
register_graph("research@1", build, modes=("work",))
