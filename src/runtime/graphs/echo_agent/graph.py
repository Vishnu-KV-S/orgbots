"""`echo_agent@1` — the trivial graph M0 uses to prove the machinery.

Two nodes. `effect` makes at most one tool call; `respond` shapes the output. It
is deliberately boring, because the thing under test is not the graph — it is what
happens when the process dies in the middle of one.

The one non-obvious detail is `ctx.node("effect")`. Opening a node scope resets the
call ordinal, so a replayed node re-issues ordinal 0 and `logical_call_id`
reproduces exactly. Forgetting that `with` is the difference between exactly-once
and a fresh journal key on every replay, which is why the scope is a context
manager rather than a convention.
"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from runtime.gateway.tools import ToolCall
from runtime.graphs.registry import GRAPH_KEY, register_graph


def _last(_current: Any, incoming: Any) -> Any:
    return incoming


class EchoState(TypedDict, total=False):
    input: Annotated[dict[str, Any], _last]
    effect: Annotated[dict[str, Any] | None, _last]
    output: Annotated[dict[str, Any], _last]


async def _effect_node(state: EchoState, config: RunnableConfig) -> dict[str, Any]:
    node_ctx = config["configurable"][GRAPH_KEY]
    gateway = node_ctx.gateway
    ctx = node_ctx.ctx
    payload = state.get("input", {})

    call: ToolCall | None = None
    if "url" in payload:
        call = ToolCall(tool="web.fetch@1", args={"url": payload["url"]})
    elif "sideeffect" in payload:
        call = ToolCall(
            tool="fixture.sideeffect@1",
            args={
                "payload": payload.get("sideeffect") or {},
                "delay_ms": int(payload.get("delay_ms", 0)),
            },
        )

    if call is None:
        return {"effect": None}

    with ctx.node("effect"):
        result = await gateway.execute(ctx, call)

    return {
        "effect": {
            "tool": result.tool,
            "value": result.value,
            "replayed": result.replayed,
            "logical_call_id": result.logical_call_id,
            "trust": result.trust.value,
        }
    }


async def _respond_node(state: EchoState, config: RunnableConfig) -> dict[str, Any]:
    _ = config
    payload = state.get("input", {})
    effect = state.get("effect")
    return {
        "output": {
            "echo": payload.get("message"),
            "effect": effect,
        }
    }


def build() -> StateGraph[EchoState, Any, Any, Any]:
    graph: StateGraph[EchoState, Any, Any, Any] = StateGraph(EchoState)
    graph.add_node("effect", _effect_node)
    graph.add_node("respond", _respond_node)
    graph.add_edge(START, "effect")
    graph.add_edge("effect", "respond")
    graph.add_edge("respond", END)
    return graph


# No `modes`, deliberately: this graph has no branch and echoes whatever it is
# given, so any list would be an invention. A caller that wants to offer choices
# gets `()` and falls back to free-form input, which is the truth here.
register_graph("echo_agent@1", build)
