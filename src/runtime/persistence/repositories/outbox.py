"""Outbox and event log.

The relay is at-least-once by construction — it can crash between `XADD` and the
`published_at` update. Exactly-once comes from two idempotency layers underneath
it: `uq_outbox_dedupe` on the write, and the conditional `published_at IS NULL`
update plus a Redis-side dedupe key on the publish. T3 is the test that this
holds.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.persistence.json import to_jsonb


@dataclass(frozen=True, slots=True)
class OutboxRow:
    id: int
    organization_id: uuid.UUID
    topic: str
    payload: dict[str, Any]
    dedupe_key: str
    attempts: int


class OutboxRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def enqueue(
        self,
        organization_id: uuid.UUID,
        topic: str,
        payload: dict[str, Any],
        dedupe_key: str,
        *,
        run_id: uuid.UUID | None = None,
        root_run_id: uuid.UUID | None = None,
    ) -> int | None:
        """Write an outbox row and its durable event twin, in the caller's
        transaction.

        Both writes are `ON CONFLICT DO NOTHING`: re-announcing the same logical
        fact is a no-op rather than an error, which is what makes retrying
        `start_run()` safe.
        """
        await self._s.execute(
            text(
                """
                INSERT INTO events (organization_id, run_id, root_run_id, topic,
                                    payload, dedupe_key)
                VALUES (:org, :run_id, :root_run_id, :topic, CAST(:payload AS jsonb), :dedupe)
                ON CONFLICT ON CONSTRAINT uq_event_dedupe DO NOTHING
                """
            ),
            {
                "org": organization_id,
                "run_id": run_id,
                "root_run_id": root_run_id,
                "topic": topic,
                "payload": to_jsonb(payload),
                "dedupe": dedupe_key,
            },
        )
        row_id = (
            await self._s.execute(
                text(
                    """
                    INSERT INTO outbox (organization_id, topic, payload, dedupe_key)
                    VALUES (:org, :topic, CAST(:payload AS jsonb), :dedupe)
                    ON CONFLICT ON CONSTRAINT uq_outbox_dedupe DO NOTHING
                    RETURNING id
                    """
                ),
                {
                    "org": organization_id,
                    "topic": topic,
                    "payload": to_jsonb(payload),
                    "dedupe": dedupe_key,
                },
            )
        ).scalar_one_or_none()
        return int(row_id) if row_id is not None else None

    async def unpublished(self, limit: int) -> list[OutboxRow]:
        """Claim a batch for publishing.

        `FOR UPDATE SKIP LOCKED` so two relay processes can run at once without
        either blocking or double-publishing. The lock is held for the length of
        the publish, which is why the batch is small.
        """
        rows = (
            await self._s.execute(
                text(
                    """
                    SELECT id, organization_id, topic, payload, dedupe_key, attempts
                      FROM outbox
                     WHERE published_at IS NULL
                     ORDER BY id
                     LIMIT :limit
                       FOR UPDATE SKIP LOCKED
                    """
                ),
                {"limit": limit},
            )
        ).all()
        return [
            OutboxRow(
                id=r.id,
                organization_id=r.organization_id,
                topic=r.topic,
                payload=r.payload,
                dedupe_key=r.dedupe_key,
                attempts=r.attempts,
            )
            for r in rows
        ]

    async def mark_published(self, ids: list[int]) -> None:
        if not ids:
            return
        await self._s.execute(
            text(
                """
                UPDATE outbox SET published_at = now(), attempts = attempts + 1
                 WHERE id = ANY(:ids) AND published_at IS NULL
                """
            ),
            {"ids": ids},
        )

    async def mark_attempted(self, ids: list[int]) -> None:
        if not ids:
            return
        await self._s.execute(
            text("UPDATE outbox SET attempts = attempts + 1 WHERE id = ANY(:ids)"),
            {"ids": ids},
        )

    async def events_for_run(self, run_id: uuid.UUID, after_id: int = 0) -> list[dict[str, Any]]:
        """The SSE tail reads here rather than from Redis: a client that reconnects
        after a Redis wipe still gets a complete, ordered history."""
        rows = (
            await self._s.execute(
                text(
                    """
                    SELECT id, topic, payload, created_at
                      FROM events
                     WHERE run_id = :run_id AND id > :after
                     ORDER BY id
                    """
                ),
                {"run_id": run_id, "after": after_id},
            )
        ).all()
        return [
            {
                "id": r.id,
                "topic": r.topic,
                "payload": r.payload,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]
