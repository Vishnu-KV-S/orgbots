"""Read the day's ed-tech briefs.

`research@1` on the scheduled path submits nothing: there is no task row to attach a
report to, so the `CompetitorReport@1` it produced lives in the `run.succeeded` event
payload and nowhere else. That is a fine place for it to live — the event log is the
run's own record and it is what the UI reads — but it is not a place anybody wants to
type a query against at eight in the morning.

    uv run python scripts/stockeds_digest.py <organization-id> [--day 2026-09-02]
                                             [--full] [--json]

Default is today in the schedule's timezone, one paragraph per brief. `--full` adds
the competitors, themes, recommendations and sources; `--json` prints the reports
unrendered, for piping somewhere else.

A failed run is listed too, with its reason. A digest that silently skipped them would
answer "what happened today" with "four briefs" on a day when the answer was "four
briefs, and the policy one died on a schema error" — which is the half you would have
wanted to know.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import sys
import textwrap
import uuid
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text

from runtime.persistence.uow import UnitOfWorkFactory
from runtime.settings import get_settings

TZ = ZoneInfo("Asia/Kolkata")
"""The schedule's timezone. A digest keyed on UTC days would split the 21:00 brief
from the four before it, which is exactly the boundary a reader does not have."""

SQL = """
SELECT r.id,
       r.status,
       r.status_reason,
       r.created_at,
       r.ended_at,
       coalesce((SELECT sum(u.cost_cents) FROM usage_ledger u WHERE u.run_id = r.id), 0)
         AS cost_cents,
       (SELECT e.payload
          FROM events e
         WHERE e.run_id = r.id AND e.topic = 'run.succeeded'
         ORDER BY e.id DESC LIMIT 1) AS payload
  FROM runs r
  JOIN actors a ON a.id = r.actor_id
 WHERE r.organization_id = :org
   AND a.name = 'research'
   AND r.created_at >= :start
   AND r.created_at < :end
 ORDER BY r.created_at
"""


def _wrap(body: str, indent: str = "  ") -> str:
    return textwrap.fill(body, width=94, initial_indent=indent, subsequent_indent=indent)


def _render(row: Any, report: dict[str, Any] | None, *, full: bool) -> None:
    when = row.created_at.astimezone(TZ)
    money = f"${row.cost_cents / 100:,.2f}"
    if report is None:
        reason = row.status_reason or "no reason recorded"
        print(f"\n{when:%H:%M}  [{row.status}]  {money}")
        print(_wrap(reason))
        return

    print(f"\n{when:%H:%M}  {report.get('subject', '(no subject)')}   {money}")
    print(_wrap(report.get("summary", "")))

    if not full:
        gaps = report.get("gaps") or []
        if gaps:
            print(f"  ({len(gaps)} gap(s) declared — --full to read them)")
        return

    for label, key in (("Named", "competitors"), ("Themes", "themes")):
        items = report.get(key) or []
        if items:
            print(f"\n  {label}:")
            for item in items:
                head = item.get("name") or item.get("statement") or ""
                print(_wrap(f"- {head}", indent="    "))
    for label, key, field in (
        ("Recommendations", "recommendations", "action"),
        ("Gaps", "gaps", None),
    ):
        items = report.get(key) or []
        if items:
            print(f"\n  {label}:")
            for item in items:
                body = item if field is None else (item.get(field) or str(item))
                print(_wrap(f"- {body}", indent="    "))
    sources = report.get("sources") or []
    if sources:
        print(f"\n  Sources ({len(sources)}):")
        for s in sources:
            print(f"    {s.get('url', '')}")


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("organization")
    ap.add_argument("--day", default=None, help="ISO date, Asia/Kolkata. Default: today")
    ap.add_argument("--full", action="store_true", help="the whole report, not the summary")
    ap.add_argument("--json", action="store_true", help="raw reports, one JSON array")
    args = ap.parse_args()

    day = dt.date.fromisoformat(args.day) if args.day else dt.datetime.now(TZ).date()
    start = dt.datetime.combine(day, dt.time.min, tzinfo=TZ)
    params = {
        "org": uuid.UUID(args.organization),
        "start": start,
        "end": start + dt.timedelta(days=1),
    }

    factory = UnitOfWorkFactory(get_settings())
    async with factory() as uow:
        rows = (await uow.session.execute(text(SQL), params)).all()

    reports = [
        ((r.payload or {}).get("output") or {}).get("report") if r.payload else None for r in rows
    ]

    if args.json:
        print(json.dumps([r for r in reports if r], indent=2))
        return 0

    filed = sum(1 for r in reports if r)
    spend = sum(r.cost_cents for r in rows)
    print(f"stockeds — ed-tech briefs for {day:%A %d %B %Y}")
    print(f"{filed} of {len(rows)} run(s) filed a report · ${spend / 100:,.2f} spent")
    if not rows:
        print("\nNothing ran. Either no slot has come round yet today, or the worker's")
        print("conductor is off — see `RUNTIME_CONDUCTOR_ENABLED`.")
        return 0
    for row, report in zip(rows, reports, strict=True):
        _render(row, report, full=args.full)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
