"""The inbox.

Two conditional statements carry the whole thing.

`send()` is `ON CONFLICT DO NOTHING` on `dedupe_key`, so announcing the same fact
twice wakes an actor once. This is the same idempotency trick as `uq_run_idem` and
`uq_outbox_dedupe`, applied to actor-to-actor delivery.

`claim_pending()` is a conditional UPDATE that stamps `delivered_run_id`, so two
dispatchers racing over the same message produce one run. Delivery is claimed, not
read — a `SELECT ... WHERE status = 'PENDING'` followed by a separate update is the
lost-update race that turns one task assignment into two.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.domain.enums import MessageStatus
from runtime.domain.ids import MessageId
from runtime.persistence.json import to_jsonb

MAX_HOP_COUNT = 8
"""§6 T20. Mirrors `ck_inbox_hop_cap`; the constraint is the authority."""


@dataclass(frozen=True, slots=True)
class InboxRow:
    id: MessageId
    organization_id: uuid.UUID
    correlation_id: uuid.UUID
    causation_id: uuid.UUID | None
    kind: str
    sender_name: str | None
    recipient_name: str
    subject: str
    body: dict[str, Any]
    task_id: uuid.UUID | None
    artifact_id: uuid.UUID | None
    session_id: uuid.UUID | None
    hop_count: int
    dedupe_key: str
    status: MessageStatus
    created_at: Any


_SELECT = """
    SELECT id, organization_id, correlation_id, causation_id, kind, sender_name,
           recipient_name, subject, body, task_id, artifact_id, session_id,
           hop_count, dedupe_key, status, created_at
      FROM inbox_messages
"""


def _row(r: Any) -> InboxRow:
    return InboxRow(
        id=MessageId(r.id),
        organization_id=r.organization_id,
        correlation_id=r.correlation_id,
        causation_id=r.causation_id,
        kind=r.kind,
        sender_name=r.sender_name,
        recipient_name=r.recipient_name,
        subject=r.subject,
        body=dict(r.body or {}),
        task_id=r.task_id,
        artifact_id=r.artifact_id,
        session_id=r.session_id,
        hop_count=r.hop_count,
        dedupe_key=r.dedupe_key,
        status=MessageStatus(r.status),
        created_at=r.created_at,
    )


class InboxRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def send(self, row: dict[str, Any]) -> MessageId | None:
        """Write one message. `None` when `dedupe_key` was already used."""
        got = (
            await self._s.execute(
                text(
                    """
                    INSERT INTO inbox_messages (
                        id, organization_id, correlation_id, causation_id, kind,
                        sender_actor_id, sender_name, recipient_name, subject, body,
                        task_id, artifact_id, session_id, hop_count, dedupe_key, status,
                        drop_reason
                    ) VALUES (
                        :id, :organization_id, :correlation_id, :causation_id, :kind,
                        :sender_actor_id, :sender_name, :recipient_name, :subject,
                        CAST(:body AS jsonb), :task_id, :artifact_id, :session_id,
                        :hop_count, :dedupe_key, :status, :drop_reason
                    )
                    ON CONFLICT ON CONSTRAINT uq_inbox_dedupe DO NOTHING
                    RETURNING id
                    """
                ),
                {**row, "body": to_jsonb(row.get("body", {}))},
            )
        ).scalar_one_or_none()
        return MessageId(got) if got is not None else None

    async def get(self, message_id: MessageId) -> InboxRow | None:
        r = (
            await self._s.execute(text(_SELECT + " WHERE id = :id"), {"id": message_id})
        ).one_or_none()
        return _row(r) if r is not None else None

    async def pending(self, limit: int = 32) -> list[InboxRow]:
        rows = (
            await self._s.execute(
                text(
                    _SELECT
                    + """ WHERE status = 'PENDING'
                          ORDER BY created_at LIMIT :limit
                          FOR UPDATE SKIP LOCKED"""
                ),
                {"limit": limit},
            )
        ).all()
        return [_row(r) for r in rows]

    async def claim_delivery(self, message_id: MessageId, run_id: uuid.UUID | None) -> bool:
        """Mark a message delivered. First writer wins.

        `run_id` is nullable because a notification — a task closure, a note to a
        human — is delivered without starting anything. Leaving those PENDING would
        have the dispatcher re-examine them on every tick for the life of the
        system.
        """
        row = (
            await self._s.execute(
                text(
                    """
                    UPDATE inbox_messages
                       SET status = 'DELIVERED', delivered_run_id = :run, delivered_at = now()
                     WHERE id = :id AND status = 'PENDING'
                    RETURNING id
                    """
                ),
                {"id": message_id, "run": run_id},
            )
        ).one_or_none()
        return row is not None

    async def mark_read(self, message_id: MessageId) -> None:
        await self._s.execute(
            text("UPDATE inbox_messages SET read_at = now() WHERE id = :id AND read_at IS NULL"),
            {"id": message_id},
        )

    async def recent_for_actor(
        self, organization_id: uuid.UUID, actor_name: str, limit: int = 6
    ) -> list[InboxRow]:
        """The "last 6 messages" of the fixed context template (§2).

        Ascending by time after the limit is applied, so the prompt reads
        oldest-first while the *selection* is newest-first. Getting that backwards
        gives the model the six oldest messages in the session, which is the
        opposite of the intent and looks identical in the code.
        """
        rows = (
            await self._s.execute(
                text(
                    """
                    SELECT * FROM (
                    """
                    + _SELECT
                    + """ WHERE organization_id = :org
                            AND (recipient_name = :actor OR sender_name = :actor)
                            AND status <> 'DROPPED'
                          ORDER BY created_at DESC LIMIT :limit
                    ) recent ORDER BY created_at
                    """
                ),
                {"org": organization_id, "actor": actor_name, "limit": limit},
            )
        ).all()
        return [_row(r) for r in rows]

    async def for_session(self, session_id: uuid.UUID, limit: int = 200) -> list[InboxRow]:
        rows = (
            await self._s.execute(
                text(_SELECT + " WHERE session_id = :s ORDER BY created_at LIMIT :limit"),
                {"s": session_id, "limit": limit},
            )
        ).all()
        return [_row(r) for r in rows]

    async def count_dropped(self, correlation_id: uuid.UUID) -> int:
        return int(
            (
                await self._s.execute(
                    text(
                        "SELECT count(*) FROM inbox_messages "
                        "WHERE correlation_id = :c AND status = 'DROPPED'"
                    ),
                    {"c": correlation_id},
                )
            ).scalar_one()
        )
