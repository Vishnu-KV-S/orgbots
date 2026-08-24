"""The memory worker. §6's *"never the hot path"*, made structural.

A separate consumer group on `run.succeeded`, drained by its own loop alongside the
relay, the reaper and the governance sweeper. The run is already finished and its
outcome already announced by the time an entry arrives here, so **there is no code path
by which anything in this module can add latency to a run** — which is what T44 asserts
by measuring P95 with consolidation running and comparing against it stopped.

**Its own consumer group, not a second reader on the workers' group.** A group is a
partition of the stream: if the memory worker joined `workers`, every `run.succeeded`
entry it claimed would be one the run workers never saw. Two groups get two independent
copies of every event, which is exactly the fan-out this needs and is how Redis Streams
is meant to be used for it.

**At-least-once delivery, made idempotent in the database.** Redis redelivers, and it
should — an entry lost because a worker died mid-extraction is a run whose facts are
gone forever with nothing recording that they were missed. So every delivery is safe to
repeat: `_already_processed` short-circuits on a `memory_audit` row for the run, and a
run that yielded no facts writes a marker row so that "extracted nothing" and "never
looked" stay distinguishable. The second of those is the useful one — it is how §12's
*"did memory actually stop the re-derivation you predicted"* gets a denominator.

**Failure is absorbed, and counted.** Ack first, then process. That is the opposite of
the relay's ordering and the opposite of what a correctness-critical consumer should do,
and it is right here for one reason: the alternative is a poison entry — a run whose
extraction reliably raises — being redelivered forever at MEMORY-class prices. §13 risk 4 is
about consolidation cost becoming invisible; an infinite retry loop is that risk with a
multiplier. A dropped memory is a gap in `v_memory_inventory`; a retry storm is a bill.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import uuid

from sqlalchemy import text

from runtime.domain.enums import MemoryScope
from runtime.domain.ids import BudgetPoolId, MemoryId, OrganizationId, RunId
from runtime.events.stream import RedisStreams
from runtime.events.topics import TOPIC_RUN_SUCCEEDED
from runtime.memory.service import MemoryService
from runtime.observability.logging import get_logger
from runtime.org.department import company_scope_id
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.settings import Settings, get_settings

log = get_logger("memory.worker")

MEMORY_GROUP = "memory"
"""The consumer group. Distinct from `Settings.consumer_group` ("workers") on purpose —
see the module docstring."""

RUN_MARKER_PREFIX = "run:"
"""`memory_audit.memory_id` for the "this run produced no memories" marker.

A prefixed synthetic id rather than a nullable column, because `memory_audit` is the
one place that answers "what has ever happened to a memory" and a marker with a null
subject would need a second query shape to find. The prefix is not a valid store id —
store ids are UUID strings — so a marker can never collide with a real memory."""


class MemoryWorker:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        streams: RedisStreams,
        service: MemoryService,
        *,
        settings: Settings | None = None,
    ) -> None:
        self._uow = uow_factory
        self._streams = streams
        self._service = service
        self._settings = settings or get_settings()
        self._stopping = asyncio.Event()
        self._consumer = f"memory-{uuid.uuid4().hex[:8]}"
        self.processed = 0
        self.failed = 0
        self.skipped = 0

    def stop(self) -> None:
        self._stopping.set()

    async def setup(self) -> None:
        await self._streams.ensure_group(TOPIC_RUN_SUCCEEDED, MEMORY_GROUP)

    async def run_forever(self) -> None:
        if not self._settings.memory_enabled:
            log.info("memory.worker_disabled", reason="RUNTIME_MEMORY_ENABLED is false")
            return
        await self.setup()
        log.info("memory.worker_started", consumer=self._consumer)
        await asyncio.gather(self._write_loop(), self._consolidation_loop())

    async def _write_loop(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.drain()
            except Exception as exc:
                log.error("memory.worker_failed", error=f"{type(exc).__name__}: {exc}")
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stopping.wait(), timeout=5.0)

    async def _consolidation_loop(self) -> None:
        """Pruning and proposal hygiene, on a slow clock.

        Hourly rather than on the write interval. Consolidation is maintenance over the
        whole store, and running it every five seconds would mean the store spends more
        effort tidying itself than remembering things — which is §13 risk 4 arriving as
        a design choice rather than as a surprise.
        """
        if not self._settings.memory_consolidation_enabled:
            return
        while not self._stopping.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=3600.0)
            if self._stopping.is_set():
                return
            try:
                await self.consolidate_all()
            except Exception as exc:
                log.error("memory.consolidation_failed", error=str(exc))

    async def drain(self, *, block_ms: int = 2_000) -> int:
        """One batch. Returns how many entries were handled (processed or skipped)."""
        count = self._settings.memory_write_batch_size
        entries = await self._streams.read_pending(
            TOPIC_RUN_SUCCEEDED, self._consumer, count=count, group=MEMORY_GROUP
        )
        if not entries:
            entries = await self._streams.read(
                TOPIC_RUN_SUCCEEDED,
                self._consumer,
                count=count,
                block_ms=block_ms,
                group=MEMORY_GROUP,
            )
        if not entries:
            return 0

        handled = 0
        for entry_id, fields in entries:
            # Ack before work. See the module docstring: a poison entry retried forever
            # is more expensive than a memory not written.
            await self._streams.ack(TOPIC_RUN_SUCCEEDED, [entry_id], group=MEMORY_GROUP)
            handled += 1
            try:
                await self._handle(fields)
            except Exception as exc:
                self.failed += 1
                log.error(
                    "memory.entry_failed",
                    entry_id=entry_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
        return handled

    async def _handle(self, fields: dict[str, str]) -> None:
        import json

        organization_id = OrganizationId(uuid.UUID(fields["organization_id"]))
        payload = json.loads(fields.get("payload") or "{}")
        raw_run_id = payload.get("run_id")
        if raw_run_id is None:
            self.skipped += 1
            return
        run_id = RunId(uuid.UUID(str(raw_run_id)))

        if await self._already_processed(run_id):
            self.skipped += 1
            return

        pool_id = await self._pool_for(run_id)
        outcome = await self._service.write_from_run(
            organization_id=organization_id,
            run_id=run_id,
            pool_id=pool_id,
            scope=MemoryScope.PRIVATE_ACTOR,
        )
        if not outcome.written:
            await self._mark_examined(organization_id, run_id, outcome.skipped_reason)
        self.processed += 1

    async def _already_processed(self, run_id: RunId) -> bool:
        """Any audit row for this run means the write path has run to completion.

        Cheap — `ix_memory_audit_memory` does not serve this, but the row count per run
        is single digits and the query is bounded by `LIMIT 1`. Correct, which matters
        more: the alternative shapes (a processed-runs table, a cursor) both add a write
        to the hot path's neighbourhood to save a read on a path that is already off it.
        """
        async with self._uow() as uow:
            return bool(
                (
                    await uow.session.execute(
                        text("SELECT 1 FROM memory_audit WHERE run_id = :r LIMIT 1"),
                        {"r": run_id},
                    )
                ).scalar_one_or_none()
            )

    async def _mark_examined(
        self, organization_id: OrganizationId, run_id: RunId, reason: str | None
    ) -> None:
        """Record that this run was looked at and yielded nothing.

        Without this row, "extracted nothing" and "never processed" are the same state,
        and the second one silently re-processes on every redelivery — paying for a
        second extraction of a run that has already been read.
        """
        async with self._uow.transaction() as uow:
            await uow.memories.audit(
                memory_id=MemoryId(f"{RUN_MARKER_PREFIX}{run_id}"),
                organization_id=organization_id,
                event="NOOP",
                run_id=run_id,
                detail={"reason": reason or "no facts", "marker": True},
            )

    async def _pool_for(self, run_id: RunId) -> BudgetPoolId | None:
        """The run's own budget pool, so extraction is charged where the run was.

        §8 asks for the review to have its own budget line and §13 risk 4 asks for
        consolidation cost to be visible from day one. `work_class=MEMORY` gives the
        line; charging the originating actor's pool gives the attribution — memory spend
        for `research` lands on `research`, which is the only way "is memory worth it for
        this actor" is answerable per actor rather than as one departmental lump.
        """
        async with self._uow() as uow:
            # In the frozen spec, not on `runs`: `RunSpec.budget_pool_id` is the leaf
            # of the chain the run was admitted against, and admission is the only
            # thing that ever decided it.
            pool = (
                await uow.session.execute(
                    text("SELECT spec ->> 'budget_pool_id' FROM run_specs WHERE run_id = :r"),
                    {"r": run_id},
                )
            ).scalar_one_or_none()
        return BudgetPoolId(uuid.UUID(str(pool))) if pool else None

    async def consolidate_all(self) -> dict[str, int]:
        totals = {"retired": 0, "withdrawn": 0}
        async with self._uow() as uow:
            orgs = (
                (
                    await uow.session.execute(
                        text("SELECT DISTINCT organization_id FROM memory_metadata")
                    )
                )
                .scalars()
                .all()
            )
        for raw in orgs:
            organization_id = OrganizationId(uuid.UUID(str(raw)))
            result = await self._service.consolidate(
                organization_id,
                scope=MemoryScope.COMPANY,
                scope_id=company_scope_id(organization_id),
                prune_after_days=self._settings.memory_prune_after_days,
            )
            for key, value in result.items():
                totals[key] = totals.get(key, 0) + value
        return totals


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)
