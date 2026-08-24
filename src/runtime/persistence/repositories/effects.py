"""Effect intent rows.

`begin()` is the only interesting method. It must be one statement, because the
gap between "check whether an intent exists" and "insert one" is exactly the
window in which two workers both decide to fire the same effect.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.domain.enums import EffectStatus
from runtime.persistence.json import to_jsonb, to_jsonb_or_none


@dataclass(frozen=True, slots=True)
class EffectRow:
    id: uuid.UUID
    logical_call_id: str
    status: EffectStatus
    recovery_policy: str
    tool_name: str
    tool_version: int
    idempotency_key: str | None
    marker: str | None
    provider_ref: str | None
    result_ref: uuid.UUID | None
    result_inline: dict[str, Any] | None
    attempts: int
    fence: int


def _row_to_effect(row: Any) -> EffectRow:
    return EffectRow(
        id=row.id,
        logical_call_id=row.logical_call_id,
        status=EffectStatus(row.status),
        recovery_policy=row.recovery_policy,
        tool_name=row.tool_name,
        tool_version=row.tool_version,
        idempotency_key=row.idempotency_key,
        marker=row.marker,
        provider_ref=row.provider_ref,
        result_ref=row.result_ref,
        result_inline=row.result_inline,
        attempts=row.attempts,
        fence=row.fence,
    )


_SELECT_COLUMNS = """
    id, logical_call_id, status, recovery_policy, tool_name, tool_version,
    idempotency_key, marker, provider_ref, result_ref, result_inline, attempts, fence
"""


class EffectRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def upsert_intent(self, row: dict[str, Any]) -> tuple[EffectRow, bool]:
        """Insert an INTENT row, or return the one already there.

        Returns `(row, created)`. `created is False` means this call is a replay of
        something that has been attempted before, and the recovery policy decides
        what happens next.

        `DO UPDATE SET attempts = attempts + 1` rather than `DO NOTHING` because the
        RETURNING clause of `DO NOTHING` yields no row on conflict, which would
        force a second round trip in exactly the contended case where round trips
        are expensive.
        """
        stmt = text(
            f"""
            INSERT INTO effect_intents (
                id, logical_call_id, organization_id, run_id, root_run_id, fence,
                node, checkpoint_ns, ordinal, args_hash, tool_name, tool_version,
                status, recovery_policy, blast_radius, idempotency_key, marker, attempts
            ) VALUES (
                :id, :logical_call_id, :organization_id, :run_id, :root_run_id, :fence,
                :node, :checkpoint_ns, :ordinal, :args_hash, :tool_name, :tool_version,
                'INTENT', :recovery_policy, :blast_radius, :idempotency_key, :marker, 1
            )
            ON CONFLICT ON CONSTRAINT uq_effect_logical_call DO UPDATE
                SET attempts = effect_intents.attempts + 1,
                    fence = EXCLUDED.fence
            RETURNING {_SELECT_COLUMNS},
                      (xmax = 0) AS created
            """
        )
        result = (await self._s.execute(stmt, row)).one()
        return _row_to_effect(result), bool(result.created)

    async def get(self, logical_call_id: str) -> EffectRow | None:
        row = (
            await self._s.execute(
                text(f"SELECT {_SELECT_COLUMNS} FROM effect_intents WHERE logical_call_id = :key"),
                {"key": logical_call_id},
            )
        ).one_or_none()
        return _row_to_effect(row) if row is not None else None

    async def commit_effect(
        self,
        logical_call_id: str,
        *,
        result_ref: uuid.UUID | None,
        result_inline: dict[str, Any] | None,
        provider_ref: str | None,
        marker: str | None = None,
    ) -> None:
        await self._s.execute(
            text(
                """
                UPDATE effect_intents
                   SET status = 'COMMITTED',
                       result_ref = :result_ref,
                       result_inline = CAST(:result_inline AS jsonb),
                       provider_ref = COALESCE(:provider_ref, provider_ref),
                       marker = COALESCE(:marker, marker),
                       settled_at = now(),
                       error = NULL
                 WHERE logical_call_id = :key
                """
            ),
            {
                "key": logical_call_id,
                "result_ref": result_ref,
                "result_inline": to_jsonb_or_none(result_inline),
                "provider_ref": provider_ref,
                "marker": marker,
            },
        )

    async def settle(self, logical_call_id: str, status: EffectStatus, error: str) -> None:
        await self._s.execute(
            text(
                """
                UPDATE effect_intents
                   SET status = :status, error = :error, settled_at = now()
                 WHERE logical_call_id = :key
                """
            ),
            {"key": logical_call_id, "status": status.value, "error": error[:4000]},
        )

    async def for_run(self, run_id: uuid.UUID) -> list[EffectRow]:
        rows = (
            await self._s.execute(
                text(
                    f"SELECT {_SELECT_COLUMNS} FROM effect_intents "
                    "WHERE run_id = :run_id ORDER BY created_at, ordinal"
                ),
                {"run_id": run_id},
            )
        ).all()
        return [_row_to_effect(r) for r in rows]

    async def open_intents(self, limit: int = 100) -> list[EffectRow]:
        """Rows stuck at INTENT. Every one is either an effect that never fired or
        an effect whose outcome we failed to record — recovery has to decide
        which."""
        rows = (
            await self._s.execute(
                text(
                    f"SELECT {_SELECT_COLUMNS} FROM effect_intents "
                    "WHERE status = 'INTENT' ORDER BY created_at LIMIT :limit"
                ),
                {"limit": limit},
            )
        ).all()
        return [_row_to_effect(r) for r in rows]


class FixtureRepository:
    """The observation surface for the exactly-once test.

    `fixture.sideeffect@1` writes here and nowhere else, so "did the effect fire
    twice" is `SELECT count(*)`.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def write(
        self, marker: str, run_id: uuid.UUID, logical_call_id: str, payload: dict[str, Any]
    ) -> None:
        """Deliberately *not* `ON CONFLICT DO NOTHING`.

        If the runtime double-fires this effect, the test must see it. Swallowing
        the duplicate here would make the fixture launder the very bug it exists to
        detect — so a second write raises IntegrityError instead.
        """
        await self._s.execute(
            text(
                """
                INSERT INTO sideeffect_fixture (marker, run_id, logical_call_id, payload)
                VALUES (:marker, :run_id, :lcid, CAST(:payload AS jsonb))
                """
            ),
            {
                "marker": marker,
                "run_id": run_id,
                "lcid": logical_call_id,
                "payload": to_jsonb(payload),
            },
        )

    async def find_by_marker(self, marker: str) -> dict[str, Any] | None:
        """The probe path. Returns the stamped row if the effect already landed."""
        row = (
            await self._s.execute(
                text(
                    "SELECT id, marker, run_id, logical_call_id, payload "
                    "FROM sideeffect_fixture WHERE marker = :marker"
                ),
                {"marker": marker},
            )
        ).one_or_none()
        if row is None:
            return None
        return {
            "id": row.id,
            "marker": row.marker,
            "run_id": str(row.run_id),
            "logical_call_id": row.logical_call_id,
            "payload": row.payload,
        }

    async def count(self, run_id: uuid.UUID | None = None) -> int:
        if run_id is None:
            stmt, params = text("SELECT count(*) FROM sideeffect_fixture"), {}
        else:
            stmt, params = (
                text("SELECT count(*) FROM sideeffect_fixture WHERE run_id = :run_id"),
                {"run_id": run_id},
            )
        return int((await self._s.execute(stmt, params)).scalar_one())
