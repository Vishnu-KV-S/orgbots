"""The scheduler — cron fires become runs.

Two tests define this module and both are about what happens when things go wrong
rather than when they go right.

**T18: two schedulers, same cron minute → one run.** Both compute
`trigger_key = H(trigger_id, scheduled_for_utc)`, both try to insert a
`trigger_fires` row, one wins the primary key and the other does nothing. There is
no leader election, no advisory lock and no "is it my turn" check — the primary key
*is* the election. That is why `trigger_fires` has no surrogate id: the natural key
is the identity.

**T19: 48h downtime + `skip` → one run, not 48.** On waking, the scheduler
enumerates every occurrence it missed. Under `skip` it claims a fire row for *all*
of them — marking the older ones `skipped = true` — and starts a run only for the
most recent. The skipped rows are the point: "we missed 47 plans" is answerable
rather than merely absent, and the next tick will not re-enumerate them because they
are already claimed.

The fire row is written **before** the run is started, and the run id is stamped on
afterwards. A scheduler that dies in between leaves a fire row with a null `run_id`
— visible, diagnosable, and crucially not a duplicate. The reverse ordering would
leave a run nobody knows fired.

`last_evaluated_at` is a cursor, not a lock. If it is null — a fresh trigger, or one
whose row was restored from a backup — enumeration falls back to the previous
occurrence rather than to the beginning of time, and `MAX_CATCHUP_WINDOW` bounds it
regardless.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
from dataclasses import dataclass, field

from runtime.domain.enums import CatchupPolicy
from runtime.domain.ids import CorrelationId, OrganizationId, TriggerId
from runtime.domain.specs import StartRunRequest
from runtime.observability.logging import get_logger
from runtime.org.cron import CronExpr, parse_cron
from runtime.org.department import TRIGGERS, correlation_for_week, trigger_id_for, week_of
from runtime.persistence.repositories.triggers import TriggerRow, trigger_key
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.runtime.run_service import RunService
from runtime.settings import Settings, get_settings

log = get_logger("runtime.scheduler")


@dataclass
class ScheduleStats:
    evaluated: int = 0
    fired: int = 0
    skipped: int = 0
    lost_races: int = 0
    per_trigger: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Fired:
    trigger_key: str
    trigger: str
    scheduled_for: dt.datetime
    run_id: str | None
    skipped: bool


class Scheduler:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        service: RunService,
        *,
        settings: Settings | None = None,
    ) -> None:
        self._uow = uow_factory
        self._service = service
        self._settings = settings or get_settings()
        self._stopping = asyncio.Event()
        self._compiled: dict[str, CronExpr] = {}
        self.stats = ScheduleStats()

    async def tick(self, now: dt.datetime | None = None) -> list[Fired]:
        """Evaluate every active trigger once. Returns what it did."""
        moment = (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC)
        async with self._uow() as uow:
            triggers = await uow.triggers.active()

        fired: list[Fired] = []
        for trigger in triggers:
            self.stats.evaluated += 1
            fired.extend(await self._evaluate(trigger, moment))
        return fired

    async def _evaluate(self, trigger: TriggerRow, now: dt.datetime) -> list[Fired]:
        expr = self._compiled.get(trigger.cron)
        if expr is None:
            expr = parse_cron(trigger.cron)
            self._compiled[trigger.cron] = expr

        since = trigger.last_evaluated_at
        if since is None:
            # A fresh trigger, or one restored from a backup. Look back exactly one
            # occurrence rather than to the beginning of time: firing an entire
            # backlog on first boot is how a new deployment sends 47 plans.
            previous = expr.previous(now, timezone=trigger.timezone)
            since = (previous - dt.timedelta(minutes=1)) if previous else now

        due = expr.occurrences(since, now, timezone=trigger.timezone)
        if not due:
            async with self._uow.transaction() as uow:
                await uow.triggers.mark_evaluated(trigger.id, now)
            return []

        # Under `skip`, only the most recent occurrence runs; the rest are claimed
        # and recorded as skipped so they are neither re-enumerated nor forgotten.
        to_run = due if trigger.catchup_policy is CatchupPolicy.ALL else due[-1:]
        runnable = set(to_run)

        results: list[Fired] = []
        for occurrence in due:
            skipped = occurrence not in runnable
            result = await self._fire(trigger, occurrence, skipped=skipped)
            if result is not None:
                results.append(result)

        async with self._uow.transaction() as uow:
            await uow.triggers.mark_evaluated(trigger.id, now)

        if len(due) > len(to_run):
            log.warning(
                "scheduler.caught_up",
                trigger=trigger.key,
                missed=len(due),
                ran=len(to_run),
                policy=trigger.catchup_policy.value,
            )
        return results

    async def _fire(
        self, trigger: TriggerRow, occurrence: dt.datetime, *, skipped: bool
    ) -> Fired | None:
        org_id = OrganizationId(trigger.organization_id)
        correlation = CorrelationId(correlation_for_week(org_id, week_of(occurrence)))

        async with self._uow.transaction() as uow:
            key = await uow.triggers.claim_fire(
                trigger.id, occurrence, skipped=skipped, correlation_id=correlation
            )
        if key is None:
            # Another scheduler holds this occurrence. T18.
            self.stats.lost_races += 1
            return None

        if skipped:
            self.stats.skipped += 1
            return Fired(
                trigger_key=key,
                trigger=trigger.key,
                scheduled_for=occurrence,
                run_id=None,
                skipped=True,
            )

        result = await self._service.start_run(
            StartRunRequest(
                organization_id=org_id,
                actor_name=trigger.actor_name,
                input={
                    **trigger.input,
                    "scheduled_for": occurrence.isoformat(),
                    "week_of": week_of(occurrence).isoformat(),
                },
                # The fire key doubles as the run's idempotency key, so cron dedupe
                # and run dedupe are the same constraint rather than two that could
                # disagree.
                idempotency_key=f"trigger:{key}",
                correlation_id=str(correlation),
            )
        )
        async with self._uow.transaction() as uow:
            await uow.triggers.attach_run(key, result.run_id)

        self.stats.fired += 1
        self.stats.per_trigger[trigger.key] = self.stats.per_trigger.get(trigger.key, 0) + 1
        log.info(
            "scheduler.fired",
            trigger=trigger.key,
            actor=trigger.actor_name,
            scheduled_for=occurrence.isoformat(),
            run_id=str(result.run_id),
            created=result.created,
        )
        return Fired(
            trigger_key=key,
            trigger=trigger.key,
            scheduled_for=occurrence,
            run_id=str(result.run_id),
            skipped=False,
        )

    async def run_forever(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.tick()
            except Exception:
                log.exception("scheduler.loop_error")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stopping.wait(),
                    timeout=self._settings.scheduler_interval_seconds,
                )

    def stop(self) -> None:
        self._stopping.set()


async def install_triggers(
    uow_factory: UnitOfWorkFactory, organization_id: OrganizationId
) -> list[TriggerId]:
    """Register the department's schedule. Idempotent — safe on every boot.

    The cron strings are parsed here, before anything is written, so a malformed
    schedule is a process that refuses to start rather than a trigger that silently
    never fires.
    """
    installed: list[TriggerId] = []
    for spec in TRIGGERS:
        parse_cron(spec.cron)
        async with uow_factory.transaction() as uow:
            trigger_id = await uow.triggers.upsert(
                trigger_id_for(organization_id, spec.key),
                organization_id=organization_id,
                key=spec.key,
                actor_name=spec.actor_name,
                cron=spec.cron,
                payload=spec.input(),
                catchup_policy=spec.catchup,
            )
        installed.append(trigger_id)
        log.info(
            "scheduler.installed",
            trigger=spec.key,
            cron=spec.cron,
            actor=spec.actor_name,
            mode=spec.mode,
        )
    return installed


__all__ = [
    "Fired",
    "ScheduleStats",
    "Scheduler",
    "install_triggers",
    # M4 re-exports this: `spec validate` parses every cron before an apply writes
    # anything, for the reason `install_triggers` does — a malformed schedule should be
    # a refused apply rather than a trigger that silently never fires.
    "parse_cron",
    "trigger_key",
]
