"""The conductor — the loop turning without a human typing `tick`.

`runtime.worker.main` now runs a `Scheduler` and a `Dispatcher` beside the worker.
Neither executes a run; both only call `RunService.start_run`, and the worker picks
the run up from the stream exactly as it does for one the API started. So what has to
be true is not "a run happened" — that is M1's territory and M1 tests it — but the
three things that only became reachable when the loop started running by itself:

1. **Two conductors are safe.** A worker pool is more than one process, and every one
   of them runs a conductor. The `trigger_fires` primary key and `uq_run_idem` are
   what make that a non-event, and both are asserted here through the conductor's own
   objects rather than through the repository, because the wiring is the new part.
2. **`actors=None` resolves recipients from the database.** M1's `ALL_ACTORS` was four
   names in Python; a company defined in YAML has different ones, and under the old
   default every one of its inbox messages was settled as `not-an-actor` and its
   wake-up discarded — silently, permanently, and looking exactly like an idle
   organization.
3. **It is on by default.** A default of off would make
   `docs/MEASUREMENT_PROTOCOL.md` §4's clean run impossible: it wants the long-lived
   processes left alone for two weeks, and with no conductor somebody has to type
   `tick`, which is the intervention that resets the clock.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text

from runtime.domain.enums import ActorKind
from runtime.domain.ids import CorrelationId
from runtime.domain.specs import ActorSpec, Ceilings
from runtime.org.department import ALL_ACTORS, trigger_id_for
from runtime.org.inbox import KIND_TASK_ASSIGNED, InboxService
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.runtime.dispatcher import Dispatcher
from runtime.runtime.run_service import RunService
from runtime.runtime.scheduler import Scheduler
from runtime.settings import Settings
from tests.conftest_m1 import build_m1, new_org

pytestmark = pytest.mark.integration

MON_0800 = dt.datetime(2026, 8, 17, 8, 0, tzinfo=dt.UTC)

OUTSIDE_THE_M1_DEPARTMENT = "growth-lead"
"""Deliberately not one of `ALL_ACTORS`. That is the entire point of the test that
uses it: a company whose head is not called `marketing-head` must still be woken."""

STRANGER_SPEC = ActorSpec(
    name=OUTSIDE_THE_M1_DEPARTMENT,
    kind=ActorKind.DETERMINISTIC_WORKER,
    handler_ref="hasher@1",
    allowed_tools=frozenset(),
    ceilings=Ceilings(max_llm_calls=0, max_tool_calls=0),
)


@pytest_asyncio.fixture
async def m1(settings: Settings) -> AsyncIterator[object]:
    async for runtime in build_m1(settings, new_org()):
        yield runtime


def test_the_conductor_is_on_by_default(settings: Settings) -> None:
    """Changing this default changes whether a deployed runtime does anything at all.

    Off would mean a worker that executes runs nobody creates. §4's clean run needs
    the opposite: processes that run for two weeks with nobody touching them.
    """
    assert settings.conductor_enabled is True


def test_the_scheduler_is_off_by_default(settings: Settings) -> None:
    """Runs start on an instruction — a person, or the run that delegated — not on a
    clock. A department meant to run itself sets `RUNTIME_SCHEDULER_ENABLED=true`."""
    assert settings.scheduler_enabled is False


# --- two conductors ---------------------------------------------------------------


async def test_two_conductors_over_one_due_trigger_start_one_run(
    m1, uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """Every worker process runs a conductor, so "two conductors" is the normal case.

    The dedupe is the `trigger_fires` primary key: both compute
    `H(trigger_id, scheduled_for)`, one INSERT wins and the other does nothing. No
    lock, no leader election.
    """
    service = RunService(m1.uow, settings=settings)
    conductors = [Scheduler(m1.uow, service, settings=settings) for _ in range(2)]

    batches = await asyncio.gather(*(c.tick(MON_0800) for c in conductors))
    fired = [f for batch in batches for f in batch if not f.skipped]
    plan_fires = [f for f in fired if f.trigger == "weekly-plan"]
    assert len(plan_fires) == 1

    async with uow_factory() as uow:
        rows = await uow.triggers.fires_for(trigger_id_for(m1.organization_id, "weekly-plan"))
    unskipped = [r for r in rows if not r["skipped"]]
    assert len(unskipped) == 1
    assert unskipped[0]["run_id"] is not None


async def test_two_conductors_over_one_inbox_message_start_one_run(
    m1, uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """The dispatcher's dedupe is *borrowed*: the run's idempotency key is
    `inbox:{dedupe_key}`, so `uq_run_idem` — the same constraint that makes cron
    dedupe and API retry safe — makes redelivery safe too.

    The loser does not error. Its `claim_delivery` returns False and its `start_run`
    returns the winner's run with `created=False`, which is why the assertion is on
    the number of *runs* rather than on which dispatcher reported success.
    """
    service = RunService(m1.uow, settings=settings)
    inbox = InboxService(m1.uow)
    correlation = CorrelationId(uuid.uuid4())

    await inbox.send(
        organization_id=m1.organization_id,
        kind=KIND_TASK_ASSIGNED,
        recipient=ALL_ACTORS[1],
        correlation_id=correlation,
        key=f"race:{uuid.uuid4()}",
        subject="one message, two dispatchers",
        body={},
    )

    dispatchers = [Dispatcher(m1.uow, service, settings=settings) for _ in range(2)]
    await asyncio.gather(*(d.drain() for d in dispatchers))

    async with uow_factory() as uow:
        runs = (
            await uow.session.execute(
                text("SELECT count(*) FROM runs WHERE organization_id = :org"),
                {"org": m1.organization_id},
            )
        ).scalar_one()
    assert runs == 1, "one message, one run, however many dispatchers saw it"


# --- who counts as an actor -------------------------------------------------------


async def test_a_dispatcher_with_no_actor_list_wakes_an_actor_m1_never_heard_of(
    m1, uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """The bug this fixes is silent and total.

    `ALL_ACTORS` is `('marketing-head', 'research', 'content', 'analytics')`. Any
    other recipient was settled DELIVERED with reason `not-an-actor` and its wake-up
    thrown away — so a company defined in YAML with its own names looked idle forever
    while its inbox drained into nothing.
    """
    from runtime.runtime.bootstrap import Registrar

    assert OUTSIDE_THE_M1_DEPARTMENT not in ALL_ACTORS
    await Registrar(m1.uow).publish_actor(m1.organization_id, STRANGER_SPEC)

    inbox = InboxService(m1.uow)
    await inbox.send(
        organization_id=m1.organization_id,
        kind=KIND_TASK_ASSIGNED,
        recipient=OUTSIDE_THE_M1_DEPARTMENT,
        correlation_id=CorrelationId(uuid.uuid4()),
        key=f"stranger:{uuid.uuid4()}",
        subject="work for somebody M1 never heard of",
        body={},
    )

    service = RunService(m1.uow, settings=settings)
    resolving = Dispatcher(m1.uow, service, settings=settings, actors=None)
    assert await resolving.drain() == 1
    assert resolving.stats.reasons.get("not-an-actor") is None


async def test_an_explicit_actor_tuple_still_means_exactly_what_it_did(
    m1, uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """`actors=None` is the new default for the worker, not a change of meaning for
    the parameter. A caller that names its recipients still gets exactly those."""
    from runtime.runtime.bootstrap import Registrar

    await Registrar(m1.uow).publish_actor(m1.organization_id, STRANGER_SPEC)
    inbox = InboxService(m1.uow)
    await inbox.send(
        organization_id=m1.organization_id,
        kind=KIND_TASK_ASSIGNED,
        recipient=OUTSIDE_THE_M1_DEPARTMENT,
        correlation_id=CorrelationId(uuid.uuid4()),
        key=f"stranger:{uuid.uuid4()}",
        subject="addressed to somebody this dispatcher does not serve",
        body={},
    )

    service = RunService(m1.uow, settings=settings)
    restricted = Dispatcher(m1.uow, service, settings=settings, actors=ALL_ACTORS)
    assert await restricted.drain() == 0
    assert restricted.stats.reasons.get("not-an-actor") == 1
