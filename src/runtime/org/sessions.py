"""Sessions and their summaries.

A session is one actor's continuing thread for one week. The key is derived —
`{actor}:{iso-week}` — rather than allocated, so two runs of the same actor on the
same Tuesday land in the same session without a lookup-then-insert race, and the
weekly loop gets a natural boundary for free.

This module owns *persistence* of summaries, not the model call that produces them.
The call lives in `runtime.graphs.common.summarize`, because a model call needs the
`ModelGateway` and `runtime.org` sits below the gateway in the layer contract. The
split is not bureaucratic: it means the summary text can be tested without a
provider, and the gateway accounting can be tested without a session.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from runtime.domain.ids import OrganizationId, RunId, SessionId
from runtime.observability.logging import get_logger
from runtime.persistence.repositories.inbox import InboxRow
from runtime.persistence.uow import UnitOfWorkFactory

log = get_logger("org.sessions")

SUMMARIZE_EVERY = 12
"""Messages between summaries.

Twice the six-message window the context template carries (§2), so a summary always
covers ground the window has already fallen off the back of. Lower and the
SUMMARIZATION class starts to dominate the coordination ratio for no benefit;
higher and the actor loses the thread.
"""


def session_key(actor_name: str, when: dt.datetime) -> str:
    """`{actor}:{iso-year}-W{week}`. Derived, so it needs no coordination."""
    iso = when.astimezone(dt.UTC).isocalendar()
    return f"{actor_name}:{iso.year}-W{iso.week:02d}"


def session_id_for(organization_id: OrganizationId, key: str) -> SessionId:
    return SessionId(uuid.uuid5(uuid.NAMESPACE_URL, f"session:{organization_id}:{key}"))


@dataclass(frozen=True, slots=True)
class SessionView:
    """What a graph node needs to know about its session, in one object."""

    session_id: SessionId
    key: str
    message_count: int
    summary: str | None
    summarized_upto: int

    @property
    def needs_summary(self) -> bool:
        return self.message_count - self.summarized_upto >= SUMMARIZE_EVERY


class SessionService:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow = uow_factory

    async def open(
        self,
        organization_id: OrganizationId,
        actor_name: str,
        *,
        correlation_id: uuid.UUID | None = None,
        now: dt.datetime | None = None,
    ) -> SessionView:
        key = session_key(actor_name, now or dt.datetime.now(dt.UTC))
        session_id = session_id_for(organization_id, key)
        async with self._uow.transaction() as uow:
            resolved = await uow.sessions.ensure(
                session_id, organization_id, actor_name, key, correlation_id
            )
            row = await uow.sessions.get(resolved)
            summary = await uow.sessions.latest_summary(resolved)
        assert row is not None  # ensure() just wrote it
        return SessionView(
            session_id=resolved,
            key=key,
            message_count=row.message_count,
            summary=summary.summary if summary else None,
            summarized_upto=row.summarized_upto,
        )

    async def get(self, session_id: SessionId) -> SessionView | None:
        async with self._uow() as uow:
            row = await uow.sessions.get(session_id)
            if row is None:
                return None
            summary = await uow.sessions.latest_summary(session_id)
        return SessionView(
            session_id=session_id,
            key=row.session_key,
            message_count=row.message_count,
            summary=summary.summary if summary else None,
            summarized_upto=row.summarized_upto,
        )

    async def note_activity(self, session_id: SessionId, messages: int = 1) -> int:
        async with self._uow.transaction() as uow:
            return await uow.sessions.bump_message_count(session_id, messages)

    async def record_summary(
        self,
        session_id: SessionId,
        *,
        upto_message_count: int,
        summary: str,
        run_id: RunId | None = None,
        input_tokens: int = 0,
        output_tokens: int = 0,
        cost_cents: int = 0,
    ) -> bool:
        """Store a summary. False when this cut point already has one.

        The idempotency matters for cost, not just for tidiness: a replayed
        summarize node that wrote a second row would also have made a second
        SUMMARIZATION model call, and that call shows up in the coordination ratio.
        """
        async with self._uow.transaction() as uow:
            written = await uow.sessions.add_summary(
                session_id,
                upto_message_count=upto_message_count,
                summary=summary,
                run_id=run_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_cents=cost_cents,
            )
        if written is not None:
            log.info(
                "session.summarized",
                session_id=str(session_id),
                upto=upto_message_count,
                cost_cents=cost_cents,
            )
        return written is not None

    async def transcript(self, session_id: SessionId, limit: int = 200) -> list[InboxRow]:
        async with self._uow() as uow:
            return await uow.inbox.for_session(session_id, limit)
