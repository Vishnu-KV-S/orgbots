"""Sessions and their summaries."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.domain.ids import SessionId


@dataclass(frozen=True, slots=True)
class SessionRow:
    id: SessionId
    organization_id: uuid.UUID
    actor_name: str
    session_key: str
    status: str
    message_count: int
    summarized_upto: int


@dataclass(frozen=True, slots=True)
class SummaryRow:
    id: int
    session_id: SessionId
    upto_message_count: int
    summary: str


class SessionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def ensure(
        self,
        session_id: SessionId,
        organization_id: uuid.UUID,
        actor_name: str,
        session_key: str,
        correlation_id: uuid.UUID | None = None,
    ) -> SessionId:
        """Get-or-create on `session_key`.

        `INSERT ... ON CONFLICT DO NOTHING` then read, rather than SELECT-then-
        INSERT: two runs of the same actor starting in the same second would
        otherwise both find nothing and both insert.
        """
        await self._s.execute(
            text(
                """
                INSERT INTO sessions (id, organization_id, actor_name, session_key,
                                      correlation_id)
                VALUES (:id, :org, :actor, :key, :corr)
                ON CONFLICT ON CONSTRAINT uq_session_key DO NOTHING
                """
            ),
            {
                "id": session_id,
                "org": organization_id,
                "actor": actor_name,
                "key": session_key,
                "corr": correlation_id,
            },
        )
        found = (
            await self._s.execute(
                text("SELECT id FROM sessions WHERE organization_id = :org AND session_key = :key"),
                {"org": organization_id, "key": session_key},
            )
        ).scalar_one()
        return SessionId(found)

    async def get(self, session_id: SessionId) -> SessionRow | None:
        r = (
            await self._s.execute(
                text(
                    """
                    SELECT id, organization_id, actor_name, session_key, status,
                           message_count, summarized_upto
                      FROM sessions WHERE id = :id
                    """
                ),
                {"id": session_id},
            )
        ).one_or_none()
        if r is None:
            return None
        return SessionRow(
            id=SessionId(r.id),
            organization_id=r.organization_id,
            actor_name=r.actor_name,
            session_key=r.session_key,
            status=r.status,
            message_count=r.message_count,
            summarized_upto=r.summarized_upto,
        )

    async def bump_message_count(self, session_id: SessionId, by: int = 1) -> int:
        row = (
            await self._s.execute(
                text(
                    """
                    UPDATE sessions SET message_count = message_count + :by
                     WHERE id = :id RETURNING message_count
                    """
                ),
                {"id": session_id, "by": by},
            )
        ).one()
        return int(row.message_count)

    async def add_summary(
        self,
        session_id: SessionId,
        *,
        upto_message_count: int,
        summary: str,
        run_id: uuid.UUID | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_cents: int = 0,
    ) -> int | None:
        """Append a summary. `None` when this cut point already has one.

        `uq_session_summary_upto` makes a replayed summarize node a no-op rather
        than a second identical summary — and, more to the point, rather than a
        second SUMMARIZATION model call showing up in the coordination ratio.
        """
        got = (
            await self._s.execute(
                text(
                    """
                    INSERT INTO session_summaries (session_id, run_id, upto_message_count,
                                                   summary, input_tokens, output_tokens,
                                                   cost_cents)
                    VALUES (:sid, :run, :upto, :summary, :in_tok, :out_tok, :cost)
                    ON CONFLICT ON CONSTRAINT uq_session_summary_upto DO NOTHING
                    RETURNING id
                    """
                ),
                {
                    "sid": session_id,
                    "run": run_id,
                    "upto": upto_message_count,
                    "summary": summary,
                    "in_tok": input_tokens,
                    "out_tok": output_tokens,
                    "cost": cost_cents,
                },
            )
        ).scalar_one_or_none()
        if got is not None:
            await self._s.execute(
                text("UPDATE sessions SET summarized_upto = :upto WHERE id = :id"),
                {"id": session_id, "upto": upto_message_count},
            )
        return int(got) if got is not None else None

    async def latest_summary(self, session_id: SessionId) -> SummaryRow | None:
        r = (
            await self._s.execute(
                text(
                    """
                    SELECT id, session_id, upto_message_count, summary
                      FROM session_summaries WHERE session_id = :id
                     ORDER BY upto_message_count DESC, id DESC LIMIT 1
                    """
                ),
                {"id": session_id},
            )
        ).one_or_none()
        if r is None:
            return None
        return SummaryRow(
            id=int(r.id),
            session_id=SessionId(r.session_id),
            upto_message_count=int(r.upto_message_count),
            summary=r.summary,
        )
