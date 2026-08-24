"""Outbox relay.

Moves rows from the `outbox` table to Redis Streams. The ordering inside `drain()`
is the entire design:

1. `SELECT ... FOR UPDATE SKIP LOCKED` — take a batch, holding row locks so no
   other relay touches these rows while we work.
2. `XADD` each one, through the Lua dedupe, so a publish that already happened is
   not repeated.
3. Mark them published and commit.

A crash anywhere in that sequence is safe. Before step 3, the rows stay
unpublished and are retried; step 2's dedupe means the retry does not duplicate
the stream entry. After the commit, the rows are done. There is no ordering of the
crash that produces either a lost event or a doubled one — which is T3.

The alternative — mark published, then XADD — loses events, and is the version
that looks fine in testing because the two operations almost always both succeed.
"""

from __future__ import annotations

import asyncio
import contextlib

from runtime.domain.hashing import canonical_json
from runtime.events.stream import RedisStreams
from runtime.observability.logging import get_logger
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.settings import Settings, get_settings

log = get_logger("events.relay")


class OutboxRelay:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        streams: RedisStreams,
        settings: Settings | None = None,
    ) -> None:
        self._uow = uow_factory
        self._streams = streams
        self._settings = settings or get_settings()
        self._stopping = asyncio.Event()

    async def drain(self) -> int:
        """Publish one batch. Returns how many rows were marked published."""
        async with self._uow.transaction() as uow:
            rows = await uow.outbox.unpublished(self._settings.relay_batch_size)
            if not rows:
                return 0

            published: list[int] = []
            for row in rows:
                try:
                    await self._streams.publish(
                        row.topic,
                        row.dedupe_key,
                        {
                            "outbox_id": str(row.id),
                            "organization_id": str(row.organization_id),
                            "dedupe_key": row.dedupe_key,
                            "payload": canonical_json(row.payload),
                        },
                    )
                except Exception:
                    # Leave this row and everything after it unpublished. Marking a
                    # partial batch and continuing would reorder events for the same
                    # run, and ordering within a run is the one thing the stream is
                    # relied upon for.
                    log.warning("relay.publish_failed", outbox_id=row.id, topic=row.topic)
                    break
                published.append(row.id)

            await uow.outbox.mark_published(published)
            return len(published)

    async def run_forever(self) -> None:
        while not self._stopping.is_set():
            try:
                count = await self.drain()
            except Exception:
                log.exception("relay.drain_failed")
                count = 0
            if count == 0:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._stopping.wait(), timeout=self._settings.relay_interval_seconds
                    )

    def stop(self) -> None:
        self._stopping.set()
