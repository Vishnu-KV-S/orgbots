"""Graph registry.

Graphs are looked up by the `graph_ref` frozen into a RunSpec — `"echo_agent@1"`,
not a module path. The version is part of the key so that changing a graph's shape
means publishing a new ref rather than silently altering what an in-flight run
resumes into.

A node never receives a live gateway from module scope; it reads it out of the
LangGraph config. That keeps graphs free of imports the `gateway-only` contract
forbids, and it means a graph can be unit-tested against a fake gateway without
patching anything global.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from runtime.domain.errors import SpecError

GRAPH_KEY = "runtime_ctx"
"""Key under `config["configurable"]` holding the per-run node context."""


class GraphBuilder(Protocol):
    def __call__(self) -> Any:
        """Return an uncompiled `StateGraph`."""


_GRAPHS: dict[str, Callable[[], Any]] = {}
_MODES: dict[str, tuple[str, ...]] = {}


def register_graph(ref: str, builder: Callable[[], Any], *, modes: tuple[str, ...] = ()) -> None:
    """Register a graph, and optionally say which entry points it dispatches on.

    `modes` is the list a caller may put in `input["mode"]` and expect the graph to
    route on — **read off the graph's own branch function, never off a schedule**. The
    two disagree: `runtime.org.department.MODES` is derived from `TRIGGERS`, so it
    contains `weekly_metrics` (which `marketing_head@1` does not dispatch on) and omits
    `task.submitted` (which it does). Seeding from it would offer an operator a dead
    option and hide a live one.

    Empty means *unknown*, not *none*. A caller that needs to show the choices falls
    back to free text rather than pretending a graph has no entry points.
    """
    if ref in _GRAPHS:
        raise SpecError(f"graph {ref!r} is already registered")
    _GRAPHS[ref] = builder
    _MODES[ref] = tuple(modes)


def get_graph(ref: str) -> Callable[[], Any]:
    try:
        return _GRAPHS[ref]
    except KeyError as exc:
        raise SpecError(f"no graph registered as {ref!r}; known: {sorted(_GRAPHS)}") from exc


def known_graphs() -> frozenset[str]:
    return frozenset(_GRAPHS)


def modes_for(ref: str) -> tuple[str, ...]:
    """The entry points this graph routes on, or `()` if it did not declare any."""
    return _MODES.get(ref, ())
