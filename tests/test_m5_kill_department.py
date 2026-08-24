"""`KillScope.DEPARTMENT` — stopping a department, not four actors one at a time.

The other four scopes are the subjects a gateway call already carries: this tool,
this actor, this connection, or everything. A department is the one an operator
actually says out loud — *"stop growth"* — and expressing it as four actor switches
engaged in sequence is how the fifth actor gets missed.

Four properties are asserted here, and the fourth is the one that would rot silently:

1. It covers every member and nobody outside it, for runs *and* for tool calls.
2. `halt` still beats `drain`, at department scope like everywhere else.
3. `actor=None` is not covered, because a department is resolved through the actor
   and a check with no actor has nothing to resolve. A stated gap, not a bug.
4. **The org-chart lookup is not issued when no department switch is engaged.** That
   is the whole cost argument for the second cache: `check()` is on the hot path of
   every model and tool call, and a department scope that added a query to all of
   them would have been the wrong trade.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from runtime.domain.enums import KillMode, KillScope
from runtime.domain.errors import KillSwitchEngaged
from runtime.domain.ids import OrganizationId
from runtime.gateway.tools import ToolCall
from runtime.org.department import CONTENT, HEAD, RESEARCH
from runtime.org.governance_seed import DEPARTMENT
from runtime.org.killswitch import KillSwitchService
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.settings import Settings
from tests.conftest_m2 import (
    build_harness,
    grant_tools,
    make_authority,
    make_ctx,
    new_governed_org,
)

pytestmark = pytest.mark.integration

OUTSIDER = "lone-contractor"


class CountingKillSwitchService(KillSwitchService):
    """`KillSwitchService`, plus a tally of how often it consulted the org chart.

    It counts *consultations*, not queries — the cache sits inside the method, so a
    consultation that hits it costs nothing. Zero consultations is the claim worth
    making: it means the code path was never entered at all.

    Subclassed rather than monkeypatched so the counter cannot drift away from the
    method it is counting — a patch would keep passing after the call site moved.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.department_lookups = 0

    async def departments_by_actor(self, organization_id: OrganizationId) -> dict[str, str]:
        self.department_lookups += 1
        return await super().departments_by_actor(organization_id)


async def _org_with_an_outsider(uow_factory: UnitOfWorkFactory) -> OrganizationId:
    """The shipped department, plus one actor placed in no department at all.

    The outsider is the control. A switch that stopped everything would pass every
    assertion about members and say nothing about scope.
    """
    organization_id = await new_governed_org(uow_factory)
    async with uow_factory.transaction() as uow:
        await uow.session.execute(
            text(
                """
                INSERT INTO actors (id, organization_id, name, kind, department)
                VALUES (:id, :org, :name, 'llm', NULL)
                """
            ),
            {"id": uuid.uuid4(), "org": organization_id, "name": OUTSIDER},
        )
    return organization_id


# --- coverage -------------------------------------------------------------------------


async def test_a_department_switch_stops_every_member_and_nobody_else(
    uow_factory: UnitOfWorkFactory,
) -> None:
    organization_id = await _org_with_an_outsider(uow_factory)
    service = KillSwitchService(uow_factory)

    assert await service.engage(
        organization_id,
        scope_type=KillScope.DEPARTMENT,
        scope_id=DEPARTMENT,
        reason="growth is spending too fast",
    )

    for member in (HEAD, RESEARCH, CONTENT):
        verdict = await service.admits_runs(organization_id, member)
        assert verdict.stopped, member
        assert verdict.scope == f"department:{DEPARTMENT}"
        assert verdict.mode is KillMode.DRAIN

    assert not (await service.admits_runs(organization_id, OUTSIDER)).stopped, (
        "an actor in no department is outside a department switch"
    )
    assert not (await service.admits_runs(organization_id, "never-published")).stopped, (
        "an unknown actor resolves to no department and is therefore not covered"
    )


async def test_a_department_switch_stops_a_tool_call(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """A stop that only refused *new runs* would let a running actor keep publishing.

    `ToolGateway` checks with `actor=` both before the effect and after it, so a
    department switch reaches the two places that matter without a call-site change.
    """
    organization_id = await _org_with_an_outsider(uow_factory)
    await grant_tools(uow_factory, organization_id, RESEARCH, "test.noop@1")
    harness = build_harness(uow_factory, settings)
    ctx = make_ctx(organization_id, authority=make_authority(actor_name=RESEARCH))

    await harness.kill_switches.engage(
        organization_id,
        scope_type=KillScope.DEPARTMENT,
        scope_id=DEPARTMENT,
        reason="stop the department",
    )

    with pytest.raises(KillSwitchEngaged, match="stop the department"):
        await harness.gateway.execute(ctx, ToolCall("test.noop@1", {"value": "x"}))

    assert harness.tool.calls == [], "the tool never ran"
    async with uow_factory() as uow:
        assert await uow.effects.for_run(ctx.run_id) == [], "drain refuses before the journal"


async def test_halt_beats_drain_at_department_scope(uow_factory: UnitOfWorkFactory) -> None:
    """An operator escalating drain → halt must not have to disengage first.

    Two live switches cover `research`: an actor-scoped drain and a department-scoped
    halt. The post-effect check is where the modes differ, and `halt` has to win, or
    the escalation is silently a no-op.
    """
    organization_id = await _org_with_an_outsider(uow_factory)
    service = KillSwitchService(uow_factory)

    await service.engage(
        organization_id,
        scope_type=KillScope.ACTOR,
        scope_id=RESEARCH,
        mode=KillMode.DRAIN,
        reason="first, gently",
    )
    await service.engage(
        organization_id,
        scope_type=KillScope.DEPARTMENT,
        scope_id=DEPARTMENT,
        mode=KillMode.HALT,
        reason="now, hard",
    )

    post = await service.check(organization_id, actor=RESEARCH, post_effect=True)
    assert post.stopped and post.mode is KillMode.HALT
    assert post.reason == "now, hard"


async def test_a_department_switch_does_not_cover_a_check_with_no_actor(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """The stated gap, asserted so it stays stated.

    A department is resolved *through* the actor. A tool- or connection-only check
    carries no actor, so there is nothing to resolve and the honest answer is that the
    switch does not cover it. Stopping such a call would mean guessing.
    """
    organization_id = await _org_with_an_outsider(uow_factory)
    service = KillSwitchService(uow_factory)
    await service.engage(
        organization_id,
        scope_type=KillScope.DEPARTMENT,
        scope_id=DEPARTMENT,
        reason="department stopped",
    )

    assert not (await service.check(organization_id, tool="test.noop@1")).stopped
    assert not (await service.check(organization_id, connection="search-primary")).stopped
    assert (await service.check(organization_id, tool="test.noop@1", actor=RESEARCH)).stopped


# --- cost -----------------------------------------------------------------------------


async def test_the_org_chart_is_not_read_when_no_department_switch_is_engaged(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """The hot path pays nothing for a scope nobody is using.

    `check()` runs on every model call and every tool call. If resolving a department
    were unconditional, adding this scope would have put a second query behind all of
    them — for an answer that is `no switches` 99.99% of the time.
    """
    organization_id = await _org_with_an_outsider(uow_factory)
    service = CountingKillSwitchService(uow_factory)

    for _ in range(3):
        assert not (await service.admits_runs(organization_id, RESEARCH)).stopped
    assert service.department_lookups == 0, "no switch at all: nothing to resolve"

    await service.engage(
        organization_id,
        scope_type=KillScope.ACTOR,
        scope_id=CONTENT,
        reason="one actor only",
    )
    for _ in range(3):
        await service.admits_runs(organization_id, RESEARCH)
    assert service.department_lookups == 0, "a live switch of another scope changes nothing"

    await service.engage(
        organization_id,
        scope_type=KillScope.DEPARTMENT,
        scope_id=DEPARTMENT,
        reason="now it matters",
    )
    assert (await service.admits_runs(organization_id, RESEARCH)).stopped
    assert service.department_lookups == 1, "and only now is it consulted"


async def test_the_department_map_is_cached_and_invalidate_drops_it(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """The map is cached for the same ten seconds the switch list is, and `invalidate`
    clears both.

    Asserted by moving an actor into the stopped department *underneath* a warm cache:
    the stale answer is the evidence that the second query was not issued. Then
    `invalidate()` — which every `engage`/`disengage` already calls — makes it current.

    Per-process, and that is the trade M2 §7 chose: the process that pulled the switch
    sees it at once, and every other process within `KILL_CACHE_TTL_SECONDS`.
    """
    organization_id = await _org_with_an_outsider(uow_factory)
    service = KillSwitchService(uow_factory)
    await service.engage(
        organization_id,
        scope_type=KillScope.DEPARTMENT,
        scope_id=DEPARTMENT,
        reason="stopped",
    )

    assert not (await service.admits_runs(organization_id, OUTSIDER)).stopped

    async with uow_factory.transaction() as uow:
        await uow.authority.set_actor_role(
            organization_id, OUTSIDER, role_name=None, department=DEPARTMENT
        )

    assert not (await service.admits_runs(organization_id, OUTSIDER)).stopped, (
        "still the cached org chart — no second query was issued"
    )

    service.invalidate(organization_id)
    assert (await service.admits_runs(organization_id, OUTSIDER)).stopped
