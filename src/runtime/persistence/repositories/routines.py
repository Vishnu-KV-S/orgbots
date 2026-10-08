"""Bot routines and their firings. See migration 043.

Like the vault's repository, this one stores a signing secret only as ciphertext and
holds no key: sealing and opening it is the API's, through the credential cipher.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_COLUMNS = """
    id, organization_id, bot_id, name, instruction, kind, cron, timezone, source, match,
    token, secret_key_id, secret_nonce, secret_ciphertext, inputs, output, approval,
    when_missing, active, created_by_kind, created_by_bot_id, next_fire_at,
    last_evaluated_at, last_fired_at, fire_count, created_at, updated_at
"""

_RUN_COLUMNS = """
    id, routine_id, bot_id, trigger, scheduled_for, status, run_id, detail, event,
    created_at, started_at
"""

EDITABLE = frozenset(
    {
        "name",
        "instruction",
        "cron",
        "timezone",
        "source",
        "match",
        "inputs",
        "output",
        "approval",
        "when_missing",
        "active",
        "next_fire_at",
        "last_evaluated_at",
        "secret_key_id",
        "secret_nonce",
        "secret_ciphertext",
    }
)
_JSON_FIELDS = frozenset({"match"})


@dataclass(frozen=True, slots=True)
class RoutineRow:
    id: uuid.UUID
    organization_id: uuid.UUID
    bot_id: uuid.UUID
    name: str
    instruction: str
    kind: str
    cron: str | None
    timezone: str
    source: str | None
    match: dict[str, Any]
    token: str | None
    secret_key_id: str | None
    secret_nonce: bytes | None
    secret_ciphertext: bytes | None
    inputs: str
    output: str
    approval: str
    when_missing: str
    active: bool
    created_by_kind: str
    created_by_bot_id: uuid.UUID | None
    next_fire_at: dt.datetime | None
    last_evaluated_at: dt.datetime | None
    last_fired_at: dt.datetime | None
    fire_count: int
    created_at: dt.datetime
    updated_at: dt.datetime

    @property
    def has_secret(self) -> bool:
        return self.secret_ciphertext is not None

    def __repr__(self) -> str:
        return f"RoutineRow(id={self.id}, name={self.name!r}, kind={self.kind!r})"


@dataclass(frozen=True, slots=True)
class RoutineRunRow:
    id: uuid.UUID
    routine_id: uuid.UUID
    bot_id: uuid.UUID
    trigger: str
    scheduled_for: dt.datetime | None
    status: str
    run_id: uuid.UUID | None
    detail: str
    event: dict[str, Any]
    created_at: dt.datetime
    started_at: dt.datetime | None


def _routine(r: Any) -> RoutineRow:
    return RoutineRow(
        id=r.id,
        organization_id=r.organization_id,
        bot_id=r.bot_id,
        name=r.name,
        instruction=r.instruction,
        kind=r.kind,
        cron=r.cron,
        timezone=r.timezone,
        source=r.source,
        match=dict(r.match or {}),
        token=r.token,
        secret_key_id=r.secret_key_id,
        secret_nonce=bytes(r.secret_nonce) if r.secret_nonce is not None else None,
        secret_ciphertext=(bytes(r.secret_ciphertext) if r.secret_ciphertext is not None else None),
        inputs=r.inputs,
        output=r.output,
        approval=r.approval,
        when_missing=r.when_missing,
        active=r.active,
        created_by_kind=r.created_by_kind,
        created_by_bot_id=r.created_by_bot_id,
        next_fire_at=r.next_fire_at,
        last_evaluated_at=r.last_evaluated_at,
        last_fired_at=r.last_fired_at,
        fire_count=r.fire_count,
        created_at=r.created_at,
        updated_at=r.updated_at,
    )


def _run(r: Any) -> RoutineRunRow:
    return RoutineRunRow(
        id=r.id,
        routine_id=r.routine_id,
        bot_id=r.bot_id,
        trigger=r.trigger,
        scheduled_for=r.scheduled_for,
        status=r.status,
        run_id=r.run_id,
        detail=r.detail,
        event=dict(r.event or {}),
        created_at=r.created_at,
        started_at=r.started_at,
    )


class RoutineRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    # --- routines ------------------------------------------------------------------

    async def create(
        self,
        routine_id: uuid.UUID,
        *,
        organization_id: uuid.UUID,
        bot_id: uuid.UUID,
        name: str,
        instruction: str,
        kind: str,
        cron: str | None,
        timezone: str,
        source: str | None,
        match: dict[str, Any],
        token: str | None,
        inputs: str,
        output: str,
        approval: str,
        when_missing: str,
        active: bool,
        created_by_kind: str,
        created_by_bot_id: uuid.UUID | None,
        next_fire_at: dt.datetime | None,
    ) -> bool:
        """False when a routine with this id already exists (a replayed step)."""
        result = await self._s.execute(
            text(
                """
                INSERT INTO bot_routines (
                    id, organization_id, bot_id, name, instruction, kind, cron, timezone,
                    source, match, token, inputs, output, approval, when_missing, active,
                    created_by_kind, created_by_bot_id, next_fire_at, last_evaluated_at
                ) VALUES (
                    :id, :org, :bot, :name, :instruction, :kind, :cron, :tz, :source,
                    CAST(:match AS jsonb), :token, :inputs, :output, :approval, :missing,
                    :active, :by_kind, :by_bot, :next, now()
                )
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {
                "id": routine_id,
                "org": organization_id,
                "bot": bot_id,
                "name": name,
                "instruction": instruction,
                "kind": kind,
                "cron": cron,
                "tz": timezone,
                "source": source,
                "match": json.dumps(match),
                "token": token,
                "inputs": inputs,
                "output": output,
                "approval": approval,
                "missing": when_missing,
                "active": active,
                "by_kind": created_by_kind,
                "by_bot": created_by_bot_id,
                "next": next_fire_at,
            },
        )
        return bool(getattr(result, "rowcount", 0))

    async def get(self, routine_id: uuid.UUID) -> RoutineRow | None:
        row = (
            await self._s.execute(
                text(f"SELECT {_COLUMNS} FROM bot_routines WHERE id = :id AND deleted_at IS NULL"),
                {"id": routine_id},
            )
        ).first()
        return None if row is None else _routine(row)

    async def by_name(self, bot_id: uuid.UUID, name: str) -> RoutineRow | None:
        row = (
            await self._s.execute(
                text(
                    f"SELECT {_COLUMNS} FROM bot_routines WHERE bot_id = :bot "
                    "AND lower(name) = lower(:name) AND deleted_at IS NULL"
                ),
                {"bot": bot_id, "name": name.strip()},
            )
        ).first()
        return None if row is None else _routine(row)

    async def for_bot(self, bot_id: uuid.UUID) -> list[RoutineRow]:
        rows = (
            await self._s.execute(
                text(
                    f"SELECT {_COLUMNS} FROM bot_routines WHERE bot_id = :bot "
                    "AND deleted_at IS NULL ORDER BY created_at"
                ),
                {"bot": bot_id},
            )
        ).all()
        return [_routine(r) for r in rows]

    async def for_organization(self, organization_id: uuid.UUID) -> list[RoutineRow]:
        rows = (
            await self._s.execute(
                text(
                    f"SELECT {_COLUMNS} FROM bot_routines WHERE organization_id = :org "
                    "AND deleted_at IS NULL ORDER BY created_at"
                ),
                {"org": organization_id},
            )
        ).all()
        return [_routine(r) for r in rows]

    async def count_for_bot(self, bot_id: uuid.UUID) -> int:
        return int(
            (
                await self._s.execute(
                    text(
                        "SELECT count(*) FROM bot_routines WHERE bot_id = :bot "
                        "AND deleted_at IS NULL"
                    ),
                    {"bot": bot_id},
                )
            ).scalar_one()
        )

    async def update(self, routine_id: uuid.UUID, fields: dict[str, Any]) -> None:
        unknown = set(fields) - EDITABLE
        if unknown:
            raise ValueError(f"not editable: {sorted(unknown)}")
        if not fields:
            return
        assignments = ", ".join(
            f"{name} = CAST(:{name} AS jsonb)" if name in _JSON_FIELDS else f"{name} = :{name}"
            for name in sorted(fields)
        )
        params = {
            name: json.dumps(value) if name in _JSON_FIELDS else value
            for name, value in fields.items()
        }
        await self._s.execute(
            text(f"UPDATE bot_routines SET {assignments}, updated_at = now() WHERE id = :id"),
            {**params, "id": routine_id},
        )

    async def soft_delete(self, routine_id: uuid.UUID) -> None:
        await self._s.execute(
            text(
                "UPDATE bot_routines SET deleted_at = now(), active = false, updated_at = now() "
                "WHERE id = :id"
            ),
            {"id": routine_id},
        )

    async def delete_for_bot(self, bot_id: uuid.UUID) -> int:
        """A deleted bot's routines go with it — a routine has no one else to work for."""
        result = await self._s.execute(
            text(
                "UPDATE bot_routines SET deleted_at = now(), active = false "
                "WHERE bot_id = :bot AND deleted_at IS NULL"
            ),
            {"bot": bot_id},
        )
        return int(getattr(result, "rowcount", 0) or 0)

    async def due(self, now: dt.datetime, limit: int = 100) -> list[RoutineRow]:
        rows = (
            await self._s.execute(
                text(
                    f"SELECT {_COLUMNS} FROM bot_routines WHERE deleted_at IS NULL AND active "
                    "AND kind = 'schedule' AND next_fire_at <= :now "
                    "ORDER BY next_fire_at LIMIT :limit"
                ),
                {"now": now, "limit": limit},
            )
        ).all()
        return [_routine(r) for r in rows]

    async def evaluated(
        self,
        routine_id: uuid.UUID,
        *,
        now: dt.datetime,
        next_fire_at: dt.datetime | None,
        fired: bool,
    ) -> None:
        await self._s.execute(
            text(
                """
                UPDATE bot_routines
                   SET last_evaluated_at = :now,
                       next_fire_at = :next,
                       last_fired_at = CASE WHEN :fired THEN :now ELSE last_fired_at END,
                       fire_count = fire_count + CASE WHEN :fired THEN 1 ELSE 0 END
                 WHERE id = :id
                """
            ),
            {"id": routine_id, "now": now, "next": next_fire_at, "fired": fired},
        )

    async def note_fired(self, routine_id: uuid.UUID) -> None:
        """An event or a test run — fires that are not the schedule's."""
        await self._s.execute(
            text(
                "UPDATE bot_routines SET last_fired_at = now(), fire_count = fire_count + 1 "
                "WHERE id = :id"
            ),
            {"id": routine_id},
        )

    # --- firings -------------------------------------------------------------------

    async def add_run(
        self,
        run_row_id: uuid.UUID,
        *,
        routine_id: uuid.UUID,
        bot_id: uuid.UUID,
        trigger: str,
        scheduled_for: dt.datetime | None = None,
        status: str = "queued",
        detail: str = "",
        event: dict[str, Any] | None = None,
    ) -> bool:
        """False when this firing already exists — a lost race or a retried delivery."""
        result = await self._s.execute(
            text(
                """
                INSERT INTO bot_routine_runs
                    (id, routine_id, bot_id, trigger, scheduled_for, status, detail, event)
                VALUES (:id, :routine, :bot, :trigger, :at, :status, :detail,
                        CAST(:event AS jsonb))
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {
                "id": run_row_id,
                "routine": routine_id,
                "bot": bot_id,
                "trigger": trigger,
                "at": scheduled_for,
                "status": status,
                "detail": detail,
                "event": json.dumps(event or {}, default=str),
            },
        )
        return bool(getattr(result, "rowcount", 0))

    async def queued(self, limit: int = 100) -> list[RoutineRunRow]:
        rows = (
            await self._s.execute(
                text(
                    f"SELECT {_RUN_COLUMNS} FROM bot_routine_runs WHERE status = 'queued' "
                    "ORDER BY created_at LIMIT :limit"
                ),
                {"limit": limit},
            )
        ).all()
        return [_run(r) for r in rows]

    async def settle_run(
        self,
        run_row_id: uuid.UUID,
        *,
        status: str,
        run_id: uuid.UUID | None = None,
        detail: str = "",
    ) -> bool:
        """Move a queued firing on. Conditional, so of two runners exactly one claims it
        (`status='started'`) and goes on to start its run."""
        result = await self._s.execute(
            text(
                """
                UPDATE bot_routine_runs
                   SET status = :status, run_id = :run, detail = :detail,
                       started_at = CASE WHEN :status = 'started' THEN now() ELSE NULL END
                 WHERE id = :id AND status = 'queued'
                """
            ),
            {"id": run_row_id, "status": status, "run": run_id, "detail": detail},
        )
        return bool(getattr(result, "rowcount", 0))

    async def finish_run(
        self, run_row_id: uuid.UUID, *, status: str, run_id: uuid.UUID | None, detail: str = ""
    ) -> None:
        """What a claimed firing came to: the run it started, or why it did not."""
        await self._s.execute(
            text(
                "UPDATE bot_routine_runs SET status = :status, run_id = :run, detail = :detail "
                "WHERE id = :id"
            ),
            {"id": run_row_id, "status": status, "run": run_id, "detail": detail},
        )

    async def get_run(self, run_row_id: uuid.UUID) -> RoutineRunRow | None:
        row = (
            await self._s.execute(
                text(f"SELECT {_RUN_COLUMNS} FROM bot_routine_runs WHERE id = :id"),
                {"id": run_row_id},
            )
        ).first()
        return None if row is None else _run(row)

    async def runs(self, routine_id: uuid.UUID, limit: int = 20) -> list[RoutineRunRow]:
        rows = (
            await self._s.execute(
                text(
                    f"SELECT {_RUN_COLUMNS} FROM bot_routine_runs WHERE routine_id = :routine "
                    "ORDER BY created_at DESC LIMIT :limit"
                ),
                {"routine": routine_id, "limit": limit},
            )
        ).all()
        return [_run(r) for r in rows]

    async def last_runs(self, bot_id: uuid.UUID) -> dict[uuid.UUID, RoutineRunRow]:
        """Each of a bot's routines' latest firing, for the list."""
        rows = (
            await self._s.execute(
                text(
                    f"SELECT DISTINCT ON (routine_id) {_RUN_COLUMNS} FROM bot_routine_runs "
                    "WHERE bot_id = :bot ORDER BY routine_id, created_at DESC"
                ),
                {"bot": bot_id},
            )
        ).all()
        return {r.routine_id: _run(r) for r in rows}

    async def prune_runs(self, routine_id: uuid.UUID, keep: int) -> None:
        """Keep the last `keep` settled firings. A queued one is never pruned."""
        await self._s.execute(
            text(
                """
                DELETE FROM bot_routine_runs
                 WHERE routine_id = :routine AND status <> 'queued'
                   AND id NOT IN (
                       SELECT id FROM bot_routine_runs WHERE routine_id = :routine
                        ORDER BY created_at DESC LIMIT :keep
                   )
                """
            ),
            {"routine": routine_id, "keep": keep},
        )
