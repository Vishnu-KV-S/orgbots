"""Triggers and the fire ledger.

`claim_fire()` is the entire concurrency story. It inserts a `trigger_fires` row
keyed on `H(trigger_id, scheduled_for)` and reports whether it won. Two schedulers
evaluating the same cron minute both compute the same key; one INSERT succeeds and
the other conflicts and does nothing. There is no lock, no leader election and no
"is it my turn" check — the primary key is the election. T18.

The row is written *before* the run is started and the run id is stamped onto it
afterwards. That ordering means a scheduler that dies in between leaves a fire row
with a null `run_id` — visible, diagnosable, and not a duplicate — rather than a
run nobody knows fired.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.domain.enums import CatchupPolicy
from runtime.domain.ids import TriggerId
from runtime.persistence.json import to_jsonb


def trigger_key(trigger_id: uuid.UUID, scheduled_for: dt.datetime) -> str:
    """H(trigger_id, scheduled_for_utc).

    Normalised to UTC and truncated to the minute before hashing. Cron resolution
    is one minute, so two schedulers whose clocks differ by a few hundred
    milliseconds must produce the same key or the dedupe does nothing at all.
    """
    stamp = scheduled_for.astimezone(dt.UTC).replace(second=0, microsecond=0)
    material = f"{trigger_id}\x1f{stamp.isoformat()}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class TriggerRow:
    id: TriggerId
    organization_id: uuid.UUID
    key: str
    actor_name: str
    cron: str
    timezone: str
    input: dict[str, Any]
    catchup_policy: CatchupPolicy
    last_evaluated_at: dt.datetime | None
    active: bool = True
    """Always true for a row from `active()`, which filters on it. `for_org()` is the
    read that returns paused triggers too, and it is the only caller for which this
    field carries information."""


class TriggerRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def upsert(
        self,
        trigger_id: TriggerId,
        *,
        organization_id: uuid.UUID,
        key: str,
        actor_name: str,
        cron: str,
        timezone: str = "UTC",
        payload: dict[str, Any] | None = None,
        catchup_policy: CatchupPolicy = CatchupPolicy.SKIP,
    ) -> TriggerId:
        got = (
            await self._s.execute(
                text(
                    """
                    INSERT INTO triggers (id, organization_id, key, actor_name, cron,
                                          timezone, input, catchup_policy)
                    VALUES (:id, :org, :key, :actor, :cron, :tz, CAST(:input AS jsonb), :catchup)
                    ON CONFLICT ON CONSTRAINT uq_trigger_key DO UPDATE
                       SET actor_name = EXCLUDED.actor_name,
                           cron = EXCLUDED.cron,
                           timezone = EXCLUDED.timezone,
                           input = EXCLUDED.input,
                           catchup_policy = EXCLUDED.catchup_policy,
                           active = true
                    RETURNING id
                    """
                ),
                {
                    "id": trigger_id,
                    "org": organization_id,
                    "key": key,
                    "actor": actor_name,
                    "cron": cron,
                    "tz": timezone,
                    "input": to_jsonb(payload or {}),
                    "catchup": catchup_policy.value,
                },
            )
        ).scalar_one()
        return TriggerId(got)

    async def active(self, organization_id: uuid.UUID | None = None) -> list[TriggerRow]:
        clause = " AND organization_id = :org" if organization_id else ""
        rows = (
            await self._s.execute(
                text(
                    f"""
                    SELECT id, organization_id, key, actor_name, cron, timezone, input,
                           catchup_policy, last_evaluated_at
                      FROM triggers WHERE active {clause} ORDER BY key
                    """
                ),
                {"org": organization_id} if organization_id else {},
            )
        ).all()
        return [
            TriggerRow(
                id=TriggerId(r.id),
                organization_id=r.organization_id,
                key=r.key,
                actor_name=r.actor_name,
                cron=r.cron,
                timezone=r.timezone,
                input=dict(r.input or {}),
                catchup_policy=CatchupPolicy(r.catchup_policy),
                last_evaluated_at=r.last_evaluated_at,
                active=True,
            )
            for r in rows
        ]

    async def for_org(self, organization_id: uuid.UUID) -> list[TriggerRow]:
        """Every trigger, paused ones included.

        `active()` is the scheduler's read and filters inactive rows out, which is
        correct for firing and useless for showing an operator what exists. A paused
        trigger that is invisible reads as a deleted one, and the next question is why
        `apply` brought it back.
        """
        rows = (
            await self._s.execute(
                text(
                    """
                    SELECT id, organization_id, key, actor_name, cron, timezone, input,
                           catchup_policy, last_evaluated_at, active
                      FROM triggers WHERE organization_id = :org ORDER BY key
                    """
                ),
                {"org": organization_id},
            )
        ).all()
        return [
            TriggerRow(
                id=TriggerId(r.id),
                organization_id=r.organization_id,
                key=r.key,
                actor_name=r.actor_name,
                cron=r.cron,
                timezone=r.timezone,
                input=dict(r.input or {}),
                catchup_policy=CatchupPolicy(r.catchup_policy),
                last_evaluated_at=r.last_evaluated_at,
                active=bool(r.active),
            )
            for r in rows
        ]

    async def set_active(
        self, organization_id: uuid.UUID, keys: Sequence[str], active: bool
    ) -> int:
        """Pause or resume triggers by key. Returns how many rows moved.

        **Pausing a trigger is not a stop, and must not be sold as one.** `upsert()`
        forces `active = true` in its conflict branch, so the next `spec apply` resumes
        anything paused here. That is the right behaviour for the config plane — the
        files are the intent, and a paused trigger the files still declare is drift —
        but it means an operator who wants a department to *stay* stopped wants the
        kill switch, which no apply touches. The control surface engages both, in that
        order, for exactly this reason.

        An empty `keys` is a no-op rather than "all of them": the plural form of a
        destructive verb should never widen when its argument goes missing.
        """
        if not keys:
            return 0
        # RETURNING rather than `rowcount`: the async result object does not expose a
        # row count, and counting what came back is the same number without depending
        # on the driver to report one.
        moved = (
            await self._s.execute(
                text(
                    """
                    UPDATE triggers SET active = :active
                     WHERE organization_id = :org AND key = ANY(:keys) AND active <> :active
                    RETURNING key
                    """
                ),
                {"org": organization_id, "keys": list(keys), "active": active},
            )
        ).all()
        return len(moved)

    async def claim_fire(
        self,
        trigger_id: TriggerId,
        scheduled_for: dt.datetime,
        *,
        skipped: bool = False,
        correlation_id: uuid.UUID | None = None,
    ) -> str | None:
        """Claim one occurrence. Returns the key, or `None` if someone else has it."""
        key = trigger_key(trigger_id, scheduled_for)
        got = (
            await self._s.execute(
                text(
                    """
                    INSERT INTO trigger_fires (trigger_key, trigger_id, scheduled_for,
                                               skipped, correlation_id)
                    VALUES (:key, :tid, :sched, :skipped, :corr)
                    ON CONFLICT (trigger_key) DO NOTHING
                    RETURNING trigger_key
                    """
                ),
                {
                    "key": key,
                    "tid": trigger_id,
                    "sched": scheduled_for,
                    "skipped": skipped,
                    "corr": correlation_id,
                },
            )
        ).scalar_one_or_none()
        return str(got) if got is not None else None

    async def attach_run(self, key: str, run_id: uuid.UUID) -> None:
        await self._s.execute(
            text("UPDATE trigger_fires SET run_id = :run WHERE trigger_key = :key"),
            {"key": key, "run": run_id},
        )

    async def mark_evaluated(self, trigger_id: TriggerId, at: dt.datetime) -> None:
        await self._s.execute(
            text("UPDATE triggers SET last_evaluated_at = :at WHERE id = :id"),
            {"id": trigger_id, "at": at},
        )

    async def fires_for(self, trigger_id: TriggerId) -> list[dict[str, Any]]:
        rows = (
            await self._s.execute(
                text(
                    """
                    SELECT trigger_key, scheduled_for, fired_at, run_id, skipped
                      FROM trigger_fires WHERE trigger_id = :id ORDER BY scheduled_for
                    """
                ),
                {"id": trigger_id},
            )
        ).all()
        return [dict(r._mapping) for r in rows]
