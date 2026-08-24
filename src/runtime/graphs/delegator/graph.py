"""`delegator@1` — the reference shape of a parent that decomposes work.

    START → plan_children → fan_out → summarise → END

It makes no model call. That is deliberate and it is the same argument
`marketing_head`'s publish gate makes: M5b has to measure whether delegation pays for
itself, and a reference parent that spent money on planning would put that cost inside
the thing being measured. What a real parent would do with a model — decide *which*
children to spawn — this graph takes from its input, so everything it costs is
delegation machinery and nothing else.

The three details worth copying into a real parent are all in `_fan_out`:

**Exhaustion is checked between children, not discovered as a refusal.** `drain` is
the decision *not to attempt*, and a policy that learned the subtree was full by
catching `SubtreeBudgetExceeded` would have already paid for the attempt — and would
have no way to express `drain` at all, because an exception arrives once per attempt.

**A refused child does not fail the parent.** §9 risk 1 is that delegation multiplies
existing problems; a parent that died because one child was refused would multiply this
one on the spot. Every refusal is caught, recorded in the output, and the parent carries
on with what it has.

**The ordinal comes from the node scope.** `node.delegate` handles that, and the loop
uses `ctx.node("fan_out", iteration=i)` so a replayed iteration re-derives the same
idempotency key — which is what makes the spawn replay-safe rather than merely
idempotent-looking. T67.
"""

from __future__ import annotations

import asyncio
from typing import Annotated, Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from runtime.domain.delegation import ChildContext, TaskSpec
from runtime.domain.enums import ExhaustionPolicy
from runtime.domain.errors import DelegationRefused
from runtime.graphs.registry import GRAPH_KEY, register_graph
from runtime.observability.logging import get_logger

log = get_logger("graphs.delegator")


def _last(_current: Any, incoming: Any) -> Any:
    return incoming


class DelegatorState(TypedDict, total=False):
    input: Annotated[dict[str, Any], _last]
    children: Annotated[list[dict[str, Any]], _last]
    drained: Annotated[str, _last]
    output: Annotated[dict[str, Any], _last]


def _requested(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """The children this run was asked to spawn.

    A list of `{"actor": ..., "task": {...}, "facts": [...]}`. Malformed entries are
    dropped with a log line rather than raising: the input to a parent is frequently a
    model's plan, and a plan with one unusable entry should cost one child rather than
    the whole run.
    """
    out: list[dict[str, Any]] = []
    for entry in payload.get("delegate_to") or []:
        if isinstance(entry, dict) and entry.get("actor"):
            out.append(entry)
        else:
            log.warning("delegator.unusable_child_request", entry=str(entry)[:200])
    return out


async def _plan_children(state: DelegatorState, config: RunnableConfig) -> dict[str, Any]:
    node = config["configurable"][GRAPH_KEY]
    payload = state.get("input", {}) or dict(node.ctx.spec.input)
    return {"input": payload}


async def _fan_out(state: DelegatorState, config: RunnableConfig) -> dict[str, Any]:
    node = config["configurable"][GRAPH_KEY]
    payload = state.get("input", {})
    if payload.get("concurrent"):
        return await _fan_out_concurrent(node, payload)
    return await _fan_out_sequential(node, payload)


async def _fan_out_concurrent(node: Any, payload: dict[str, Any]) -> dict[str, Any]:
    """All children at once. The shape `max_children` and `max_live_descendants` govern.

    Sequential fan-out is the safe default — see `_fan_out_sequential` — but it is also
    the shape in which the *live* fan-out limits can never bind, because there is never
    more than one live child. A parent that genuinely decomposes work wants them
    running together, and that is the parent those limits exist for.

    **The ordinal is the loop index, explicitly.** In the sequential path it comes from
    the node scope, which is correct because the calls happen in order. Here they do
    not: `gather` starts the coroutines in creation order today and that is an
    implementation detail of the event loop, not a guarantee — so an ordinal drawn from
    a shared counter would be assigned in whatever order the loop happened to schedule,
    and a replay that scheduled differently would derive different idempotency keys and
    spawn a second set of children. Passing `i` makes the key a function of the *plan*
    rather than of the scheduler.
    """
    ctx = node.ctx
    entries = _requested(payload)

    # Exhaustion is checked once, before the fan-out, rather than between children:
    # there is no "between" here. A concurrent parent that wants `drain` semantics
    # per child is a parent that should be spawning them in batches.
    if node.delegation is not None:
        state_now = await node.delegation.enforce_exhaustion(ctx)
        if state_now.exhausted:
            return {"children": [], "drained": state_now.reason}

    async def one(index: int, entry: dict[str, Any]) -> dict[str, Any]:
        target = str(entry["actor"])
        with ctx.node("fan_out", iteration=index):
            try:
                outcome = await node.delegate(target, _child_context(ctx, entry), ordinal=index)
            except DelegationRefused as exc:
                return {
                    "actor": target,
                    "spawned": False,
                    "refused": type(exc).__name__,
                    "detail": str(exc)[:300],
                }
        return _spawned(target, outcome)

    results = await asyncio.gather(*(one(i, e) for i, e in enumerate(entries)))
    return {"children": list(results), "drained": ""}


async def _fan_out_sequential(node: Any, payload: dict[str, Any]) -> dict[str, Any]:
    ctx = node.ctx
    results: list[dict[str, Any]] = []
    drained = ""

    for i, entry in enumerate(_requested(payload)):
        # Between children, never inside the attempt. See the module docstring.
        if node.delegation is not None:
            state_now = await node.delegation.enforce_exhaustion(ctx)
            if state_now.exhausted:
                drained = state_now.reason
                policy = ctx.spec.delegation_limits.exhaustion_policy
                log.info(
                    "delegator.stopped",
                    policy=policy.value,
                    reason=state_now.reason,
                    spawned=len(results),
                    **ctx.log_fields(),
                )
                break

        target = str(entry["actor"])
        with ctx.node("fan_out", iteration=i):
            try:
                outcome = await node.delegate(target, _child_context(ctx, entry), ordinal=i)
            except DelegationRefused as exc:
                results.append(
                    {
                        "actor": target,
                        "spawned": False,
                        "refused": type(exc).__name__,
                        "detail": str(exc)[:300],
                    }
                )
                continue
        results.append(_spawned(target, outcome))

    return {"children": results, "drained": drained}


def _child_context(ctx: Any, entry: dict[str, Any]) -> ChildContext:
    """The whole of what crosses the boundary, in one place.

    Shared by both fan-out shapes so there is one answer to "what does a child get",
    and so the line that a well-meaning change would otherwise turn into
    `facts=ctx.spec.input` is a line somebody has to find rather than one of two.
    """
    return ChildContext(
        task=TaskSpec(
            input=dict(entry.get("task") or {}),
            output_schema_ref=entry.get("output_schema_ref"),
            title=str(entry.get("title", "")),
            objective=str(entry.get("objective", "")),
        ),
        # Explicit sentences the parent extracted — never a transcript.
        facts=tuple(str(f) for f in (entry.get("facts") or [])),
        memory_scopes=ctx.spec.memory_scopes or (),
        budget_headroom_cents=int(entry.get("budget_headroom_cents", 0)),
        deadline_s=entry.get("deadline_s"),
    )


def _spawned(target: str, outcome: Any) -> dict[str, Any]:
    return {
        "actor": target,
        "spawned": True,
        "child_run_id": str(outcome.child_run_id),
        "status": outcome.status,
        "output": outcome.output,
        "cost_cents": outcome.cost_cents,
        "reused": outcome.reused,
    }


async def _summarise(state: DelegatorState, config: RunnableConfig) -> dict[str, Any]:
    """Report what came back, including what did not.

    No model call: this is a `dict`, and a parent that paid to render one would make
    the coordination ratio worse for no information. A real parent that wants prose
    writes it with a `SUMMARIZATION` call and is billed for it — visibly, which is the
    point of the work classes.
    """
    node = config["configurable"][GRAPH_KEY]
    children = state.get("children", [])
    drained = state.get("drained", "")
    accepted = [c for c in children if c.get("status") == "SUCCESS"]
    policy = node.ctx.spec.delegation_limits.exhaustion_policy

    return {
        "output": {
            "children": children,
            "spawned": sum(1 for c in children if c.get("spawned")),
            "refused": sum(1 for c in children if not c.get("spawned")),
            "succeeded": len(accepted),
            "subtree_cost_cents": sum(int(c.get("cost_cents", 0)) for c in children),
            # Named rather than implied. A parent that stopped early because the tree
            # ran out of money and a parent that had nothing more to do produce the
            # same child list, and only one of them is a signal.
            "drained": drained or None,
            "exhaustion_policy": policy.value,
            "limit_reached": bool(drained) and policy is ExhaustionPolicy.STRICT,
        }
    }


def build() -> StateGraph[DelegatorState, Any, Any, Any]:
    graph: StateGraph[DelegatorState, Any, Any, Any] = StateGraph(DelegatorState)
    graph.add_node("plan_children", _plan_children)
    graph.add_node("fan_out", _fan_out)
    graph.add_node("summarise", _summarise)
    graph.add_edge(START, "plan_children")
    graph.add_edge("plan_children", "fan_out")
    graph.add_edge("fan_out", "summarise")
    graph.add_edge("summarise", END)
    return graph


register_graph("delegator@1", build)
