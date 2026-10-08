"""The tools the runtime ships.

M0 shipped two, chosen to cover the two halves of the recovery story: one read-only
tool whose replay is harmless, and one that mutates state so that "exactly once" has
something to be true about.

M1 adds two more and they are the two that matter to the gate:

- `web.search@1` — the first tool with a **price**. Until M1, `estimate_tool_cents`
  returned zero for everything and the reservation path was a seam rather than a
  number.
- `publish.external@1` — the first tool with a **consequence**. IRREVERSIBLE,
  approval-gated, pointed at staging by default.
"""

from __future__ import annotations

from runtime.domain.enums import BlastRadius
from runtime.gateway.builtin import (
    browser,
    fixture_sideeffect,
    publish_external,
    web_fetch,
    web_search,
)
from runtime.gateway.tools import ToolRegistry
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.settings import Settings, get_settings

_STRICTEST = {BlastRadius.READ: 0, BlastRadius.REVERSIBLE: 1, BlastRadius.IRREVERSIBLE: 2}


def build_registry(
    uow_factory: UnitOfWorkFactory, settings: Settings | None = None
) -> ToolRegistry:
    resolved = settings or get_settings()
    registry = ToolRegistry()
    web_fetch.register(registry)
    fixture_sideeffect.register(registry, uow_factory)
    web_search.register(registry, resolved)
    publish_external.register(registry, resolved)
    browser.register(registry, resolved)
    return registry


def action_floors(registry: ToolRegistry) -> dict[str, BlastRadius]:
    """authority action → the **widest** blast radius of any tool performing it.

    M2 §3's last resolution step, as data. Two tools can perform one action —
    `publish.external@1` and a future `publish.social@1` both resolve
    `publish_external` — and the floor has to be the strictest of them, or adding a
    tool would be a way to loosen a gate that another tool is held to.

    Built from the registry rather than stored in a table because the registry is the
    authority on what a tool does. A policy row claiming an action is `read` would
    otherwise be able to contradict the tool that performs it.
    """
    floors: dict[str, BlastRadius] = {}
    for name in sorted(registry.names()):
        tool = registry.get(name)
        action = tool.definition.authority_action
        if action is None:
            continue
        current = floors.get(action)
        if current is None or _STRICTEST[tool.blast_radius] > _STRICTEST[current]:
            floors[action] = tool.blast_radius
    return floors


def default_action_floors(settings: Settings | None = None) -> dict[str, BlastRadius]:
    """The floors for the tools this runtime ships. Used by `RunService`'s default."""
    from runtime.persistence.uow import UnitOfWorkFactory as _Factory

    # The registry needs a factory for the fixture tool's DB access; nothing here
    # calls a tool, so an unconnected factory is enough and avoids requiring the
    # caller to have one on hand just to ask what the floors are.
    return action_floors(build_registry(_Factory(settings), settings))


__all__ = [
    "action_floors",
    "browser",
    "build_registry",
    "default_action_floors",
    "fixture_sideeffect",
    "publish_external",
    "web_fetch",
    "web_search",
]
