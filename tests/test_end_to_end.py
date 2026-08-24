"""The first working run, and what it must leave behind.

This is the trace from §7 of the M0 plan, asserted rather than described.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text

from runtime.domain.enums import EffectStatus, RunStatus
from runtime.domain.ids import OrganizationId
from runtime.settings import Settings
from tests.conftest_runtime import Runtime, build_runtime

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def rt(settings: Settings, organization_id: OrganizationId) -> AsyncIterator[Runtime]:
    async for runtime in build_runtime(settings, organization_id):
        yield runtime


async def test_a_run_goes_from_queued_to_success(rt: Runtime) -> None:
    started = await rt.start("echo-agent", "e2e-1", {"message": "hello"})
    assert started.status is RunStatus.QUEUED

    await rt.pump()

    async with rt.uow() as uow:
        run = await uow.runs.get(started.run_id)
    assert run is not None
    assert run.status == "SUCCESS"
    assert run.started_at is not None
    assert run.ended_at is not None
    assert run.fence == 1, "one claim means one fence bump"


async def test_the_success_event_carries_the_output(rt: Runtime) -> None:
    started = await rt.start("echo-agent", "e2e-2", {"message": "hi there"})
    await rt.pump()

    async with rt.uow() as uow:
        events = await uow.outbox.events_for_run(started.run_id)
    topics = [e["topic"] for e in events]
    assert topics == ["run.queued", "run.succeeded"]
    assert events[-1]["payload"]["output"]["echo"] == "hi there"


async def test_a_tool_call_leaves_exactly_one_committed_effect(rt: Runtime) -> None:
    started = await rt.start(
        "echo-agent", "e2e-3", {"message": "go", "sideeffect": {"kind": "demo"}}
    )
    await rt.pump()

    async with rt.uow() as uow:
        effects = await uow.effects.for_run(started.run_id)
        fixture_rows = (
            await uow.session.execute(
                text("SELECT count(*) FROM sideeffect_fixture WHERE run_id = :r"),
                {"r": started.run_id},
            )
        ).scalar_one()

    assert len(effects) == 1
    assert effects[0].status is EffectStatus.COMMITTED
    assert effects[0].tool_name == "fixture.sideeffect"
    assert fixture_rows == 1


async def test_the_run_records_an_audit_trail(rt: Runtime) -> None:
    started = await rt.start("echo-agent", "e2e-4", {"sideeffect": {}})
    await rt.pump()

    async with rt.uow() as uow:
        entries = await uow.audit.for_run(started.run_id)
    actions = [e["action"] for e in entries]
    assert actions == ["run.start", "tool.fixture.sideeffect", "run.finish"]
    assert entries[-1]["outcome"] == "SUCCESS"


async def test_a_deterministic_worker_runs_without_a_graph(rt: Runtime) -> None:
    started = await rt.start("hasher", "e2e-5", {"b": 2, "a": 1})
    await rt.pump()

    async with rt.uow() as uow:
        run = await uow.runs.get(started.run_id)
        events = await uow.outbox.events_for_run(started.run_id)
    assert run is not None
    assert run.status == "SUCCESS"
    assert events[-1]["payload"]["output"]["keys"] == ["a", "b"]


async def test_the_budget_reservation_is_reconciled(rt: Runtime) -> None:
    """No held reservation may outlive its run — that is how a pool leaks."""
    started = await rt.start("echo-agent", "e2e-6", {"sideeffect": {}})
    await rt.pump()

    async with rt.uow() as uow:
        held = (
            await uow.session.execute(
                text(
                    "SELECT count(*) FROM budget_reservations WHERE run_id = :r AND status = 'HELD'"
                ),
                {"r": started.run_id},
            )
        ).scalar_one()
        # Every level, not just one. M2 made the pool a chain, and a reservation
        # holds against all of it — so a leak at the org root while the actor pool
        # looks clean is exactly the failure this test now has to be able to see.
        pools = (
            await uow.session.execute(
                text(
                    "SELECT scope_type, depth, reserved_cents, allocated_cents "
                    "FROM budget_pools ORDER BY depth"
                )
            )
        ).all()
    assert held == 0
    assert [p.scope_type for p in pools] == ["org", "actor"], "org -> actor, no department"
    assert all(p.reserved_cents == 0 for p in pools), f"headroom still held: {pools}"
    assert all(p.allocated_cents == 0 for p in pools), (
        "a terminal run declares nothing: the allocation must be closed at every level "
        "or the soft admission ceiling ratchets"
    )


async def test_a_second_worker_finds_nothing_to_do(rt: Runtime) -> None:
    """Once a run is terminal it must not be claimable, or a stream redelivery
    would run it twice."""
    started = await rt.start("echo-agent", "e2e-7", {"sideeffect": {}})
    await rt.pump()

    assert await rt.worker.run_one(started.run_id) is None
    async with rt.uow() as uow:
        count = (
            await uow.session.execute(
                text("SELECT count(*) FROM sideeffect_fixture WHERE run_id = :r"),
                {"r": started.run_id},
            )
        ).scalar_one()
    assert count == 1
