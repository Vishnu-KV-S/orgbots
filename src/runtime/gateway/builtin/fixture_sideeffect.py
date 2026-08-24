"""`fixture.sideeffect@1` — a tool that really does mutate something.

This exists for one reason: T7 needs an effect it can *count*. `web.fetch@1` is
replay-safe, so re-executing it after a crash proves nothing — the interesting
case is a tool where a second execution is a bug, and where that bug leaves
evidence.

So this tool writes a row to `sideeffect_fixture`, keyed on a marker derived from
the logical call ID. Its recovery policy is `probe`: after a crash, the runtime
searches for the marker, finds the row the dead worker wrote, and commits without
firing again. The fixture table's unique index on `marker` means a genuine
double-fire raises rather than quietly appending a second row.

The write deliberately happens on a connection of its own, in its own transaction,
so it behaves like a real external effect rather than like something the caller
can roll back.
"""

from __future__ import annotations

import asyncio
import os
import signal
from typing import Any

from pydantic import BaseModel, Field

from runtime.domain.enums import BlastRadius, RecoveryPolicy
from runtime.effects.probes import register_probe
from runtime.gateway.tools import EffectCapabilities, ToolContext, ToolDef, ToolRegistry
from runtime.persistence.repositories.effects import FixtureRepository
from runtime.persistence.uow import UnitOfWorkFactory

CRASH_AFTER_EFFECT_ENV = "RUNTIME_CHAOS_CRASH_AFTER_EFFECT"
"""Set by the chaos harness. When set, the tool SIGKILLs its own process after the
row is written and before the journal can record the commit — which is precisely
the window T7 exists to test. It is read from the environment rather than passed
as an argument so that the crash point is not part of any call's `args_hash`."""


class SideEffectArgs(BaseModel):
    payload: dict[str, Any] = Field(default_factory=dict)
    delay_ms: int = Field(default=0, ge=0, le=5000)


class SideEffectResult(BaseModel):
    marker: str
    wrote: bool
    """False when the marker was already present — i.e. this is a probe hit that
    reached the tool anyway. Should not happen; recorded rather than hidden."""


def build(uow_factory: UnitOfWorkFactory) -> tuple[ToolDef, Any]:
    """Bind the tool to a unit-of-work factory.

    The tool needs database access to write its row, but `ToolContext` deliberately
    does not carry a session — a tool that could reach `runs` could defeat the
    fence. Closing over a factory here gives this one tool exactly the access it
    needs and no more.
    """

    async def probe_marker(marker: str) -> dict[str, object] | None:
        async with uow_factory() as uow:
            return await FixtureRepository(uow.session).find_by_marker(marker)

    async def side_effect(ctx: ToolContext, args: Any) -> SideEffectResult:
        typed: SideEffectArgs = args
        if typed.delay_ms:
            await asyncio.sleep(typed.delay_ms / 1000)

        async with uow_factory.transaction() as uow:
            await FixtureRepository(uow.session).write(
                marker=ctx.marker,
                run_id=__import__("uuid").UUID(ctx.run_id),
                logical_call_id=ctx.logical_call_id,
                payload=typed.payload,
            )

        if os.environ.get(CRASH_AFTER_EFFECT_ENV):
            # The effect has landed and the journal still says INTENT. Die here.
            os.kill(os.getpid(), signal.SIGKILL)

        return SideEffectResult(marker=ctx.marker, wrote=True)

    register_probe("fixture.sideeffect.probe", probe_marker)

    definition = ToolDef(
        name="fixture.sideeffect",
        version=1,
        args_model=SideEffectArgs,
        result_model=SideEffectResult,
        capabilities=EffectCapabilities(
            mutates_external_state=True,
            searchable_marker=True,
            marker_field="marker",
            marker_search_fn="fixture.sideeffect.probe",
            max_blast_radius=BlastRadius.REVERSIBLE,
        ),
        recovery_policy=RecoveryPolicy.PROBE,
        timeout_s=30.0,
    )
    return definition, side_effect


def register(registry: ToolRegistry, uow_factory: UnitOfWorkFactory) -> None:
    definition, fn = build(uow_factory)
    registry.register(definition, fn)
