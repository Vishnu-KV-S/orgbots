"""T3 — the outbox publishes each row exactly once, across relay crashes.

The failure this guards against is the one that looks fine in a happy-path test:
a relay that marks rows published before it publishes them loses events only when
it crashes in a window a few microseconds wide.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text

from runtime.events.relay import OutboxRelay
from runtime.events.stream import RedisStreams
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.settings import Settings

pytestmark = pytest.mark.integration

TOPIC = "run.queued"


@pytest_asyncio.fixture
async def streams(settings: Settings) -> RedisStreams:
    s = RedisStreams(settings)
    await s.client.flushall()
    yield s
    await s.client.flushall()
    await s.close()


async def _enqueue(uow_factory: UnitOfWorkFactory, n: int, org: uuid.UUID) -> None:
    async with uow_factory.transaction() as uow:
        for i in range(n):
            await uow.outbox.enqueue(
                organization_id=org,
                topic=TOPIC,
                payload={"run_id": str(uuid.uuid4()), "n": i},
                dedupe_key=f"row-{i}",
            )


async def test_drain_publishes_every_row_once(
    uow_factory: UnitOfWorkFactory, streams: RedisStreams, settings: Settings
) -> None:
    org = uuid.uuid4()
    await _enqueue(uow_factory, 25, org)
    relay = OutboxRelay(uow_factory, streams, settings)

    assert await relay.drain() == 25
    assert await relay.drain() == 0
    assert await streams.length(TOPIC) == 25


async def test_a_crash_before_commit_does_not_duplicate_the_stream_entry(
    uow_factory: UnitOfWorkFactory, streams: RedisStreams, settings: Settings
) -> None:
    """T3. Simulate the dangerous window: XADD succeeded, the commit did not.

    The rows are still unpublished, so the next drain retries them. The Lua dedupe
    is what stops the retry from writing a second copy.
    """
    org = uuid.uuid4()
    await _enqueue(uow_factory, 10, org)

    class CrashingRelay(OutboxRelay):
        async def drain(self) -> int:
            async with self._uow.transaction() as uow:
                rows = await uow.outbox.unpublished(100)
                for row in rows:
                    await self._streams.publish(
                        row.topic,
                        row.dedupe_key,
                        {"outbox_id": str(row.id), "dedupe_key": row.dedupe_key, "payload": "{}"},
                    )
                raise RuntimeError("worker died between XADD and commit")

    with pytest.raises(RuntimeError, match="died between"):
        await CrashingRelay(uow_factory, streams, settings).drain()

    async with uow_factory() as uow:
        unpublished = (
            await uow.session.execute(
                text("SELECT count(*) FROM outbox WHERE published_at IS NULL")
            )
        ).scalar_one()
    assert unpublished == 10, "the crash must not have marked anything published"

    assert await OutboxRelay(uow_factory, streams, settings).drain() == 10
    assert await streams.length(TOPIC) == 10, "the retry duplicated stream entries"


async def test_two_relays_racing_do_not_double_publish(
    uow_factory: UnitOfWorkFactory, streams: RedisStreams, settings: Settings
) -> None:
    """`FOR UPDATE SKIP LOCKED` plus the dedupe hash, under real concurrency."""
    import asyncio

    org = uuid.uuid4()
    await _enqueue(uow_factory, 60, org)
    relays = [OutboxRelay(uow_factory, streams, settings) for _ in range(4)]

    totals = await asyncio.gather(*(r.drain() for r in relays))
    remaining = await OutboxRelay(uow_factory, streams, settings).drain()

    assert sum(totals) + remaining == 60
    assert await streams.length(TOPIC) == 60


async def test_the_same_dedupe_key_never_produces_two_stream_entries(
    streams: RedisStreams,
) -> None:
    first = await streams.publish(TOPIC, "same", {"payload": "{}"})
    second = await streams.publish(TOPIC, "same", {"payload": "{}"})
    assert first is not None
    assert second is None
    assert await streams.length(TOPIC) == 1


async def test_publish_survives_a_redis_flush(streams: RedisStreams) -> None:
    """After FLUSHALL the cached script SHA is stale. That must not be fatal — R1
    depends on the relay coming back rather than crash-looping."""
    await streams.publish(TOPIC, "before", {"payload": "{}"})
    await streams.client.flushall()
    entry = await streams.publish(TOPIC, "after", {"payload": "{}"})
    assert entry is not None
    assert await streams.length(TOPIC) == 1


async def test_outbox_enqueue_is_idempotent_on_dedupe_key(
    uow_factory: UnitOfWorkFactory,
) -> None:
    org = uuid.uuid4()
    async with uow_factory.transaction() as uow:
        first = await uow.outbox.enqueue(org, TOPIC, {"a": 1}, "dupe")
        second = await uow.outbox.enqueue(org, TOPIC, {"a": 2}, "dupe")
    assert first is not None
    assert second is None

    async with uow_factory() as uow:
        count = (await uow.session.execute(text("SELECT count(*) FROM outbox"))).scalar_one()
        events = (await uow.session.execute(text("SELECT count(*) FROM events"))).scalar_one()
    assert (count, events) == (1, 1)
