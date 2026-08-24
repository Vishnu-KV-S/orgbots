"""Kill switches.

One read, `active()`, which returns every live switch for an organization in one
query. The gateway caches the result for 10s (M2 §7, edge case 70) — fast enough that
engaging a switch takes effect while somebody is still watching the dashboard, slow
enough that it does not put a query on every tool call.

Everything else here is an operator action, and none of it is on a hot path.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.domain.enums import KillMode, KillScope


@dataclass(frozen=True, slots=True)
class KillSwitchRow:
    id: uuid.UUID
    scope_type: KillScope
    scope_id: str | None
    mode: KillMode
    reason: str
    engaged_by: str
    engaged_at: dt.datetime

    def covers(self, *, tool: str | None, actor: str | None, connection: str | None) -> bool:
        """Does this switch apply to the thing being attempted?

        An `org` switch covers everything, which is the only scope where a NULL
        `scope_id` is legal — the check constraint in 019 says so, and this method
        relies on it rather than re-deriving it.
        """
        if self.scope_type is KillScope.ORG:
            return True
        if self.scope_type is KillScope.TOOL:
            return tool is not None and self.scope_id == tool
        if self.scope_type is KillScope.ACTOR:
            return actor is not None and self.scope_id == actor
        return connection is not None and self.scope_id == connection


class KillSwitchRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def active(self, organization_id: uuid.UUID) -> list[KillSwitchRow]:
        rows = (
            await self._s.execute(
                text(
                    """
                    SELECT id, scope_type, scope_id, mode, reason, engaged_by, engaged_at
                      FROM kill_switches
                     WHERE organization_id = :org AND disengaged_at IS NULL
                     ORDER BY engaged_at
                    """
                ),
                {"org": organization_id},
            )
        ).all()
        return [
            KillSwitchRow(
                id=r.id,
                scope_type=KillScope(r.scope_type),
                scope_id=r.scope_id,
                mode=KillMode(r.mode),
                reason=r.reason,
                engaged_by=r.engaged_by,
                engaged_at=r.engaged_at,
            )
            for r in rows
        ]

    async def engage(
        self,
        switch_id: uuid.UUID,
        organization_id: uuid.UUID,
        *,
        scope_type: KillScope,
        scope_id: str | None,
        mode: KillMode,
        reason: str,
        engaged_by: str,
    ) -> uuid.UUID | None:
        """Engage a switch. Returns None if one is already live for this scope.

        Not an upsert. Two live switches for one scope in different modes is an
        ambiguity nobody resolves correctly at 3am, and silently *changing* an
        existing switch's mode under whoever engaged it is worse — so the second
        caller is told no and has to disengage first.
        """
        row = (
            await self._s.execute(
                text(
                    """
                    INSERT INTO kill_switches (id, organization_id, scope_type, scope_id,
                                               mode, reason, engaged_by)
                    VALUES (:id, :org, :st, :sid, :mode, :reason, :by)
                    ON CONFLICT DO NOTHING
                    RETURNING id
                    """
                ),
                {
                    "id": switch_id,
                    "org": organization_id,
                    "st": scope_type.value,
                    "sid": scope_id,
                    "mode": mode.value,
                    "reason": reason,
                    "by": engaged_by,
                },
            )
        ).one_or_none()
        return uuid.UUID(str(row.id)) if row is not None else None

    async def disengage(
        self,
        organization_id: uuid.UUID,
        *,
        scope_type: KillScope,
        scope_id: str | None,
        disengaged_by: str,
    ) -> bool:
        row = (
            await self._s.execute(
                text(
                    """
                    UPDATE kill_switches
                       SET disengaged_at = now(), disengaged_by = :by
                     WHERE organization_id = :org AND scope_type = :st
                       AND scope_id IS NOT DISTINCT FROM :sid
                       AND disengaged_at IS NULL
                    RETURNING id
                    """
                ),
                {
                    "org": organization_id,
                    "st": scope_type.value,
                    "sid": scope_id,
                    "by": disengaged_by,
                },
            )
        ).one_or_none()
        return row is not None
