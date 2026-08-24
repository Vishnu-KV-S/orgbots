"""The dispatcher — inbox messages become runs.

This is the piece that makes the weekly loop turn, and it is deliberately the
dullest component in M1: a poll, a claim, a `start_run`, a mark. No model calls, no
decisions, no state of its own.

**Why the inbox and not the event stream.** Task assignment already writes a durable
`inbox_messages` row in the same transaction as the task's state change. Driving
runs off that row rather than off a Redis event means actor-to-actor delivery
inherits every property M0 built: it survives a `FLUSHALL` (R1), it is deduped by
`uq_inbox_dedupe`, and "the task is assigned" and "the assignee will be woken" cannot
disagree, because they are one write.

**Idempotency is borrowed, not invented.** The run's idempotency key is
`inbox:{dedupe_key}`, so `uq_run_idem` — the same constraint that makes cron dedupe
and API retry safe — makes redelivery safe too. Two dispatchers racing on one
message produce one run: the loser's `claim_delivery` returns False and its
`start_run` returns the winner's run unchanged.

**A message with no actor is delivered, not dropped.** Notes to `operator` and task
closures are notifications; they have no run to start. Marking them DELIVERED with a
null `delivered_run_id` keeps them out of the poll and keeps them in the record. A
message that stayed PENDING forever would be re-examined on every tick for the life
of the system.

**Who counts as an actor is a question for the database.** M1 answered it with
`ALL_ACTORS`, four names in Python, which was correct while the department was
Python. It is not correct for a company defined in YAML: a recipient outside that
frozenset is settled as `not-an-actor` and its wake-up is *silently discarded*, so an
organization whose head is called anything but `marketing-head` never runs at all.
`actors=None` — the default now — resolves the organization's active actor names per
drain, cached for the same ten seconds the kill switch uses. Passing an explicit tuple
still works and still means exactly what it did.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from dataclasses import dataclass, field

from runtime.domain.ids import OrganizationId, RunId, TaskId
from runtime.domain.specs import StartRunRequest
from runtime.observability.logging import get_logger
from runtime.org.department import ALL_ACTORS, HEAD
from runtime.org.inbox import (
    KIND_APPROVAL_DECIDED,
    KIND_TASK_ASSIGNED,
    KIND_TASK_CLOSED,
    KIND_TASK_REWORK,
    KIND_TASK_SUBMITTED,
    KIND_TRIGGER_FIRED,
)
from runtime.persistence.repositories.inbox import InboxRow
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.runtime.run_service import RunService
from runtime.settings import Settings, get_settings

log = get_logger("runtime.dispatcher")

MODE_FOR_KIND: dict[str, str] = {
    KIND_TASK_ASSIGNED: "work",
    KIND_TASK_REWORK: "work",
    KIND_TASK_SUBMITTED: "evaluate",
    KIND_APPROVAL_DECIDED: "publish_gate",
    KIND_TRIGGER_FIRED: "",  # taken from the message body
}
"""Which entry point a message wakes. `task.closed` and `note` are absent: they are
notifications, and a notification that started a run would double the loop."""

TERMINAL_KINDS = frozenset({KIND_TASK_CLOSED, "note"})

ACTOR_CACHE_TTL_SECONDS = 10.0
"""How long a resolved actor list is trusted. Same number as the kill switch's cache
and for the same reason: an actor published a moment ago starts receiving work while
somebody is still watching, and the poll does not put a query on every message."""


@dataclass
class DispatchStats:
    examined: int = 0
    started: int = 0
    skipped: int = 0
    failed: int = 0
    reasons: dict[str, int] = field(default_factory=dict)

    def note(self, reason: str) -> None:
        self.reasons[reason] = self.reasons.get(reason, 0) + 1


class Dispatcher:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        service: RunService,
        *,
        settings: Settings | None = None,
        actors: tuple[str, ...] | None = ALL_ACTORS,
    ) -> None:
        """`actors=None` resolves the recipient set from the database, per organization.

        The default is still M1's four names so that every existing caller and every
        existing test behaves exactly as before. `runtime.worker.main` passes `None`,
        which is what a company whose actors are named in YAML needs.
        """
        self._uow = uow_factory
        self._service = service
        self._settings = settings or get_settings()
        self._actors = frozenset(actors) if actors is not None else None
        self._resolved: dict[str, tuple[frozenset[str], float]] = {}
        self._stopping = asyncio.Event()
        self.stats = DispatchStats()

    async def _recipients(self, organization_id: uuid.UUID) -> frozenset[str]:
        """Who may receive a wake-up in this organization."""
        if self._actors is not None:
            return self._actors
        key = str(organization_id)
        now = time.monotonic()
        hit = self._resolved.get(key)
        if hit is not None and hit[1] > now:
            return hit[0]
        async with self._uow() as uow:
            names = frozenset(await uow.actors.active_names(organization_id))
        self._resolved[key] = (names, now + ACTOR_CACHE_TTL_SECONDS)
        return names

    async def drain(self, limit: int | None = None) -> int:
        """One pass. Returns how many runs were started."""
        batch = limit or self._settings.dispatcher_batch_size
        async with self._uow() as uow:
            pending = await uow.inbox.pending(batch)

        started = 0
        for message in pending:
            self.stats.examined += 1
            if await self._handle(message):
                started += 1
        return started

    async def _handle(self, message: InboxRow) -> bool:
        recipients = await self._recipients(message.organization_id)
        if message.kind in TERMINAL_KINDS or message.recipient_name not in recipients:
            # A notification, or addressed to a human. Settle it so the poll does
            # not re-examine it forever, and keep the row.
            await self._settle(message, run_id=None)
            self.stats.skipped += 1
            self.stats.note("notification" if message.kind in TERMINAL_KINDS else "not-an-actor")
            return False

        mode = MODE_FOR_KIND.get(message.kind)
        if mode is None:
            await self._settle(message, run_id=None)
            self.stats.skipped += 1
            self.stats.note(f"unknown-kind:{message.kind}")
            log.warning("dispatcher.unknown_kind", kind=message.kind, id=str(message.id))
            return False
        if message.kind == KIND_TRIGGER_FIRED or mode == "":
            mode = str(message.body.get("mode") or "weekly_plan")

        if (
            mode == "work"
            and message.task_id is not None
            and not await self._input_is_ready(TaskId(message.task_id))
        ):
            # Left PENDING, like a transient failure: the next tick re-examines it,
            # and the tick after the dependency lands starts the run.
            self.stats.skipped += 1
            self.stats.note("waiting-on-dependency")
            return False

        try:
            result = await self._service.start_run(
                StartRunRequest(
                    organization_id=OrganizationId(message.organization_id),
                    actor_name=message.recipient_name,
                    input={
                        "mode": mode,
                        "message_id": str(message.id),
                        "task_id": str(message.task_id) if message.task_id else None,
                        **message.body,
                    },
                    # Borrowed idempotency: one message, one run, forever.
                    idempotency_key=f"inbox:{message.dedupe_key}",
                    task_id=TaskId(message.task_id) if message.task_id else None,
                    correlation_id=str(message.correlation_id),
                    session_id=None,
                )
            )
        except Exception:
            # Left PENDING on purpose. A transient failure here — the database
            # blinked, an actor is not published yet — must be retried on the next
            # tick, and settling it would lose the wake-up permanently.
            self.stats.failed += 1
            log.exception(
                "dispatcher.start_failed",
                message_id=str(message.id),
                recipient=message.recipient_name,
                kind=message.kind,
            )
            return False

        await self._settle(message, run_id=result.run_id)
        if result.created:
            self.stats.started += 1
            log.info(
                "dispatcher.started",
                run_id=str(result.run_id),
                actor=message.recipient_name,
                mode=mode,
                kind=message.kind,
                task_id=str(message.task_id) if message.task_id else None,
                correlation_id=str(message.correlation_id),
            )
        else:
            self.stats.skipped += 1
            self.stats.note("already-started")
        return result.created

    async def _input_is_ready(self, task_id: TaskId) -> bool:
        """Does this task's declared dependency have an output yet?

        The head plans a week in one pass — research *and* the piece written from
        it — and assigns both, so both wake-ups land in the same tick. Nothing used
        to hold the second one back: on 2026-08-24 `content` started 2m14s before
        the research it names finished, found a NULL output, and drafted anyway.
        Every content failure that evening was this, and the runs that *didn't*
        fail were worse — the model satisfied `ContentDraft`'s mandatory `sources`
        by inventing `https://example.com/...` citations.

        Holding the message rather than failing the run is deliberate: the
        dependency is usually minutes away, the wake-up is durable, and a task that
        waits two ticks costs nothing. It is the assignee's graph that fails
        (`UpstreamNotReady`) if the upstream finishes with nothing — waiting
        forever on a dependency that is already closed would be the other bug.

        Unknown means ready. A task with no `source_task_id`, a dangling id, a row
        that has not been written yet — none of those is a dependency this can
        reason about, and a dispatcher that refuses to dispatch what it cannot
        parse stops the organization instead of one task.
        """
        async with self._uow() as uow:
            task = await uow.tasks.get(task_id)
            if task is None:
                return True
            upstream_id = (task.input or {}).get("source_task_id")
            if not upstream_id:
                return True
            try:
                upstream = await uow.tasks.get(TaskId(uuid.UUID(str(upstream_id))))
            except ValueError:
                log.warning(
                    "dispatcher.unparseable_dependency",
                    task_id=str(task_id),
                    source_task_id=str(upstream_id),
                )
                return True
            if upstream is None or upstream.output is not None:
                return True
            if upstream.status.is_terminal:
                # Closed with no output. Nothing to wait for, and the assignee is
                # the one that has to say so — it knows what an empty dependency
                # means for the schema it is pinned to.
                log.warning(
                    "dispatcher.dependency_closed_empty",
                    task_id=str(task_id),
                    upstream_task=str(upstream.id),
                    upstream_status=upstream.status.value,
                    upstream_outcome=upstream.outcome.value if upstream.outcome else None,
                )
                return True
            log.info(
                "dispatcher.waiting_on_dependency",
                task_id=str(task_id),
                upstream_task=str(upstream.id),
                upstream_status=upstream.status.value,
            )
            return False

    async def _settle(self, message: InboxRow, run_id: RunId | None) -> None:
        """Mark the message delivered. `run_id` is None for a notification.

        The column is nullable and a delivered message with no run is exactly what a
        notification is — resist "fixing" this into a synthetic uuid, which would
        make `delivered_run_id` point at a run that does not exist.
        """
        async with self._uow.transaction() as uow:
            await uow.inbox.claim_delivery(message.id, run_id)

    async def run_forever(self) -> None:
        while not self._stopping.is_set():
            try:
                worked = await self.drain()
            except Exception:
                log.exception("dispatcher.loop_error")
                worked = 0
            if not worked:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._stopping.wait(),
                        timeout=self._settings.dispatcher_interval_seconds,
                    )

    def stop(self) -> None:
        self._stopping.set()


async def evaluate_submissions(
    uow_factory: UnitOfWorkFactory, service: RunService, organization_id: OrganizationId
) -> int:
    """Wake the manager for anything submitted but not yet evaluated.

    A backstop, not the main path — `task.submitted` messages normally do this. It
    exists because the failure it covers is silent and expensive: a lost message
    means a task sits SUBMITTED until `eval_deadline` and then becomes
    AUTO_ACCEPTED, which inflates the one share §9 caps at 20% while looking like
    nothing went wrong.
    """
    from runtime.domain.enums import TaskStatus

    started = 0
    async with uow_factory() as uow:
        open_tasks = await uow.tasks.open_for_org(organization_id)

    for task in open_tasks:
        if task.status is not TaskStatus.SUBMITTED or task.outcome is not None:
            continue
        result = await service.start_run(
            StartRunRequest(
                organization_id=organization_id,
                actor_name=HEAD,
                input={"mode": "evaluate", "task_id": str(task.id)},
                idempotency_key=f"evaluate:{task.id}:{task.rework_count}",
                task_id=task.id,
                correlation_id=str(task.correlation_id),
            )
        )
        if result.created:
            started += 1
            log.info("dispatcher.evaluation_backstop", task_id=str(task.id))
    return started


__all__ = ["DispatchStats", "Dispatcher", "evaluate_submissions"]
