"""Sending messages between actors.

Every send is deduped on a caller-supplied `dedupe_key` and carries a `hop_count`
one greater than the message that caused it. Those two facts are what make the
event fabric safe to be at-least-once and impossible to loop forever.

The hop limit is enforced *before* the insert and the refusal is written down. A
message that would exceed the cap is stored with `status = DROPPED` and a reason,
which is the difference between "the loop stopped" and "the loop stopped and we can
prove it and say where". T20 asserts on the dropped row, not on the absence of a
row, because absence is also what a bug looks like.

None of the sends here cost a model call. §3 is explicit that task assignment,
submission notification, dependency completion and status rollup are rule-based —
if the coordination ratio comes out high anyway, the hierarchy is the problem and
not the plumbing, and that conclusion is only available if the plumbing is provably
free.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import Any

from runtime.domain.enums import MessageStatus
from runtime.domain.errors import HopLimitExceeded
from runtime.domain.ids import (
    ArtifactId,
    CorrelationId,
    MessageId,
    OrganizationId,
    SessionId,
    TaskId,
    new_message_id,
)
from runtime.observability.logging import get_logger
from runtime.persistence.repositories.inbox import MAX_HOP_COUNT, InboxRow
from runtime.persistence.uow import UnitOfWork, UnitOfWorkFactory

log = get_logger("org.inbox")

# The message kinds the weekly loop runs on. A closed set: an unknown kind reaching
# an actor is a bug, and an actor that silently ignores kinds it does not recognise
# is a loop that stalls without anybody noticing.
KIND_TASK_ASSIGNED = "task.assigned"
KIND_TASK_SUBMITTED = "task.submitted"
KIND_TASK_REWORK = "task.rework"
KIND_TASK_CLOSED = "task.closed"
KIND_TRIGGER_FIRED = "trigger.fired"
KIND_APPROVAL_DECIDED = "approval.decided"
KIND_NOTE = "note"
KIND_BOT_CONTINUE = "bot.continue"
"""A bot's long task carrying on in a fresh run. Addressed by the bot to its own actor;
the dispatcher starts the run. See `runtime.org.bots.BotService.continue_later`."""

KNOWN_KINDS = frozenset(
    {
        KIND_TASK_ASSIGNED,
        KIND_TASK_SUBMITTED,
        KIND_TASK_REWORK,
        KIND_TASK_CLOSED,
        KIND_TRIGGER_FIRED,
        KIND_APPROVAL_DECIDED,
        KIND_NOTE,
        KIND_BOT_CONTINUE,
    }
)


def dedupe_key(*parts: object) -> str:
    """A stable key from the facts that make a message unique.

    Hashed rather than concatenated so the key has a bounded length regardless of
    what a caller passes, and so a part containing the separator cannot forge a
    collision with a different tuple.
    """
    material = "\x1f".join(str(p) for p in parts)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class SendResult:
    message_id: MessageId | None
    created: bool
    dropped: bool
    reason: str | None = None

    @property
    def delivered_somewhere(self) -> bool:
        return self.message_id is not None and not self.dropped


class InboxService:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow = uow_factory

    async def send(
        self,
        *,
        organization_id: OrganizationId,
        kind: str,
        recipient: str,
        correlation_id: CorrelationId,
        key: str,
        subject: str = "",
        body: dict[str, Any] | None = None,
        sender: str | None = None,
        sender_actor_id: uuid.UUID | None = None,
        task_id: TaskId | None = None,
        artifact_id: ArtifactId | None = None,
        session_id: SessionId | None = None,
        causation_id: MessageId | None = None,
        hop_count: int = 0,
        uow: UnitOfWork | None = None,
    ) -> SendResult:
        """Send one message.

        Pass `uow` to enlist in the caller's transaction — which task assignment
        does, so that "the task is ASSIGNED" and "the assignee was told" are one
        write or neither. Omit it and the send gets its own transaction.
        """
        if kind not in KNOWN_KINDS:
            raise ValueError(f"unknown message kind {kind!r}; known: {sorted(KNOWN_KINDS)}")

        if hop_count > MAX_HOP_COUNT:
            return await self._drop(
                organization_id=organization_id,
                kind=kind,
                recipient=recipient,
                correlation_id=correlation_id,
                key=key,
                subject=subject,
                body=body,
                sender=sender,
                task_id=task_id,
                causation_id=causation_id,
                uow=uow,
            )

        row = {
            "id": new_message_id(),
            "organization_id": organization_id,
            "correlation_id": correlation_id,
            "causation_id": causation_id,
            "kind": kind,
            "sender_actor_id": sender_actor_id,
            "sender_name": sender,
            "recipient_name": recipient,
            "subject": subject,
            "body": body or {},
            "task_id": task_id,
            "artifact_id": artifact_id,
            "session_id": session_id,
            "hop_count": hop_count,
            "dedupe_key": key,
            "status": MessageStatus.PENDING.value,
            "drop_reason": None,
        }
        if uow is not None:
            created = await uow.inbox.send(row)
        else:
            async with self._uow.transaction() as own:
                created = await own.inbox.send(row)
        if created is None:
            log.debug("inbox.duplicate", kind=kind, recipient=recipient, dedupe_key=key[:16])
            return SendResult(message_id=None, created=False, dropped=False, reason="duplicate")
        return SendResult(message_id=created, created=True, dropped=False)

    async def reply_hop(self, cause: InboxRow | None) -> int:
        """The hop count a reply to `cause` should carry.

        A message with no cause starts a chain at 0. Everything else is one more
        than what it is answering — which is what makes A→B→A→B terminate rather
        than restart the count each time it changes direction.
        """
        return 0 if cause is None else cause.hop_count + 1

    async def _drop(self, *, uow: UnitOfWork | None, **fields: Any) -> SendResult:
        reason = (
            f"hop limit {MAX_HOP_COUNT} exceeded; the chain from correlation "
            f"{fields['correlation_id']} is cut here"
        )
        row = {
            "id": new_message_id(),
            "organization_id": fields["organization_id"],
            "correlation_id": fields["correlation_id"],
            "causation_id": fields.get("causation_id"),
            "kind": fields["kind"],
            "sender_actor_id": None,
            "sender_name": fields.get("sender"),
            "recipient_name": fields["recipient"],
            "subject": fields.get("subject", ""),
            "body": fields.get("body") or {},
            "task_id": fields.get("task_id"),
            "artifact_id": None,
            "session_id": None,
            # Stored at the cap rather than above it: `ck_inbox_hop_cap` refuses
            # anything higher, and the row must survive to be the evidence.
            "hop_count": MAX_HOP_COUNT,
            "dedupe_key": fields["key"],
            "status": MessageStatus.DROPPED.value,
            "drop_reason": reason,
        }
        if uow is not None:
            await uow.inbox.send(row)
        else:
            async with self._uow.transaction() as own:
                await own.inbox.send(row)
        log.warning(
            "inbox.hop_limit",
            kind=fields["kind"],
            recipient=fields["recipient"],
            correlation_id=str(fields["correlation_id"]),
            max_hop_count=MAX_HOP_COUNT,
        )
        return SendResult(
            message_id=MessageId(row["id"]), created=True, dropped=True, reason=reason
        )

    async def send_or_raise(self, **kwargs: Any) -> SendResult:
        """`send()` for callers that treat a dropped message as a failure.

        The dispatcher does not; a graph node that is trying to escalate to a human
        does, because a silently dropped escalation is worse than a failed run.
        """
        result = await self.send(**kwargs)
        if result.dropped:
            raise HopLimitExceeded(result.reason or "hop limit exceeded")
        return result

    # --- reads ---------------------------------------------------------------------

    async def get(self, message_id: MessageId) -> InboxRow | None:
        async with self._uow() as uow:
            return await uow.inbox.get(message_id)

    async def recent_for(
        self, organization_id: OrganizationId, actor_name: str, limit: int = 6
    ) -> list[InboxRow]:
        """The last N messages for the fixed context template (§2)."""
        async with self._uow() as uow:
            return await uow.inbox.recent_for_actor(organization_id, actor_name, limit)

    async def mark_read(self, message_id: MessageId) -> None:
        async with self._uow.transaction() as uow:
            await uow.inbox.mark_read(message_id)
