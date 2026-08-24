"""M2 §7 — the kill switch. T40, T41.

Two modes and one check apart. The tests are those two sentences:

    drain   refuses new runs and new tool calls; a call already past the journal
            completes and commits. Zero orphan INTENT rows.
    halt    also refuses after the effect has fired, leaving an INTENT row to
            reconcile by hand.

`drain` is the default because most incidents are not the kind where an effect in
flight is worse than an effect you have to reconcile — and `halt` turns every
in-flight call into manual work at the exact moment nobody has attention for it.
"""

from __future__ import annotations

import uuid

import pytest

from runtime.domain.enums import EffectStatus, KillMode, KillScope, RunStatus
from runtime.domain.errors import KillSwitchEngaged
from runtime.domain.ids import OrganizationId
from runtime.domain.specs import StartRunRequest
from runtime.gateway.tools import ToolCall
from runtime.org.killswitch import KillSwitchService
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.runtime.bootstrap import Registrar
from runtime.settings import Settings
from tests.conftest_m2 import (
    RecordingTool,
    build_harness,
    grant_tools,
    make_authority,
    make_ctx,
    new_governed_org,
)

pytestmark = pytest.mark.integration


async def _org(uow_factory: UnitOfWorkFactory) -> OrganizationId:
    """A bare org with the test tool granted to `research`.

    The grant is real rather than skipped: an authority with no `tool_grants` bypasses
    the live permission check entirely, and a kill-switch test that quietly did that
    would stop noticing if the kill switch moved behind it in the pipeline.
    """
    organization_id = OrganizationId(uuid.uuid4())
    await Registrar(uow_factory).ensure_organization(organization_id, "acme")
    await grant_tools(uow_factory, organization_id, "research", "test.noop@1")
    return organization_id


# --- T40: drain -----------------------------------------------------------------------


async def test_t40_drain_refuses_new_calls_and_leaves_no_orphan_intent(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """v3 edge 70. A drained tool refuses *before* the journal, so no INTENT row exists
    to reconcile — which is the whole reason `drain` is the default."""
    organization_id = await _org(uow_factory)
    harness = build_harness(uow_factory, settings)
    ctx = make_ctx(organization_id, authority=make_authority())

    await harness.kill_switches.engage(
        organization_id,
        scope_type=KillScope.TOOL,
        scope_id="test.noop@1",
        mode=KillMode.DRAIN,
        reason="provider incident 42",
    )

    with pytest.raises(KillSwitchEngaged, match="incident 42"):
        await harness.gateway.execute(ctx, ToolCall("test.noop@1", {"value": "x"}))

    assert harness.tool.calls == [], "the tool never ran"
    async with uow_factory() as uow:
        effects = await uow.effects.for_run(ctx.run_id)
    assert effects == [], "zero orphan INTENT rows — nothing to reconcile"


async def test_t40_a_call_already_past_the_journal_completes_under_drain(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """The other half of `drain`: in-flight work finishes.

    The switch is engaged *during* the tool's execution, so the pre-flight check has
    already passed and the post-effect check is the only one left. Under `drain` it
    answers "allowed", because the effect happened and committing the record of it is
    the only way to avoid an orphan.
    """
    organization_id = await _org(uow_factory)
    tool = RecordingTool()
    harness = build_harness(uow_factory, settings, tool=tool, kill_ttl=0.0)

    async def engage_mid_flight() -> None:
        await harness.kill_switches.engage(
            organization_id,
            scope_type=KillScope.TOOL,
            scope_id="test.noop@1",
            mode=KillMode.DRAIN,
            reason="drained mid-flight",
        )

    tool.during_call = engage_mid_flight
    ctx = make_ctx(organization_id, authority=make_authority())
    result = await harness.gateway.execute(ctx, ToolCall("test.noop@1", {"value": "x"}))

    assert result.ok, "an effect that already fired is committed, not abandoned"
    async with uow_factory() as uow:
        effects = await uow.effects.for_run(ctx.run_id)
    assert [e.status for e in effects] == [EffectStatus.COMMITTED]


async def test_t40_drain_refuses_new_runs_at_admission(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """ "Refuses new runs" is half the definition of drain, and it is enforced at the
    only door — `start_run` — rather than by hoping nobody calls it."""
    from runtime.runtime.run_service import RunService

    organization_id = await new_governed_org(uow_factory)
    kill = KillSwitchService(uow_factory, ttl_seconds=0.0)
    service = RunService(uow_factory, settings=settings, kill_switches=kill)

    await kill.engage(
        organization_id,
        scope_type=KillScope.ORG,
        scope_id=None,
        mode=KillMode.DRAIN,
        reason="end of month freeze",
    )

    result = await service.start_run(
        StartRunRequest(
            organization_id=organization_id, actor_name="analytics", idempotency_key="k1"
        )
    )

    assert result.status is RunStatus.LIMIT_REACHED
    assert result.refusal_reason == "KILL_SWITCH"
    async with uow_factory() as uow:
        run = await uow.runs.get(result.run_id)
        events = await uow.outbox.events_for_run(result.run_id)
    assert run is not None and "end of month freeze" in (run.status_reason or "")
    assert [e["topic"] for e in events] == ["run.refused"], (
        "a refused run is announced — silence is how an organization quietly stops "
        "working and nobody notices (edge case 21)"
    )


# --- T41: halt ------------------------------------------------------------------------


async def test_t41_halt_mid_effect_leaves_an_enumerable_intent_row(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """v3 edge 70. `halt` stops at the next check even after the effect has fired.

    The INTENT row is the deliverable: it is the evidence that something happened out
    there which the runtime never recorded a result for, and it has to be findable by
    a human doing reconciliation rather than inferred from a gap.
    """
    organization_id = await _org(uow_factory)
    tool = RecordingTool()
    harness = build_harness(uow_factory, settings, tool=tool, kill_ttl=0.0)

    async def engage_mid_flight() -> None:
        await harness.kill_switches.engage(
            organization_id,
            scope_type=KillScope.TOOL,
            scope_id="test.noop@1",
            mode=KillMode.HALT,
            reason="credential compromised",
        )

    tool.during_call = engage_mid_flight
    ctx = make_ctx(organization_id, authority=make_authority())
    with pytest.raises(KillSwitchEngaged, match="effect already fired"):
        await harness.gateway.execute(ctx, ToolCall("test.noop@1", {"value": "x"}))

    async with uow_factory() as uow:
        effects = await uow.effects.for_run(ctx.run_id)
    assert [e.status for e in effects] == [EffectStatus.INTENT], (
        "the effect fired and was not committed — this row is the reconciliation queue"
    )
    assert effects[0].tool_name == "test.noop"


# --- scoping and caching ---------------------------------------------------------------


async def test_an_org_switch_covers_everything_and_a_tool_switch_does_not(
    uow_factory: UnitOfWorkFactory,
) -> None:
    organization_id = await _org(uow_factory)
    kill = KillSwitchService(uow_factory, ttl_seconds=0.0)

    await kill.engage(
        organization_id,
        scope_type=KillScope.TOOL,
        scope_id="publish.external@1",
        mode=KillMode.DRAIN,
        reason="x",
    )
    assert (await kill.check(organization_id, tool="publish.external@1")).stopped
    assert not (await kill.check(organization_id, tool="web.fetch@1")).stopped

    await kill.engage(
        organization_id,
        scope_type=KillScope.ORG,
        scope_id=None,
        mode=KillMode.DRAIN,
        reason="everything",
    )
    assert (await kill.check(organization_id, tool="web.fetch@1")).stopped


async def test_halt_wins_over_drain_when_both_match(uow_factory: UnitOfWorkFactory) -> None:
    """An operator escalating from drain to halt should not have to disengage the
    first one while an incident is happening."""
    organization_id = await _org(uow_factory)
    kill = KillSwitchService(uow_factory, ttl_seconds=0.0)
    await kill.engage(
        organization_id,
        scope_type=KillScope.ORG,
        scope_id=None,
        mode=KillMode.DRAIN,
        reason="first",
    )
    await kill.engage(
        organization_id,
        scope_type=KillScope.TOOL,
        scope_id="test.noop@1",
        mode=KillMode.HALT,
        reason="second",
    )
    verdict = await kill.check(organization_id, tool="test.noop@1", post_effect=True)
    assert verdict.stopped and verdict.mode is KillMode.HALT


async def test_a_second_switch_for_one_scope_is_refused_rather_than_overwriting(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """Two live switches for one scope in different modes is an ambiguity nobody
    resolves correctly at 3am, and silently *changing* the mode under whoever engaged
    the first one is worse."""
    organization_id = await _org(uow_factory)
    kill = KillSwitchService(uow_factory, ttl_seconds=0.0)
    assert await kill.engage(
        organization_id,
        scope_type=KillScope.ORG,
        scope_id=None,
        mode=KillMode.DRAIN,
        reason="first",
    )
    assert not await kill.engage(
        organization_id,
        scope_type=KillScope.ORG,
        scope_id=None,
        mode=KillMode.HALT,
        reason="second",
    )
    assert (await kill.check(organization_id)).mode is KillMode.DRAIN


async def test_disengaging_keeps_the_row(uow_factory: UnitOfWorkFactory) -> None:
    """An incident review asks "when was it on", and a deleted row answers nothing."""
    organization_id = await _org(uow_factory)
    kill = KillSwitchService(uow_factory, ttl_seconds=0.0)
    await kill.engage(
        organization_id,
        scope_type=KillScope.ORG,
        scope_id=None,
        mode=KillMode.DRAIN,
        reason="incident 9",
    )
    assert await kill.disengage(organization_id, scope_type=KillScope.ORG, scope_id=None)
    assert not (await kill.check(organization_id)).stopped

    from sqlalchemy import text

    async with uow_factory() as uow:
        rows = (
            await uow.session.execute(
                text("SELECT reason, disengaged_at FROM kill_switches WHERE organization_id = :o"),
                {"o": organization_id},
            )
        ).all()
    assert len(rows) == 1
    assert rows[0].reason == "incident 9"
    assert rows[0].disengaged_at is not None


async def test_the_cache_is_bounded_and_invalidated_on_engage(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """Edge case 70: fast enough to matter, slow enough not to hammer Postgres.

    The invalidation on engage is the courtesy — an operator who just pulled the
    switch should not spend ten seconds wondering whether it took. The TTL is the
    guarantee, and it covers a switch engaged by *another* process.
    """
    organization_id = await _org(uow_factory)
    long_cache = KillSwitchService(uow_factory, ttl_seconds=3_600.0)
    other_process = KillSwitchService(uow_factory, ttl_seconds=0.0)

    assert not (await long_cache.check(organization_id)).stopped  # warms the cache

    await other_process.engage(
        organization_id,
        scope_type=KillScope.ORG,
        scope_id=None,
        mode=KillMode.DRAIN,
        reason="from elsewhere",
    )
    assert not (await long_cache.check(organization_id)).stopped, "still cached"

    long_cache.invalidate(organization_id)
    assert (await long_cache.check(organization_id)).stopped


async def test_the_local_override_still_works(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """M0's process-local switch is checked *before* the table, so a worker being
    drained by hand cannot be overridden by the absence of a database row."""
    from runtime.gateway.tools import KillSwitch

    organization_id = await _org(uow_factory)
    harness = build_harness(uow_factory, settings)
    harness.gateway._kill = KillSwitch(engaged=True, reason="local drain")

    ctx = make_ctx(organization_id, authority=make_authority())
    with pytest.raises(KillSwitchEngaged, match="local drain"):
        await harness.gateway.execute(ctx, ToolCall("test.noop@1", {"value": "x"}))

    async with uow_factory() as uow:
        rows = await uow.audit.decisions_for_run(ctx.run_id)
    assert [r["check"] for r in rows] == ["kill_switch_local"], (
        "a stop that left no trace is the one nobody can explain afterwards"
    )
