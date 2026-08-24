"""A five-field cron expression, parsed and evaluated.

Written rather than depended on. M1 needs exactly two schedules — Monday 08:00 and
Friday 16:00/17:00 — and the parts of cron that make a library worth having
(`@reboot`, `L`, `#`, seconds fields, DST-aware "skip or double") are parts M1 must
not use anyway. Ninety lines that fail loudly on anything they do not understand
beat a dependency whose behaviour at a DST boundary is a thing we would have to go
and read.

Supported: `*`, `n`, `a-b`, `a,b,c`, `*/n`, `a-b/n`, in the five standard fields
`minute hour day-of-month month day-of-week`, with `0 = Sunday`. Names are not
supported and `?` is not supported; both raise at parse time, which is at trigger
registration, which is at startup.

The day-of-month / day-of-week rule follows POSIX and it is the one genuinely
surprising thing in cron: when *both* are restricted, a timestamp matches if
*either* matches. M1 uses neither in combination, but implementing the rule
correctly costs three lines and getting it wrong silently would be a schedule that
fires on the wrong days.

Evaluation walks minute by minute. `occurrences()` over a 7-day catch-up window is
10 080 comparisons of five integers — cheaper than the database round trip that
follows it, and it means there is no closed-form "next fire" arithmetic to be
subtly wrong about.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from runtime.domain.errors import SpecError

_FIELD = re.compile(r"^(?:\*|\d+)(?:-\d+)?(?:/\d+)?$")

_RANGES: tuple[tuple[str, int, int], ...] = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day_of_month", 1, 31),
    ("month", 1, 12),
    ("day_of_week", 0, 6),
)

MAX_CATCHUP_WINDOW = dt.timedelta(days=7)
"""How far back `occurrences()` will look when a scheduler has been down.

A bound, not a policy. `CatchupPolicy` decides what to *do* with the occurrences;
this decides how many there can be, so a trigger whose `last_evaluated_at` is null
or ancient enumerates a week rather than a decade.
"""


@dataclass(frozen=True, slots=True)
class CronExpr:
    minutes: frozenset[int]
    hours: frozenset[int]
    days_of_month: frozenset[int]
    months: frozenset[int]
    days_of_week: frozenset[int]
    dom_restricted: bool
    dow_restricted: bool
    source: str

    def matches(self, when: dt.datetime) -> bool:
        """Does this local-time timestamp fall on an occurrence?

        `when` must already be in the trigger's timezone; `occurrences()` does that
        conversion. Passing a UTC timestamp to a 08:00 Europe/London trigger would
        match at the wrong hour for half the year.
        """
        if when.minute not in self.minutes or when.hour not in self.hours:
            return False
        if when.month not in self.months:
            return False
        dom_ok = when.day in self.days_of_month
        # Python: Monday=0. Cron: Sunday=0.
        dow_ok = ((when.weekday() + 1) % 7) in self.days_of_week
        if self.dom_restricted and self.dow_restricted:
            return dom_ok or dow_ok
        return dom_ok and dow_ok

    def occurrences(
        self, after: dt.datetime, until: dt.datetime, *, timezone: str = "UTC"
    ) -> list[dt.datetime]:
        """Every occurrence in `(after, until]`, as UTC, oldest first.

        Half-open at the bottom so that re-evaluating with `after = last_fired`
        does not re-emit the occurrence already handled. Closed at the top so an
        occurrence landing exactly on "now" fires now rather than next tick.
        """
        tz = ZoneInfo(timezone)
        start = max(after, until - MAX_CATCHUP_WINDOW)
        cursor = start.astimezone(dt.UTC).replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
        end = until.astimezone(dt.UTC).replace(second=0, microsecond=0)
        out: list[dt.datetime] = []
        while cursor <= end:
            if self.matches(cursor.astimezone(tz)):
                out.append(cursor)
            cursor += dt.timedelta(minutes=1)
        return out

    def previous(self, before: dt.datetime, *, timezone: str = "UTC") -> dt.datetime | None:
        """The most recent occurrence at or before `before`, within the window."""
        fires = self.occurrences(before - MAX_CATCHUP_WINDOW, before, timezone=timezone)
        return fires[-1] if fires else None


def parse_cron(expression: str) -> CronExpr:
    """Parse, or raise `SpecError`.

    Raising at parse time matters: triggers are registered at startup, so a
    malformed cron is a process that refuses to start rather than a schedule that
    silently never fires.
    """
    fields = expression.split()
    if len(fields) != 5:
        raise SpecError(
            f"cron {expression!r} has {len(fields)} fields; expected 5 "
            "(minute hour day-of-month month day-of-week)"
        )
    parsed: list[frozenset[int]] = []
    restricted: list[bool] = []
    for raw, (name, low, high) in zip(fields, _RANGES, strict=True):
        values, is_restricted = _parse_field(raw, name, low, high, expression)
        parsed.append(values)
        restricted.append(is_restricted)
    return CronExpr(
        minutes=parsed[0],
        hours=parsed[1],
        days_of_month=parsed[2],
        months=parsed[3],
        days_of_week=parsed[4],
        dom_restricted=restricted[2],
        dow_restricted=restricted[4],
        source=expression,
    )


def _parse_field(
    raw: str, name: str, low: int, high: int, expression: str
) -> tuple[frozenset[int], bool]:
    values: set[int] = set()
    restricted = False
    for part in raw.split(","):
        if not _FIELD.match(part):
            raise SpecError(
                f"cron {expression!r}: {name} field {part!r} is not supported "
                "(use *, n, a-b, */n or a-b/n; names and ? are not supported)"
            )
        body, _, step_text = part.partition("/")
        step = int(step_text) if step_text else 1
        if step < 1:
            raise SpecError(f"cron {expression!r}: {name} step must be >= 1")
        if body == "*":
            start, stop = low, high
        else:
            restricted = True
            start_text, _, stop_text = body.partition("-")
            start = int(start_text)
            stop = int(stop_text) if stop_text else start
        if start < low or stop > high or start > stop:
            raise SpecError(f"cron {expression!r}: {name} range {body!r} is outside {low}-{high}")
        values.update(range(start, stop + 1, step))
    if not values:  # pragma: no cover - unreachable given the checks above
        raise SpecError(f"cron {expression!r}: {name} field matches nothing")
    return frozenset(values), restricted
