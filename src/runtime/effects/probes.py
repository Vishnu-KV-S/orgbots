"""Probe registry.

A `probe`-policy tool declares `marker_search_fn` as a *name*, not a callable, so
the declaration can be validated at registration and stored in the journal row
without pickling anything. The function it names answers one question: "does the
provider already hold a record stamped with this marker?"

A probe returns `None` for "definitely not there" and a dict for "here it is".
There is no third return value — a probe that cannot tell must raise, so the
caller marks the effect ORPHANED instead of guessing.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from runtime.domain.errors import SpecError

ProbeFn = Callable[[str], Awaitable[dict[str, object] | None]]

_PROBES: dict[str, ProbeFn] = {}


def register_probe(name: str, fn: ProbeFn) -> None:
    """Register (or re-register) a probe by name.

    Re-registration replaces. It has to: a probe that needs database access is a
    closure over a unit-of-work factory, so building a second worker in the same
    process legitimately produces a second, equivalent function object under the
    same name. Rejecting that would make "two workers in one process" impossible
    for no safety gain — a genuine name collision between two different tools is a
    naming problem that `marker_search_fn` validation at registration already
    surfaces.
    """
    _PROBES[name] = fn


def get_probe(name: str) -> ProbeFn:
    try:
        return _PROBES[name]
    except KeyError as exc:
        raise SpecError(
            f"probe {name!r} is not registered; a probe policy that names a "
            "non-existent search function is worse than no probe at all"
        ) from exc


def probe_exists(name: str) -> bool:
    return name in _PROBES


def clear_probes() -> None:
    """Test hook."""
    _PROBES.clear()
