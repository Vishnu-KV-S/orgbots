"""Routines, as the graph and the API see them: saving one, and when it fires next.

A person saves a routine from the bot's Details (`/v1/bots/{id}/routines`); a bot saves
one when its person asks for recurring work in the conversation (`save_routine`). Both
go through `RoutineService`, so both get the same checks — a cron that parses, a
timezone that exists, at least five minutes between firings, at most fifty routines a
bot — and the same `next_fire_at`, which is all the runner reads.

**A bot may only schedule its own work, and only on its person's word.** The graph
refuses `save_routine` on a turn a routine, an event or another bot started: a routine
that creates routines is a loop with a clock in it, and an event is written by whoever
can reach a webhook. That check is the graph's; this module trusts its caller.

Nothing here starts a run. Firing is `runtime.runtime.routines`, one layer up, because
starting a run is `RunService`'s and only the runtime layer may call it.
"""

from __future__ import annotations

import datetime as dt
import itertools
import secrets
import uuid
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from runtime.domain.errors import SpecError
from runtime.domain.routines import (
    MAX_ROUTINES_PER_BOT,
    MIN_SPACING,
    RoutineDraft,
    RoutineError,
    RoutineSpec,
    routine_id_for,
)
from runtime.org.cron import CronExpr, parse_cron
from runtime.persistence.repositories.bots import BotRow
from runtime.persistence.repositories.routines import RoutineRow
from runtime.persistence.uow import UnitOfWork, UnitOfWorkFactory

HORIZON_DAYS = 400
"""How far ahead `next_fire` looks. A yearly routine fires inside it; `0 0 30 2 *`
(30 February) does not, and is refused as a routine that never fires."""

_DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]


LEGACY_ZONES = {
    "Asia/Calcutta": "Asia/Kolkata",
    "Asia/Saigon": "Asia/Ho_Chi_Minh",
    "Asia/Katmandu": "Asia/Kathmandu",
    "Asia/Rangoon": "Asia/Yangon",
    "Asia/Dacca": "Asia/Dhaka",
    "Asia/Thimbu": "Asia/Thimphu",
    "Asia/Ulan_Bator": "Asia/Ulaanbaatar",
    "Europe/Kiev": "Europe/Kyiv",
    "Atlantic/Faeroe": "Atlantic/Faroe",
    "America/Godthab": "America/Nuuk",
    "America/Buenos_Aires": "America/Argentina/Buenos_Aires",
    "America/Indianapolis": "America/Indiana/Indianapolis",
    "Pacific/Truk": "Pacific/Chuuk",
    "Pacific/Ponape": "Pacific/Pohnpei",
    "US/Eastern": "America/New_York",
    "US/Central": "America/Chicago",
    "US/Mountain": "America/Denver",
    "US/Pacific": "America/Los_Angeles",
    "Etc/UTC": "UTC",
    "GMT": "UTC",
}
"""Old names browsers still report — Chrome says `Asia/Calcutta` — that newer tzdata
ships only in a legacy package (Debian's `tzdata-legacy`), so `ZoneInfo` cannot open
them on a current system. Mapped to the names that replaced them, and stored that way."""


def canonical_zone(timezone: str) -> str:
    """The zone's current IANA name, or `RoutineError` if there is no such zone."""
    name = timezone.strip()
    for candidate in (name, LEGACY_ZONES.get(name, "")):
        if not candidate:
            continue
        try:
            ZoneInfo(candidate)
        except (ZoneInfoNotFoundError, ValueError):
            continue
        return candidate
    raise RoutineError(f"{timezone!r} is not a timezone; use an IANA name like Europe/London")


def zone(timezone: str) -> ZoneInfo:
    return ZoneInfo(canonical_zone(timezone))


def check_schedule(cron: str, timezone: str) -> CronExpr:
    """Parse and vet a routine's schedule, or raise `RoutineError` saying what is wrong."""
    zone(timezone)
    try:
        expr = parse_cron(cron.strip())
    except SpecError as exc:
        raise RoutineError(str(exc)) from exc
    gap = smallest_gap(expr)
    if gap < MIN_SPACING:
        minutes = int(gap.total_seconds() // 60)
        raise RoutineError(
            f"{cron!r} fires {minutes} minute(s) apart; routines need at least "
            f"{int(MIN_SPACING.total_seconds() // 60)} minutes between runs"
        )
    if next_fire(expr, dt.datetime.now(dt.UTC), timezone) is None:
        raise RoutineError(f"{cron!r} never fires (no such date within a year)")
    return expr


def smallest_gap(expr: CronExpr) -> dt.timedelta:
    """The shortest time between two firings on one day, or across midnight.

    Conservative across midnight: it assumes the next day matches too, so a schedule
    is never let through that could fire too close together on some pair of days.
    """
    times = sorted(h * 60 + m for h in expr.hours for m in expr.minutes)
    gaps = [b - a for a, b in itertools.pairwise(times)]
    gaps.append(24 * 60 - times[-1] + times[0])
    return dt.timedelta(minutes=min(gaps))


def _day_matches(expr: CronExpr, day: dt.date) -> bool:
    if day.month not in expr.months:
        return False
    dom_ok = day.day in expr.days_of_month
    dow_ok = ((day.weekday() + 1) % 7) in expr.days_of_week
    if expr.dom_restricted and expr.dow_restricted:
        return dom_ok or dow_ok
    return dom_ok and dow_ok


def next_fire(expr: CronExpr, after: dt.datetime, timezone: str) -> dt.datetime | None:
    """The first occurrence strictly after `after`, as UTC — a day at a time, so a
    monthly routine costs a few dozen checks rather than a minute-by-minute scan."""
    tz = zone(timezone)
    times = sorted((h, m) for h in expr.hours for m in expr.minutes)
    day = after.astimezone(tz).date()
    for _ in range(HORIZON_DAYS + 1):
        if _day_matches(expr, day):
            for h, m in times:
                local = dt.datetime(day.year, day.month, day.day, h, m, tzinfo=tz)
                at = local.astimezone(dt.UTC)
                if at > after:
                    return at
        day += dt.timedelta(days=1)
    return None


def describe(cron: str | None, timezone: str) -> str:
    """A schedule as a person says it, for the common shapes; the cron otherwise."""
    if not cron:
        return ""
    parts = cron.split()
    if len(parts) != 5:
        return cron
    minute, hour, dom, month, dow = parts
    tz = "" if timezone == "UTC" else f" ({timezone})"
    if minute.startswith("*/") and hour == dom == month == dow == "*":
        return f"Every {minute[2:]} minutes"
    if minute.isdigit() and hour == "*" and dom == month == dow == "*":
        return f"Every hour at :{int(minute):02d}"
    if not (minute.isdigit() and hour.isdigit()):
        return f"{cron}{tz}"
    at = f"{int(hour):02d}:{int(minute):02d}{tz}"
    if dom == month == "*":
        if dow == "*":
            return f"Every day at {at}"
        if dow == "1-5":
            return f"Weekdays at {at}"
        if dow in ("0,6", "6,0"):
            return f"Weekends at {at}"
        if dow.isdigit():
            return f"Every {_DAYS[int(dow) % 7]} at {at}"
        if all(d.isdigit() for d in dow.split(",")):
            names = ", ".join(_DAYS[int(d) % 7][:3] for d in dow.split(","))
            return f"{names} at {at}"
    if dom.isdigit() and month == "*" and dow == "*":
        return f"Monthly on day {int(dom)} at {at}"
    return f"{cron}{tz}"


def new_token() -> str:
    """An event routine's URL secret. Whoever has the URL can start the routine, so it
    is unguessable, and a person can rotate it by recreating the routine."""
    return secrets.token_urlsafe(24)


@dataclass(frozen=True, slots=True)
class Saved:
    routine: RoutineRow
    outcome: str
    """`created`, `updated` or `unchanged`."""


class RoutineService:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow = uow_factory

    async def for_bot(self, bot_id: uuid.UUID) -> list[RoutineRow]:
        async with self._uow() as uow:
            return await uow.routines.for_bot(bot_id)

    async def get(self, routine_id: uuid.UUID) -> RoutineRow | None:
        async with self._uow() as uow:
            return await uow.routines.get(routine_id)

    # --- a person's routines ---------------------------------------------------------

    async def create(
        self,
        bot: BotRow,
        spec: RoutineSpec,
        *,
        routine_id: uuid.UUID | None = None,
        created_by_kind: str = "person",
        created_by_bot_id: uuid.UUID | None = None,
    ) -> RoutineRow:
        rid = routine_id or uuid.uuid4()
        async with self._uow.transaction() as uow:
            existing = await uow.routines.get(rid)
            if existing is not None:
                return existing
            await self._create(
                uow,
                bot,
                spec,
                rid,
                created_by_kind=created_by_kind,
                created_by_bot_id=created_by_bot_id,
            )
            row = await uow.routines.get(rid)
        assert row is not None
        return row

    async def _create(
        self,
        uow: UnitOfWork,
        bot: BotRow,
        spec: RoutineSpec,
        rid: uuid.UUID,
        *,
        created_by_kind: str,
        created_by_bot_id: uuid.UUID | None,
    ) -> None:
        if await uow.routines.count_for_bot(bot.id) >= MAX_ROUTINES_PER_BOT:
            raise RoutineError(
                f"{bot.name} already has {MAX_ROUTINES_PER_BOT} routines; change or delete "
                "one instead"
            )
        if await uow.routines.by_name(bot.id, spec.name) is not None:
            raise RoutineError(f"{bot.name} already has a routine called {spec.name!r}")
        next_at = _next_for(spec)
        await uow.routines.create(
            rid,
            organization_id=bot.organization_id,
            bot_id=bot.id,
            name=spec.name.strip(),
            instruction=spec.instruction.strip(),
            kind=spec.kind,
            cron=spec.cron.strip() if spec.kind == "schedule" and spec.cron else None,
            timezone=canonical_zone(spec.timezone),
            source=spec.source if spec.kind == "event" else None,
            match=spec.match.model_dump(),
            token=new_token() if spec.kind == "event" else None,
            inputs=spec.inputs,
            output=spec.output,
            approval=spec.approval,
            when_missing=spec.when_missing,
            active=spec.active,
            created_by_kind=created_by_kind,
            created_by_bot_id=created_by_bot_id,
            next_fire_at=next_at,
        )

    async def update(self, routine: RoutineRow, spec: RoutineSpec) -> RoutineRow:
        """Replace a routine's definition. The kind, and an event routine's URL, stay."""
        if spec.kind != routine.kind:
            raise RoutineError("a routine cannot change between a schedule and an event")
        async with self._uow.transaction() as uow:
            same_name = await uow.routines.by_name(routine.bot_id, spec.name)
            if same_name is not None and same_name.id != routine.id:
                raise RoutineError(f"there is already a routine called {spec.name!r}")
            await uow.routines.update(
                routine.id,
                {
                    "name": spec.name.strip(),
                    "instruction": spec.instruction.strip(),
                    "cron": spec.cron.strip() if spec.kind == "schedule" and spec.cron else None,
                    "timezone": canonical_zone(spec.timezone),
                    "source": spec.source if spec.kind == "event" else None,
                    "match": spec.match.model_dump(),
                    "inputs": spec.inputs,
                    "output": spec.output,
                    "approval": spec.approval,
                    "when_missing": spec.when_missing,
                    "active": spec.active,
                    # From now: a schedule moved to 09:00 does not fire the 08:00 it
                    # just stopped being, and a resumed routine does not make up the
                    # firings it was paused through.
                    "next_fire_at": _next_for(spec),
                    "last_evaluated_at": dt.datetime.now(dt.UTC),
                },
            )
            row = await uow.routines.get(routine.id)
        assert row is not None
        return row

    async def delete(self, routine_id: uuid.UUID) -> None:
        async with self._uow.transaction() as uow:
            await uow.routines.soft_delete(routine_id)

    # --- a bot's own -----------------------------------------------------------------

    async def save_draft(
        self, bot: BotRow, draft: RoutineDraft, *, run_id: object, step: int
    ) -> Saved:
        """A bot's `save_routine`: create by name, or change the named one.

        Idempotent per `(run, step)` for a create — the id is derived — and an update
        writes the same values twice to the same effect.
        """
        current = None
        async with self._uow() as uow:
            current = await uow.routines.by_name(bot.id, draft.name)
        if current is None:
            if not (draft.instruction or "").strip():
                raise RoutineError("a new routine needs `instruction` — what to do each time")
            if not (draft.cron or "").strip():
                raise RoutineError("a new routine needs `cron` — when, e.g. '0 8 * * 1-5'")
            spec = RoutineSpec(
                name=draft.name,
                instruction=draft.instruction or "",
                cron=draft.cron,
                timezone=draft.timezone or "UTC",
                output=draft.output or "",
                active=True if draft.active is None else draft.active,
            )
            rid = routine_id_for(run_id, step)
            row = await self.create(
                bot, spec, routine_id=rid, created_by_kind="bot", created_by_bot_id=bot.id
            )
            return Saved(row, "created")
        if current.kind != "schedule":
            raise RoutineError(
                f"{current.name!r} is started by events; only your person can change it"
            )
        spec = RoutineSpec(
            name=current.name,
            instruction=draft.instruction or current.instruction,
            kind="schedule",
            cron=draft.cron or current.cron,
            timezone=draft.timezone or current.timezone,
            inputs=current.inputs,
            output=current.output if draft.output is None else draft.output,
            approval=current.approval,
            when_missing=current.when_missing,
            active=current.active if draft.active is None else draft.active,
        )
        if (
            spec.instruction == current.instruction
            and spec.cron == current.cron
            and spec.timezone == current.timezone
            and spec.output == current.output
            and spec.active == current.active
        ):
            return Saved(current, "unchanged")
        return Saved(await self.update(current, spec), "updated")

    async def delete_named(self, bot: BotRow, name: str) -> RoutineRow | None:
        async with self._uow.transaction() as uow:
            row = await uow.routines.by_name(bot.id, name)
            if row is not None:
                await uow.routines.soft_delete(row.id)
        return row


def _next_for(spec: RoutineSpec) -> dt.datetime | None:
    if spec.kind != "schedule":
        zone(spec.timezone)
        return None
    if not (spec.cron or "").strip():
        raise RoutineError("a scheduled routine needs a cron schedule")
    expr = check_schedule(spec.cron or "", spec.timezone)
    return next_fire(expr, dt.datetime.now(dt.UTC), spec.timezone)


def render_routines(routines: list[RoutineRow], now: dt.datetime) -> str:
    """The bot's routines, for its system prompt — so "do you have a routine for that?"
    has an answer, and a change names a routine that exists."""
    if not routines:
        return ""
    lines = ["Your routines (work that starts on its own):"]
    for r in routines:
        when = (
            describe(r.cron, r.timezone)
            if r.kind == "schedule"
            else f"when a {r.source} event arrives"
        )
        state = "active" if r.active else "paused"
        nxt = ""
        if r.active and r.next_fire_at is not None:
            hours = (r.next_fire_at - now).total_seconds() / 3600
            nxt = f", next in {hours:.0f}h" if hours >= 1 else ", next within the hour"
        lines.append(f"- {r.name}: {when} [{state}{nxt}] — {r.instruction[:160]}")
    return "\n".join(lines)
