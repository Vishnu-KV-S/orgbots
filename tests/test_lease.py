"""T4 — lease exclusivity. T5 — fence rejection.

T5 is the important one. It reproduces the failure that a lease alone does not
cover: a worker that is frozen rather than dead, whose lease lapses, whose run is
taken by someone else, and which then wakes up and tries to carry on.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text

from runtime.domain.enums import ActorKind, RunStatus, WorkClass
from runtime.domain.errors import LeaseLost, StaleFence
from runtime.domain.ids import OrganizationId, RunId, new_worker_id
from runtime.domain.specs import (
    ActorSpec,
    Ceilings,
    ModelProfile,
    ModelProfiles,
    StartRunRequest,
)
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.runtime.bootstrap import Registrar
from runtime.runtime.run_service import RunService
from runtime.settings import Settings
from runtime.worker.lease import LeaseManager, Reaper

pytestmark = pytest.mark.integration

SPEC = ActorSpec(
    name="echo-agent",
    kind=ActorKind.LLM_AGENT,
    graph_ref="echo_agent@1",
    allowed_tools=frozenset({"web.fetch@1"}),
    ceilings=Ceilings(),
    model_profiles=ModelProfiles(
        profiles={WorkClass.GENERATION: ModelProfile(provider="fake", model="echo-1")}
    ),
)


@pytest_asyncio.fixture
async def a_run(
    uow_factory: UnitOfWorkFactory, organization_id: OrganizationId, settings: Settings
) -> RunId:
    registrar = Registrar(uow_factory)
    await registrar.ensure_organization(organization_id, "acme")
    await registrar.publish_actor(organization_id, SPEC)
    result = await RunService(uow_factory, settings=settings).start_run(
        StartRunRequest(
            organization_id=organization_id, actor_name="echo-agent", idempotency_key="lease"
        )
    )
    return result.run_id


async def _expire_lease(uow_factory: UnitOfWorkFactory, run_id: RunId) -> None:
    """Move the lease into the past without touching the fence — exactly what a
    frozen worker's run row looks like."""
    async with uow_factory.transaction() as uow:
        await uow.session.execute(
            text("UPDATE runs SET lease_until = now() - interval '1 second' WHERE id = :id"),
            {"id": run_id},
        )


async def test_five_workers_one_run_exactly_one_claim(
    uow_factory: UnitOfWorkFactory, settings: Settings, a_run: RunId
) -> None:
    """T4."""
    managers = [LeaseManager(uow_factory, settings) for _ in range(5)]
    workers = [new_worker_id() for _ in range(5)]

    leases = await asyncio.gather(
        *(m.claim(a_run, w) for m, w in zip(managers, workers, strict=True))
    )
    winners = [ls for ls in leases if ls is not None]

    assert len(winners) == 1, f"{len(winners)} workers claimed the same run"
    assert int(winners[0].fence) == 1, "the first claim must move the fence from 0 to 1"


async def test_the_fence_advances_on_every_claim(
    uow_factory: UnitOfWorkFactory, settings: Settings, a_run: RunId
) -> None:
    manager = LeaseManager(uow_factory, settings)
    first = await manager.claim(a_run, new_worker_id())
    assert first is not None

    await _expire_lease(uow_factory, a_run)
    second = await manager.claim(a_run, new_worker_id())
    assert second is not None
    assert int(second.fence) == int(first.fence) + 1


async def test_check_passes_for_the_current_holder(
    uow_factory: UnitOfWorkFactory, settings: Settings, a_run: RunId
) -> None:
    manager = LeaseManager(uow_factory, settings)
    lease = await manager.claim(a_run, new_worker_id())
    assert lease is not None
    await manager.check(lease)  # must not raise


async def test_frozen_worker_thawing_after_a_steal_is_rejected(
    uow_factory: UnitOfWorkFactory, settings: Settings, a_run: RunId
) -> None:
    """T5. Freeze past the lease, steal, thaw → StaleFence at the gateway."""
    manager = LeaseManager(uow_factory, settings)
    frozen_worker = new_worker_id()
    zombie_lease = await manager.claim(a_run, frozen_worker)
    assert zombie_lease is not None

    # The worker freezes; its lease lapses.
    await _expire_lease(uow_factory, a_run)

    # Someone else takes the run.
    thief_lease = await manager.claim(a_run, new_worker_id())
    assert thief_lease is not None
    assert int(thief_lease.fence) == int(zombie_lease.fence) + 1

    # The frozen worker thaws, still holding a lease object that looks fine.
    with pytest.raises(StaleFence) as excinfo:
        await manager.check(zombie_lease)
    assert excinfo.value.held == int(zombie_lease.fence)
    assert excinfo.value.current == int(thief_lease.fence)

    # And it cannot finish the run either.
    assert await manager.release(zombie_lease, RunStatus.SUCCESS) is False
    async with uow_factory() as uow:
        run = await uow.runs.get(a_run)
    assert run is not None
    assert run.status == "RUNNING"


async def test_a_lapsed_lease_is_rejected_even_before_anyone_steals_it(
    uow_factory: UnitOfWorkFactory, settings: Settings, a_run: RunId
) -> None:
    """The reaper clears `worker_id` when it requeues. A worker whose lease merely
    lapsed must stop then, not at the moment someone else happens to claim."""
    manager = LeaseManager(uow_factory, settings)
    lease = await manager.claim(a_run, new_worker_id())
    assert lease is not None

    await _expire_lease(uow_factory, a_run)
    await Reaper(uow_factory, settings).sweep()

    with pytest.raises(StaleFence):
        await manager.check(lease)


async def test_heartbeat_extends_the_lease(
    uow_factory: UnitOfWorkFactory, settings: Settings, a_run: RunId
) -> None:
    manager = LeaseManager(uow_factory, settings)
    lease = await manager.claim(a_run, new_worker_id())
    assert lease is not None
    beaten = await manager.heartbeat(lease)
    assert beaten.lease_until > lease.lease_until
    assert beaten.fence == lease.fence


async def test_heartbeat_after_a_steal_raises_lease_lost(
    uow_factory: UnitOfWorkFactory, settings: Settings, a_run: RunId
) -> None:
    manager = LeaseManager(uow_factory, settings)
    lease = await manager.claim(a_run, new_worker_id())
    assert lease is not None
    await _expire_lease(uow_factory, a_run)
    await manager.claim(a_run, new_worker_id())

    with pytest.raises(LeaseLost):
        await manager.heartbeat(lease)


async def test_reaper_requeues_then_abandons(
    uow_factory: UnitOfWorkFactory, settings: Settings, a_run: RunId
) -> None:
    """A run that keeps killing its worker must eventually stop being requeued."""
    manager = LeaseManager(uow_factory, settings)
    reaper = Reaper(uow_factory, settings)

    for expected in range(1, settings.max_lease_expiries):
        assert await manager.claim(a_run, new_worker_id()) is not None
        await _expire_lease(uow_factory, a_run)
        reaped = await reaper.sweep()
        assert reaped == [(a_run, RunStatus.QUEUED)], f"sweep {expected}"

    assert await manager.claim(a_run, new_worker_id()) is not None
    await _expire_lease(uow_factory, a_run)
    assert await reaper.sweep() == [(a_run, RunStatus.ABANDONED)]

    async with uow_factory() as uow:
        run = await uow.runs.get(a_run)
    assert run is not None
    assert run.status == "ABANDONED"
    assert run.ended_at is not None
    assert "lease expired" in (run.status_reason or "")


async def test_an_abandoned_run_is_not_claimable(
    uow_factory: UnitOfWorkFactory, settings: Settings, a_run: RunId
) -> None:
    async with uow_factory.transaction() as uow:
        await uow.session.execute(
            text("UPDATE runs SET status = 'ABANDONED' WHERE id = :id"), {"id": a_run}
        )
    assert await LeaseManager(uow_factory, settings).claim(a_run, new_worker_id()) is None


async def test_reaper_ignores_healthy_leases(
    uow_factory: UnitOfWorkFactory, settings: Settings, a_run: RunId
) -> None:
    manager = LeaseManager(uow_factory, settings)
    lease = await manager.claim(a_run, new_worker_id())
    assert lease is not None
    assert lease.lease_until > dt.datetime.now(dt.UTC)
    assert await Reaper(uow_factory, settings).sweep() == []


async def test_reaper_re_announces_requeued_runs(
    uow_factory: UnitOfWorkFactory, settings: Settings, a_run: RunId
) -> None:
    """A requeued run must reach a worker without waiting for the next poll, and
    the re-announcement must not collide with the original."""
    manager = LeaseManager(uow_factory, settings)
    assert await manager.claim(a_run, new_worker_id()) is not None
    await _expire_lease(uow_factory, a_run)
    await Reaper(uow_factory, settings).sweep()

    async with uow_factory() as uow:
        keys = (
            (
                await uow.session.execute(
                    text("SELECT dedupe_key FROM outbox WHERE topic = 'run.queued' ORDER BY id")
                )
            )
            .scalars()
            .all()
        )
    assert keys == [str(a_run), f"{a_run}:requeue:1"]


async def test_check_on_a_deleted_run_is_stale_not_a_crash(
    uow_factory: UnitOfWorkFactory, settings: Settings, a_run: RunId
) -> None:
    manager = LeaseManager(uow_factory, settings)
    lease = await manager.claim(a_run, new_worker_id())
    assert lease is not None
    async with uow_factory.transaction() as uow:
        await uow.session.execute(
            text("DELETE FROM budget_reservations WHERE run_id = :id"), {"id": a_run}
        )
        await uow.session.execute(text("DELETE FROM run_specs WHERE run_id = :id"), {"id": a_run})
        await uow.session.execute(text("DELETE FROM runs WHERE id = :id"), {"id": a_run})
    with pytest.raises(StaleFence):
        await manager.check(lease)


async def test_claim_is_a_no_op_for_an_unknown_run(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    unknown = RunId(uuid.uuid4())
    assert await LeaseManager(uow_factory, settings).claim(unknown, new_worker_id()) is None
