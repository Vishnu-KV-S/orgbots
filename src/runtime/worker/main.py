"""Worker process entrypoint.

Runs up to seven loops side by side: the outbox relay, the reaper, the governance
sweeper, the scheduler, the dispatcher, the memory worker, and the worker itself. In
production these would usually be separate deployments — the relay, reaper and sweeper
are singletons-ish, the workers scale out — but there is one process type and splitting
it would be scaffolding for a shape nobody has needed yet.

**The conductor — the scheduler and the dispatcher — is what makes the loop turn by
itself.** Until it was started here, nothing in a long-lived process turned a cron into
a run or an inbox message into a run: `runtime.cli tick` did, once, when a human typed
it. `docs/MEASUREMENT_PROTOCOL.md` §4 asks for two consecutive weeks with the
long-lived processes running and left alone, and counts any intervention as a reset —
so a runtime whose only crank is a person typing `tick` cannot have a clean run at all.
It is on by default for that reason. `RUNTIME_CONDUCTOR_ENABLED=false` is for the case
where something *else* is driving the loop, not for turning the loop off.

Neither half executes a run. Both only call `RunService.start_run`, which writes and
returns; the run is picked up from the stream by the worker loop below, exactly as it
is when the API starts one. So this does not change what a worker process *is* — it
changes whether anything is asking it to work.

The sweeper is M2's addition and it is not optional. Approval escalation is
time-driven: a chain that is never walked is a list, and `on_expiry` would never
fire. The budget's reservation sweep is the same story — without it one dead worker
holds headroom at every level of the pool chain until the TTL.

**The memory worker is M3's addition and it is entirely optional** — off unless
`RUNTIME_MEMORY_ENABLED` is set, in which case it consumes `run.succeeded` on its own
consumer group and writes memories after the runs that produced them have already
finished. That ordering is §6's *"never the hot path"*, and it is why this loop sits
here rather than inside `RunExecutor`: a subsystem that cannot be started is a subsystem
that cannot add latency.

`MemorySubsystem` is constructed **once** and shared between the run worker (which
retrieves) and the memory worker (which writes), because they must agree on one
`embedding_version` and share one embedding cache. Two subsystems would mean two answers
to "which collection are we in", and that failure presents as an empty retrieval rather
than as an error.

Shutdown is cooperative: SIGTERM sets the stop flags and lets the current run
finish inside its lease. A worker killed with SIGKILL instead is exactly the case
the reaper and the effect journal are built for, so nothing here needs to handle
it.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal

import runtime.graphs.delegator
import runtime.graphs.echo_agent
import runtime.handlers  # noqa: F401  registers hasher@1 and analytics@1
from runtime.events.relay import OutboxRelay
from runtime.events.stream import RedisStreams
from runtime.graphs.checkpointer import checkpointer

# Imported for the side effect as much as the symbol: the module registers
# marketing_head@1, research@1 and content@1 on import, and without it the worker
# process can run `echo_agent@1` and the delegator and nothing else.
from runtime.graphs.department import assert_registered as assert_department_registered
from runtime.memory import MemorySubsystem
from runtime.memory.worker import MemoryWorker
from runtime.observability.logging import configure_logging, get_logger
from runtime.observability.tracing import configure_tracing
from runtime.persistence.engine import dispose_engines
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.runtime.dispatcher import Dispatcher
from runtime.runtime.run_service import RunService
from runtime.runtime.scheduler import Scheduler
from runtime.runtime.sweeper import GovernanceSweeper
from runtime.settings import get_settings
from runtime.worker.lease import Reaper
from runtime.worker.worker import Worker

log = get_logger("worker.main")


async def run() -> None:
    settings = get_settings()
    configure_logging(level=settings.log_level, json=settings.log_json)
    configure_tracing(service_name=settings.service_name, enabled=settings.otel_enabled)

    # The check `runtime.graphs.department` documents but nothing was calling: a worker
    # that starts without a graph an actor's spec names does not find out until a cron
    # fires, and `no graph registered as 'marketing_head@1'` days later reads as a
    # scheduling problem rather than a missing import.
    assert_department_registered()

    uow = UnitOfWorkFactory(settings)
    streams = RedisStreams(settings)
    relay = OutboxRelay(uow, streams, settings)
    reaper = Reaper(uow, settings)
    sweeper = GovernanceSweeper(uow, settings=settings)

    # The conductor. Constructed only when enabled, and `actors=None` so the
    # dispatcher resolves who the organization's actors are from the database rather
    # than from M1's four hardcoded names — a YAML company with different names would
    # otherwise have every inbox message settled as "not-an-actor".
    scheduler: Scheduler | None = None
    dispatcher: Dispatcher | None = None
    if settings.conductor_enabled:
        service = RunService(uow, settings=settings)
        scheduler = Scheduler(uow, service, settings=settings)
        dispatcher = Dispatcher(uow, service, settings=settings, actors=None)

    async with checkpointer(settings) as saver:
        worker = Worker(uow, streams, settings=settings, checkpointer=saver)
        memory_worker: MemoryWorker | None = None
        if settings.memory_enabled:
            # Built against the worker's *own* ModelGateway, then attached — see
            # `Worker.attach_memory` for why the order is this way round.
            memory = MemorySubsystem(uow, worker.executor.models, settings=settings)
            await memory.setup()
            worker.attach_memory(memory)
            memory_worker = MemoryWorker(uow, streams, memory.service, settings=settings)
        await worker.setup()

        loop = asyncio.get_running_loop()
        stopping = asyncio.Event()

        def _stop() -> None:
            log.info("worker.shutdown_requested")
            stopping.set()
            worker.stop()
            relay.stop()
            reaper.stop()
            sweeper.stop()
            if scheduler is not None:
                scheduler.stop()
            if dispatcher is not None:
                dispatcher.stop()
            if memory_worker is not None:
                memory_worker.stop()

        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, _stop)

        log.info(
            "worker.started",
            worker_id=str(worker.worker_id),
            conductor=settings.conductor_enabled,
        )
        tasks = [
            asyncio.create_task(worker.run_forever(), name="worker"),
            asyncio.create_task(relay.run_forever(), name="relay"),
            asyncio.create_task(reaper.run_forever(), name="reaper"),
            asyncio.create_task(sweeper.run_forever(), name="sweeper"),
        ]
        if scheduler is not None and dispatcher is not None:
            tasks.append(asyncio.create_task(scheduler.run_forever(), name="scheduler"))
            tasks.append(asyncio.create_task(dispatcher.run_forever(), name="dispatcher"))
        if memory_worker is not None:
            tasks.append(asyncio.create_task(memory_worker.run_forever(), name="memory"))
        await stopping.wait()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    await streams.close()
    await dispose_engines()
    log.info("worker.stopped")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
