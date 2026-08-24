"""A worker in its own process, so it can be killed for real.

`python -m tests.chaos_worker <run_id> [durability]`

An in-process "crash" — an exception, a cancelled task — is not the failure mode
that matters. Python unwinds it, `finally` blocks run, connections close politely.
The failure that breaks exactly-once is the one where none of that happens: the
process stops between two instructions and the operating system reclaims it.

So T7 kills this for real, with `SIGKILL`, from inside the tool function, in the
window after the effect has landed and before the journal has recorded it.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid

import runtime.graphs.echo_agent
import runtime.handlers  # noqa: F401  registers hasher@1
from runtime.domain.ids import RunId
from runtime.events.stream import RedisStreams
from runtime.graphs.checkpointer import checkpointer
from runtime.persistence.engine import dispose_engines
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.settings import Settings
from runtime.worker.worker import Worker

EXIT_NO_CLAIM = 3


async def _main(run_id: RunId, durability: str) -> int:
    _ = durability  # the RunSpec carries it; this is only for the process title
    settings = Settings()
    uow = UnitOfWorkFactory(settings)
    streams = RedisStreams(settings)
    try:
        async with checkpointer(settings, setup=False) as saver:
            worker = Worker(uow, streams, settings=settings, checkpointer=saver)
            outcome = await worker.run_one(run_id)
            if outcome is None:
                return EXIT_NO_CLAIM
            return 0
    finally:
        await streams.close()
        await dispose_engines()


if __name__ == "__main__":
    target = RunId(uuid.UUID(sys.argv[1]))
    mode = sys.argv[2] if len(sys.argv) > 2 else "sync"
    os.environ.setdefault("RUNTIME_LOG_LEVEL", "WARNING")
    sys.exit(asyncio.run(_main(target, mode)))
