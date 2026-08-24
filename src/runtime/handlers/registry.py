"""Handler registry — deterministic workers.

A deterministic worker is a plain async function, not a graph. It gets the same
`RunContext` and the same gateways, and it is bound by the same ceilings — the
difference is that its ceiling for model calls is zero, so `ModelGateway` refuses
every call it might make.

That refusal is the point. "This worker does not call an LLM" enforced by the
gateway is a property of the system; the same claim enforced by reading the
handler's source is a property of whoever last read it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from runtime.domain.context import RunContext
from runtime.domain.errors import SpecError


class HandlerContext(Protocol):
    """What a handler is handed. Mirrors the graph node context.

    `org` and `artifacts` arrive by the same route as the gateways — through the
    executor, never by import — so a handler can be exercised against fakes and a
    deterministic worker still has no path to a client of its own.
    """

    ctx: RunContext
    gateway: Any
    models: Any
    org: Any
    artifacts: Any


HandlerFn = Callable[[HandlerContext, dict[str, Any]], Awaitable[dict[str, Any]]]

_HANDLERS: dict[str, HandlerFn] = {}
_MODES: dict[str, tuple[str, ...]] = {}


def register_handler(ref: str, fn: HandlerFn, *, modes: tuple[str, ...] = ()) -> None:
    """Register a handler, and optionally the entry points it branches on.

    Same contract as `register_graph`'s `modes`, and the same warning: read them off
    the handler, not off the schedule that happens to call it.

    Note the asymmetry with the graph registry, which is deliberate and older than
    this parameter: re-registering the *same* function is fine here, and any
    re-registration is an error there. A handler module is imported from several
    entry points and a graph builder is not.
    """
    if ref in _HANDLERS and _HANDLERS[ref] is not fn:
        raise SpecError(f"handler {ref!r} is already registered")
    _HANDLERS[ref] = fn
    _MODES[ref] = tuple(modes)


def get_handler(ref: str) -> HandlerFn:
    try:
        return _HANDLERS[ref]
    except KeyError as exc:
        raise SpecError(f"no handler registered as {ref!r}; known: {sorted(_HANDLERS)}") from exc


def known_handlers() -> frozenset[str]:
    return frozenset(_HANDLERS)


def modes_for(ref: str) -> tuple[str, ...]:
    """The entry points this handler branches on, or `()` if it did not declare any."""
    return _MODES.get(ref, ())
