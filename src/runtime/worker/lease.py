"""Leases and fencing.

A lease says "this worker owns this run until this time". A fence says "and here is
the proof, checkable after the fact". The difference matters because a lease alone
is useless against the failure that actually happens: a worker that is not dead,
just stopped — swapped out, GC-paused, suspended by the kernel — which wakes up
after its lease expired and someone else took over, still holding a perfectly
valid-looking lease object and a half-finished tool call.

`check()` is the answer to that. It runs at every gateway entry, reads the
authoritative fence, and refuses if the run has moved on. A thawed zombie cannot
commit an effect, cannot write an artifact, and cannot mark the run SUCCESS.

`check()` is a database read, so it is async — the `def check(...)` in the M0 plan
was indicative. The read is a primary-key lookup on a row already in cache; making
it a local comparison against a cached fence would defeat the entire purpose,
because the cached value is exactly what a frozen worker has stale.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
from typing import Protocol

from runtime.domain.context import Lease
from runtime.domain.enums import RunStatus
from runtime.domain.errors import LeaseLost, StaleFence
from runtime.domain.ids import Fence, RunId, WorkerId
from runtime.observability.logging import get_logger
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.settings import Settings, get_settings

log = get_logger("worker.lease")


class LeaseManagerProtocol(Protocol):
    async def claim(self, run_id: RunId, worker_id: WorkerId) -> Lease | None: ...
    async def heartbeat(self, lease: Lease) -> Lease: ...
    async def check(self, lease: Lease) -> None: ...


class LeaseManager:
    def __init__(self, uow_factory: UnitOfWorkFactory, settings: Settings | None = None) -> None:
        self._uow = uow_factory
        self._settings = settings or get_settings()

    async def claim(self, run_id: RunId, worker_id: WorkerId) -> Lease | None:
        """Conditional UPDATE; None if another worker owns it."""
        async with self._uow.transaction() as uow:
            result = await uow.runs.claim(run_id, worker_id, self._settings.lease_seconds)
        if result is None:
            return None
        return Lease(
            run_id=run_id,
            worker_id=worker_id,
            fence=result.fence,
            lease_until=result.lease_until,
        )

    async def heartbeat(self, lease: Lease) -> Lease:
        async with self._uow.transaction() as uow:
            result = await uow.runs.heartbeat(
                lease.run_id, lease.worker_id, lease.fence, self._settings.lease_seconds
            )
        if result is None:
            raise LeaseLost(f"run {lease.run_id}: lease no longer held at fence {lease.fence}")
        return Lease(
            run_id=lease.run_id,
            worker_id=lease.worker_id,
            fence=result.fence,
            lease_until=result.lease_until,
        )

    async def check(self, lease: Lease) -> None:
        """Raise `StaleFence` if the run's fence has advanced.

        Called at every gateway entry — this is what stops a thawed zombie from
        committing. Ownership is checked as well as the fence: the reaper clears
        `worker_id` when it requeues, so a worker whose lease merely lapsed is
        stopped even before anyone else claims the run.
        """
        async with self._uow() as uow:
            current = await uow.runs.current_fence(lease.run_id)
        if current is None:
            raise StaleFence(lease.run_id, int(lease.fence), -1)
        fence, worker_id, status = current
        if fence != int(lease.fence) or worker_id != lease.worker_id:
            raise StaleFence(lease.run_id, int(lease.fence), fence)
        if RunStatus(status).is_terminal:
            raise StaleFence(lease.run_id, int(lease.fence), fence)

    async def release(self, lease: Lease, status: RunStatus, reason: str | None = None) -> bool:
        """Terminal transition, refused if the fence moved."""
        async with self._uow.transaction() as uow:
            return await uow.runs.finish(lease.run_id, lease.worker_id, lease.fence, status, reason)


class Heartbeater:
    """Keeps a lease alive for as long as a run is executing.

    Runs as its own task rather than being called from the graph, because the whole
    point is to keep beating during a long tool call — the moment where a
    cooperative heartbeat would be starved is exactly the moment it is needed.

    On `LeaseLost` it sets `lost`, and the worker checks that between nodes. It
    does not cancel the run task itself: a hard cancel mid-tool-call is precisely
    the crash the effect journal is designed to survive, but it is still better to
    stop cleanly at a node boundary when we have the option.
    """

    def __init__(self, manager: LeaseManager, lease: Lease, interval: float) -> None:
        self._manager = manager
        self._lease = lease
        self._interval = interval
        self._task: asyncio.Task[None] | None = None
        self.lost = asyncio.Event()

    @property
    def lease(self) -> Lease:
        return self._lease

    async def _beat(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                self._lease = await self._manager.heartbeat(self._lease)
            except LeaseLost:
                log.warning(
                    "lease.lost", run_id=str(self._lease.run_id), fence=int(self._lease.fence)
                )
                self.lost.set()
                return
            except Exception:
                # A transient DB error must not kill the heartbeat loop; the lease
                # has slack for exactly this.
                log.warning("lease.heartbeat_error", run_id=str(self._lease.run_id))

    async def __aenter__(self) -> Heartbeater:
        self._task = asyncio.create_task(self._beat())
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task


class Reaper:
    """Requeues runs whose worker stopped heartbeating.

    This is the only thing that makes a `SIGKILL` recoverable: the killed worker
    cannot release its own lease, so someone has to notice the lease lapsed. The
    reaper does not need to know why the worker stopped.
    """

    def __init__(self, uow_factory: UnitOfWorkFactory, settings: Settings | None = None) -> None:
        self._uow = uow_factory
        self._settings = settings or get_settings()
        self._stopping = asyncio.Event()

    async def sweep(self) -> list[tuple[RunId, RunStatus]]:
        async with self._uow.transaction() as uow:
            reaped = await uow.runs.reap_expired(self._settings.max_lease_expiries)
            for item in reaped:
                await uow.audit.record(
                    organization_id=item.organization_id,
                    run_id=item.run_id,
                    root_run_id=item.root_run_id,
                    action="run.reaped",
                    outcome=item.new_status.value,
                    detail={"lease_expiries": item.lease_expiries},
                )
                if item.new_status is RunStatus.QUEUED:
                    # Re-announce so a worker picks it up without waiting for the
                    # next poll. The dedupe key includes the expiry count, because
                    # this is a genuinely new announcement of the same run.
                    await uow.outbox.enqueue(
                        organization_id=item.organization_id,
                        topic="run.queued",
                        payload={"run_id": str(item.run_id), "requeued": True},
                        dedupe_key=f"{item.run_id}:requeue:{item.lease_expiries}",
                        run_id=item.run_id,
                        root_run_id=item.root_run_id,
                    )
        return [(r.run_id, r.new_status) for r in reaped]

    async def run_forever(self) -> None:
        while not self._stopping.is_set():
            try:
                reaped = await self.sweep()
                if reaped:
                    log.info("reaper.swept", count=len(reaped))
            except Exception:
                log.exception("reaper.failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stopping.wait(), timeout=self._settings.reaper_interval_seconds
                )

    def stop(self) -> None:
        self._stopping.set()


def lease_expired(lease: Lease, now: dt.datetime | None = None) -> bool:
    return lease.lease_until <= (now or dt.datetime.now(dt.UTC))


def next_fence(fence: Fence) -> Fence:
    return Fence(int(fence) + 1)
