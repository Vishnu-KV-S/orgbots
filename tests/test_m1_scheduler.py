"""T18, T19 — cron dedupe and catch-up, plus the parser underneath them.

Both tests are about a scheduler that is *not* the only one running, or *not*
running when it should have been. Those are the two states a scheduler spends its
interesting time in, and both of them produce duplicate or multiplied work if the
dedupe is wrong.

The cron parser gets its own tests because it is hand-written rather than a
dependency. The place a hand-written cron goes wrong is not `0 8 * * 1` — it is the
POSIX day-of-month/day-of-week rule and the off-by-one between Python's Monday=0 and
cron's Sunday=0, so those are what is tested.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from runtime.domain.enums import CatchupPolicy
from runtime.domain.errors import SpecError
from runtime.domain.ids import TriggerId
from runtime.org.cron import parse_cron
from runtime.org.department import ANALYTICS, HEAD, trigger_id_for
from runtime.persistence.repositories.triggers import trigger_key
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.runtime.run_service import RunService
from runtime.runtime.scheduler import Scheduler
from runtime.settings import Settings
from tests.conftest_m1 import build_m1, new_org

pytestmark = pytest.mark.integration

MON_0800 = dt.datetime(2026, 8, 17, 8, 0, tzinfo=dt.UTC)


# --- the parser ---------------------------------------------------------------------


def test_the_two_schedules_m1_actually_uses() -> None:
    monday_plan = parse_cron("0 8 * * 1")
    assert monday_plan.matches(MON_0800)
    assert not monday_plan.matches(MON_0800 + dt.timedelta(minutes=1))
    assert not monday_plan.matches(MON_0800 + dt.timedelta(days=1))
    assert monday_plan.matches(MON_0800 + dt.timedelta(days=7))

    friday_summary = parse_cron("0 17 * * 5")
    assert friday_summary.matches(dt.datetime(2026, 8, 21, 17, 0, tzinfo=dt.UTC))
    assert not friday_summary.matches(dt.datetime(2026, 8, 21, 16, 0, tzinfo=dt.UTC))


def test_cron_sunday_is_zero_not_python_monday_is_zero() -> None:
    """The off-by-one that would fire everything a day early.

    Python's `weekday()` is Monday=0; cron's day-of-week is Sunday=0. A schedule
    written `* * * * 0` means Sunday and must not match Monday.
    """
    sunday_only = parse_cron("0 0 * * 0")
    assert sunday_only.matches(dt.datetime(2026, 8, 16, 0, 0, tzinfo=dt.UTC))  # Sunday
    assert not sunday_only.matches(dt.datetime(2026, 8, 17, 0, 0, tzinfo=dt.UTC))  # Monday


def test_the_posix_dom_or_dow_rule() -> None:
    """When both day fields are restricted, POSIX says match *either*.

    M1 uses neither in combination, but a rule implemented backwards fires on the
    wrong days and looks like nothing is wrong until somebody checks a calendar.
    """
    both = parse_cron("0 0 1 * 5")  # the 1st, OR any Friday
    assert both.matches(dt.datetime(2026, 9, 1, 0, 0, tzinfo=dt.UTC))  # a Tuesday 1st
    assert both.matches(dt.datetime(2026, 9, 4, 0, 0, tzinfo=dt.UTC))  # a Friday
    assert not both.matches(dt.datetime(2026, 9, 2, 0, 0, tzinfo=dt.UTC))

    dom_only = parse_cron("0 0 1 * *")
    assert dom_only.matches(dt.datetime(2026, 9, 1, 0, 0, tzinfo=dt.UTC))
    assert not dom_only.matches(dt.datetime(2026, 9, 4, 0, 0, tzinfo=dt.UTC))


def test_ranges_lists_and_steps() -> None:
    expr = parse_cron("0,30 9-17/4 * * 1-5")
    assert expr.matches(dt.datetime(2026, 8, 17, 9, 30, tzinfo=dt.UTC))
    assert expr.matches(dt.datetime(2026, 8, 17, 13, 0, tzinfo=dt.UTC))
    assert expr.matches(dt.datetime(2026, 8, 17, 17, 0, tzinfo=dt.UTC))
    assert not expr.matches(dt.datetime(2026, 8, 17, 11, 0, tzinfo=dt.UTC))
    assert not expr.matches(dt.datetime(2026, 8, 15, 9, 0, tzinfo=dt.UTC))  # Saturday


@pytest.mark.parametrize(
    "expression",
    ["0 8 * *", "0 8 * * 1 2", "60 8 * * 1", "0 8 32 * *", "0 8 * * MON", "0 8 ? * 1"],
)
def test_a_malformed_cron_is_refused_at_parse_time(expression: str) -> None:
    """Triggers are registered at startup, so this is a process that will not boot
    rather than a schedule that silently never fires."""
    with pytest.raises(SpecError):
        parse_cron(expression)


def test_occurrences_is_half_open_at_the_bottom() -> None:
    """Re-evaluating from `last_fired` must not re-emit what was already handled."""
    expr = parse_cron("0 8 * * *")
    got = expr.occurrences(MON_0800, MON_0800 + dt.timedelta(days=2))
    assert got == [MON_0800 + dt.timedelta(days=1), MON_0800 + dt.timedelta(days=2)]


def test_the_catchup_window_is_bounded() -> None:
    """A trigger whose cursor is ancient enumerates a week, not a decade."""
    expr = parse_cron("0 8 * * *")
    got = expr.occurrences(MON_0800 - dt.timedelta(days=400), MON_0800)
    assert len(got) <= 8


def test_trigger_key_is_stable_across_clock_skew_within_a_minute() -> None:
    """Two schedulers a few hundred milliseconds apart must agree on the key, or
    the dedupe does nothing at all."""
    trigger_id = uuid.uuid4()
    a = trigger_key(trigger_id, MON_0800)
    b = trigger_key(trigger_id, MON_0800 + dt.timedelta(milliseconds=900))
    c = trigger_key(trigger_id, MON_0800.astimezone(dt.timezone(dt.timedelta(hours=5))))
    assert a == b == c
    assert a != trigger_key(trigger_id, MON_0800 + dt.timedelta(minutes=1))


# --- T18 / T19 ----------------------------------------------------------------------


@pytest_asyncio.fixture
async def m1(settings: Settings) -> AsyncIterator[object]:
    async for runtime in build_m1(settings, new_org()):
        yield runtime


async def test_t18_two_schedulers_on_the_same_minute_produce_one_run(
    m1, uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """The primary key is the leader election.

    Five schedulers, one cron minute. `trigger_fires.trigger_key` lets exactly one
    INSERT through and the other four do nothing — no lock, no coordination.
    """
    service = RunService(m1.uow, settings=settings)
    schedulers = [Scheduler(m1.uow, service, settings=settings) for _ in range(5)]

    results = await asyncio.gather(*(s.tick(MON_0800) for s in schedulers))

    fired = [f for batch in results for f in batch if not f.skipped]
    plan_fires = [f for f in fired if f.trigger == "weekly-plan"]
    assert len(plan_fires) == 1, f"expected one weekly-plan fire, got {len(plan_fires)}"

    async with uow_factory() as uow:
        rows = await uow.triggers.fires_for(trigger_id_for(m1.organization_id, "weekly-plan"))
    unskipped = [r for r in rows if not r["skipped"]]
    assert len(unskipped) == 1
    assert unskipped[0]["run_id"] is not None, "the fire row records which run it started"

    # Every trigger due this minute is contended by all five schedulers, so four
    # lose each one. Asserted exactly rather than as "> 0": a dedupe that worked by
    # accident — because only one scheduler ever reached the insert — would pass a
    # loose assertion and fail the moment the timing changed.
    from runtime.org.department import TRIGGERS

    due = {f.trigger for batch in results for f in batch}
    assert sum(s.stats.lost_races for s in schedulers) == len(due) * 4
    assert due <= {t.key for t in TRIGGERS}


async def test_t19_48h_of_downtime_under_skip_produces_one_run_not_48(
    m1, uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """Waking after an outage must not fire the backlog.

    An hourly trigger, down for 48 hours. Under `skip` exactly one run starts and
    47 occurrences are recorded as skipped — recorded, not discarded, because
    "we missed 47" is a thing an operator needs to be able to find out.
    """
    trigger_id = TriggerId(uuid.uuid4())
    async with uow_factory.transaction() as uow:
        await uow.triggers.upsert(
            trigger_id,
            organization_id=m1.organization_id,
            key="hourly-thing",
            actor_name=ANALYTICS,
            cron="0 * * * *",
            payload={"mode": "weekly_metrics"},
            catchup_policy=CatchupPolicy.SKIP,
        )
        await uow.triggers.mark_evaluated(trigger_id, MON_0800 - dt.timedelta(hours=48))

    service = RunService(m1.uow, settings=settings)
    scheduler = Scheduler(m1.uow, service, settings=settings)
    fired = await scheduler.tick(MON_0800)

    hourly = [f for f in fired if f.trigger == "hourly-thing"]
    ran = [f for f in hourly if not f.skipped]
    skipped = [f for f in hourly if f.skipped]

    assert len(ran) == 1, f"skip policy must run once, ran {len(ran)}"
    assert ran[0].scheduled_for == MON_0800, "and it runs the most recent occurrence"
    assert len(skipped) == 47, f"and record the rest; recorded {len(skipped)}"

    async with uow_factory() as uow:
        rows = await uow.triggers.fires_for(trigger_id)
    assert len([r for r in rows if r["run_id"] is not None]) == 1
    assert len([r for r in rows if r["skipped"]]) == 47


async def test_the_all_policy_does_fire_every_missed_occurrence(
    m1, uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """The other branch, so `skip` is a choice rather than the only thing implemented."""
    trigger_id = TriggerId(uuid.uuid4())
    async with uow_factory.transaction() as uow:
        await uow.triggers.upsert(
            trigger_id,
            organization_id=m1.organization_id,
            key="accumulating-thing",
            actor_name=ANALYTICS,
            cron="0 * * * *",
            payload={"mode": "weekly_metrics"},
            catchup_policy=CatchupPolicy.ALL,
        )
        await uow.triggers.mark_evaluated(trigger_id, MON_0800 - dt.timedelta(hours=5))

    scheduler = Scheduler(m1.uow, RunService(m1.uow, settings=settings), settings=settings)
    fired = [f for f in await scheduler.tick(MON_0800) if f.trigger == "accumulating-thing"]

    assert len(fired) == 5
    assert all(not f.skipped for f in fired)
    assert len({f.run_id for f in fired}) == 5, "five distinct runs"


async def test_a_second_tick_in_the_same_minute_does_nothing(m1, settings: Settings) -> None:
    """Idempotent under the ordinary case as well as the racing one."""
    scheduler = Scheduler(m1.uow, RunService(m1.uow, settings=settings), settings=settings)
    first = await scheduler.tick(MON_0800)
    second = await scheduler.tick(MON_0800)

    assert [f for f in first if not f.skipped], "the first tick fires"
    assert second == [], "the second does not"


async def test_a_fresh_trigger_fires_once_rather_than_a_backlog(m1, settings: Settings) -> None:
    """First boot must not send a week of plans.

    `last_evaluated_at` is null on a newly installed trigger; enumeration falls
    back to the previous occurrence, not to the beginning of time.
    """
    scheduler = Scheduler(m1.uow, RunService(m1.uow, settings=settings), settings=settings)
    fired = await scheduler.tick(MON_0800)

    plan = [f for f in fired if f.trigger == "weekly-plan"]
    assert len(plan) == 1
    assert not plan[0].skipped


async def test_installed_triggers_match_the_department(m1, uow_factory) -> None:
    async with uow_factory() as uow:
        triggers = await uow.triggers.active(m1.organization_id)
    by_key = {t.key: t for t in triggers}
    assert set(by_key) == {"weekly-plan", "publish-gate", "weekly-metrics", "weekly-summary"}
    assert by_key["weekly-plan"].actor_name == HEAD
    assert by_key["weekly-plan"].cron == "0 8 * * 1"
    assert by_key["weekly-metrics"].actor_name == ANALYTICS
    assert all(t.catchup_policy is CatchupPolicy.SKIP for t in triggers)
