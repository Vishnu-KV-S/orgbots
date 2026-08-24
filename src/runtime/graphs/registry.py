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


def register_graph(ref: str, builder: Callable[[], Any]) -> None:
    if ref in _GRAPHS:
        raise SpecError(f"graph {ref!r} is already registered")
    _GRAPHS[ref] = builder


def get_graph(ref: str) -> Callable[[], Any]:
    try:
        return _GRAPHS[ref]
    except KeyError as exc:
        raise SpecError(f"no graph registered as {ref!r}; known: {sorted(_GRAPHS)}") from exc


def known_graphs() -> frozenset[str]:
    return frozenset(_GRAPHS)
