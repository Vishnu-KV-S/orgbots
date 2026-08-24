"""T8 / R1 — Redis is reconstructible.

The claim: Redis holds no state that cannot be rebuilt from Postgres. Wipe it
entirely, mid-flight, and nothing is lost, nothing is duplicated, and no effect
runs twice.

The failure this guards against is a design where the stream *is* the queue. Then
a `FLUSHALL` silently drops every queued run: the outbox rows are already marked
published, so nothing republishes them, and the runs sit at QUEUED forever with
nobody looking. The database poll in the worker loop is what makes that a
non-event, and this test is the only thing that keeps the poll from being deleted
as redundant.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text

from runtime.domain.enums import EffectStatus
from runtime.domain.ids import OrganizationId
from runtime.settings import Settings
from tests.conftest_runtime import Runtime, build_runtime

pytestmark = [pytest.mark.integration, pytest.mark.chaos]


@pytest_asyncio.fixture
async def rt(settings: Settings, organization_id: OrganizationId) -> AsyncIterator[Runtime]:
    async for runtime in build_runtime(settings, organization_id):
        yield runtime


async def test_a_full_flush_loses_no_run(rt: Runtime) -> None:
    """R1: stop → FLUSHALL → restart → recover."""
    started = [
        await rt.start("echo-agent", f"r1-{i}", {"message": str(i), "sideeffect": {"i": i}})
        for i in range(10)
    ]
    await rt.relay.drain()
    assert await rt.streams.length("run.queued") == 10

    # Everything Redis knows is gone: the stream, the consumer group, the dedupe
    # hash, the cached Lua SHA.
    await rt.streams.client.flushall()
    assert await rt.streams.length("run.queued") == 0

    # A restarted worker recreates the group and finds the work in Postgres.
    await rt.worker.setup()
    while await rt.worker.drain_database(limit=10):
        pass

    async with rt.uow() as uow:
        statuses = (
            await uow.session.execute(text("SELECT status, count(*) FROM runs GROUP BY status"))
        ).all()
    assert {row.status: row.count for row in statuses} == {"SUCCESS": 10}

    for result in started:
        async with rt.uow() as uow:
            effects = await uow.effects.for_run(result.run_id)
            rows = (
                await uow.session.execute(
                    text("SELECT count(*) FROM sideeffect_fixture WHERE run_id = :r"),
                    {"r": result.run_id},
                )
            ).scalar_one()
        assert len(effects) == 1
        assert effects[0].status is EffectStatus.COMMITTED
        assert rows == 1, "the wipe caused an effect to run twice"


async def test_a_flush_does_not_duplicate_or_lose_outbox_rows(rt: Runtime) -> None:
    """No outbox row published twice, none left unpublished."""
    for i in range(8):
        await rt.start("echo-agent", f"r1-outbox-{i}", {"message": str(i)})
    assert await rt.relay.drain() == 8

    await rt.streams.client.flushall()

    # Already-published rows must not be republished: `published_at` is the durable
    # record, and the wipe did not touch Postgres.
    assert await rt.relay.drain() == 0
    assert await rt.streams.length("run.queued") == 0

    async with rt.uow() as uow:
        unpublished = (
            await uow.session.execute(
                text("SELECT count(*) FROM outbox WHERE published_at IS NULL")
            )
        ).scalar_one()
        total = (await uow.session.execute(text("SELECT count(*) FROM outbox"))).scalar_one()
    assert (unpublished, total) == (0, 8)


async def test_a_flush_mid_run_does_not_disturb_an_in_flight_run(rt: Runtime) -> None:
    """Redis is not on the path between a claim and a commit, and this proves it:
    the wipe happens between the claim and the execution."""
    started = await rt.start("echo-agent", "r1-midflight", {"sideeffect": {"mid": True}})
    await rt.relay.drain()

    lease = await rt.worker.leases.claim(started.run_id, rt.worker.worker_id)
    assert lease is not None

    await rt.streams.client.flushall()

    outcome = await rt.worker.executor.execute(lease, rt.worker.worker_id)
    assert outcome.status.value == "SUCCESS"

    async with rt.uow() as uow:
        effects = await uow.effects.for_run(started.run_id)
    assert len(effects) == 1
    assert effects[0].status is EffectStatus.COMMITTED


async def test_events_survive_the_wipe_because_they_live_in_postgres(rt: Runtime) -> None:
    """The SSE tail reads `events`, not the stream. A client reconnecting after a
    wipe still gets a complete, ordered history."""
    started = await rt.start("echo-agent", "r1-events", {"message": "durable"})
    await rt.pump()
    await rt.streams.client.flushall()

    async with rt.uow() as uow:
        events = await uow.outbox.events_for_run(started.run_id)
    assert [e["topic"] for e in events] == ["run.queued", "run.succeeded"]


async def test_the_consumer_group_is_recreated_rather_than_erroring(rt: Runtime) -> None:
    await rt.streams.client.flushall()
    await rt.worker.setup()
    await rt.worker.setup()  # idempotent: BUSYGROUP is not an error
    started = await rt.start("echo-agent", "r1-group", {"message": "again"})
    await rt.pump()

    async with rt.uow() as uow:
        run = await uow.runs.get(started.run_id)
    assert run is not None
    assert run.status == "SUCCESS"
