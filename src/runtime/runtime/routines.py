"""The routine runner — due schedules and queued events become bot turns.

Two halves, each one statement wide where it matters:

**Claim.** A scheduled routine whose `next_fire_at` has passed gets a `queued` firing
for its latest occurrence, keyed `fire_id(routine, occurrence)` — so two runners insert
the same row and the second does nothing, which is the department scheduler's T18
without a lock. Older occurrences missed while nothing was running are written down
once, as `missed`, and not run: the T19 rule, applied because a bot that spends its
morning on yesterday's digests is worse than one that says it missed them. An event
routine is claimed by the API, which writes the same `queued` row from the webhook.

**Start.** The oldest queued firing of each free bot starts its turn
(`BotManager.fire_routine`). Busy bots keep their firings queued — a routine never
supersedes work in progress or expires a card the person has not answered — until
`MAX_WAIT`, after which the firing is skipped with the reason. The firing is claimed by
a conditional update before its run is started, so of two runners exactly one starts
it; the run itself is idempotent on the firing's id as well.

Like the scheduler, this only calls `RunService.start_run` (through `BotManager`); the
run is executed by the worker loop like any other.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
from dataclasses import dataclass, field

from runtime.domain.errors import SpecError
from runtime.domain.routines import MAX_WAIT, RUNS_KEPT, fire_id
from runtime.observability.logging import get_logger
from runtime.org.cron import parse_cron
from runtime.org.routines import next_fire
from runtime.persistence.repositories.routines import RoutineRow, RoutineRunRow
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.runtime.bots import BotManager, BotNotFoundError

log = get_logger("runtime.routines")

TICK_SECONDS = 20.0


@dataclass
class RoutineTick:
    claimed: int = 0
    missed: int = 0
    started: int = 0
    refused: int = 0
    skipped: int = 0
    waiting: list[str] = field(default_factory=list)


class RoutineRunner:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        bots: BotManager,
        *,
        interval_s: float = TICK_SECONDS,
    ) -> None:
        self._uow = uow_factory
        self._bots = bots
        self._interval = interval_s
        self._stopping = asyncio.Event()

    async def tick(self, now: dt.datetime | None = None) -> RoutineTick:
        moment = (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC)
        out = RoutineTick()
        await self.claim(moment, out)
        await self.start(moment, out)
        return out

    # --- claim -------------------------------------------------------------------------

    async def claim(self, now: dt.datetime, out: RoutineTick | None = None) -> None:
        out = out or RoutineTick()
        async with self._uow() as uow:
            due = await uow.routines.due(now)
        for routine in due:
            await self._claim_one(routine, now, out)

    async def _claim_one(self, routine: RoutineRow, now: dt.datetime, out: RoutineTick) -> None:
        try:
            expr = parse_cron(routine.cron or "")
        except SpecError:
            # Saved through `check_schedule`, so this is a row edited by hand. Parked
            # rather than retried every tick.
            log.warning("routine.bad_cron", routine_id=str(routine.id), cron=routine.cron)
            async with self._uow.transaction() as uow:
                await uow.routines.evaluated(routine.id, now=now, next_fire_at=None, fired=False)
            return
        since = routine.last_evaluated_at or (
            (routine.next_fire_at or now) - dt.timedelta(minutes=1)
        )
        due = expr.occurrences(since, now, timezone=routine.timezone)
        upcoming = next_fire(expr, now, routine.timezone)
        claimed = False
        async with self._uow.transaction() as uow:
            if due:
                latest = due[-1]
                claimed = await uow.routines.add_run(
                    fire_id(routine.id, latest.isoformat()),
                    routine_id=routine.id,
                    bot_id=routine.bot_id,
                    trigger="schedule",
                    scheduled_for=latest,
                )
                if len(due) > 1:
                    missed = len(due) - 1
                    await uow.routines.add_run(
                        fire_id(routine.id, f"missed:{latest.isoformat()}"),
                        routine_id=routine.id,
                        bot_id=routine.bot_id,
                        trigger="schedule",
                        scheduled_for=due[0],
                        status="missed",
                        detail=(
                            f"{missed} earlier run{'s' if missed != 1 else ''} missed while "
                            "routines were not running; only the latest one runs"
                        ),
                    )
                    out.missed += missed
            await uow.routines.evaluated(routine.id, now=now, next_fire_at=upcoming, fired=claimed)
        if claimed:
            out.claimed += 1
            log.info("routine.claimed", routine_id=str(routine.id), scheduled_for=str(due[-1]))

    # --- start -------------------------------------------------------------------------

    async def start(self, now: dt.datetime, out: RoutineTick | None = None) -> None:
        out = out or RoutineTick()
        async with self._uow() as uow:
            queued = await uow.routines.queued()
        seen: set[object] = set()
        for fire in queued:
            # One firing per bot per tick, oldest first: a bot runs one turn at a time,
            # and the second firing waits for the first to finish anyway.
            if fire.bot_id in seen:
                continue
            seen.add(fire.bot_id)
            await self._start_one(fire, now, out)

    async def _skip(self, fire: RoutineRunRow, reason: str, out: RoutineTick) -> None:
        async with self._uow.transaction() as uow:
            if await uow.routines.settle_run(fire.id, status="skipped", detail=reason):
                out.skipped += 1
                await uow.routines.prune_runs(fire.routine_id, RUNS_KEPT)
        log.info("routine.skipped", fire_id=str(fire.id), reason=reason)

    async def _start_one(self, fire: RoutineRunRow, now: dt.datetime, out: RoutineTick) -> None:
        async with self._uow() as uow:
            routine = await uow.routines.get(fire.routine_id)
        if routine is None:
            await self._skip(fire, "the routine was deleted", out)
            return
        if not routine.active:
            await self._skip(fire, "the routine was paused", out)
            return
        try:
            bot = await self._bots.get(fire.bot_id)
        except BotNotFoundError:
            await self._skip(fire, "the bot was deleted", out)
            return
        reason = await self._bots.busy(bot)
        if reason is not None:
            if now - fire.created_at > MAX_WAIT:
                hours = int(MAX_WAIT.total_seconds() // 3600)
                await self._skip(fire, f"{bot.name} was {reason} for more than {hours} hours", out)
            else:
                out.waiting.append(f"{bot.name}: {reason}")
            return

        async with self._uow.transaction() as uow:
            if not await uow.routines.settle_run(fire.id, status="started"):
                return  # another runner claimed it
        try:
            sent = await self._bots.fire_routine(routine, fire)
        except Exception as exc:
            log.exception("routine.start_failed", fire_id=str(fire.id))
            async with self._uow.transaction() as uow:
                await uow.routines.finish_run(
                    fire.id, status="refused", run_id=None, detail=f"{type(exc).__name__}: {exc}"
                )
            out.refused += 1
            return
        async with self._uow.transaction() as uow:
            await uow.routines.finish_run(
                fire.id,
                status="started" if sent.admitted else "refused",
                run_id=sent.run_id,
                detail=sent.refusal_reason or "",
            )
            if fire.trigger != "schedule":
                await uow.routines.note_fired(routine.id)
            await uow.routines.prune_runs(routine.id, RUNS_KEPT)
        if sent.admitted:
            out.started += 1
            log.info("routine.started", routine_id=str(routine.id), run_id=str(sent.run_id))
        else:
            out.refused += 1

    # --- loop ---------------------------------------------------------------------------

    async def run_forever(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.tick()
            except Exception:
                log.exception("routine.tick_failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=self._interval)

    def stop(self) -> None:
        self._stopping.set()
