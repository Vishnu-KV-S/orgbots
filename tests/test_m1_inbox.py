"""T20 — the hop limit, and the dispatcher that turns messages into runs.

A message loop is the failure mode that costs money while looking like activity.
A→B→A→B produces real tasks, real model calls and a rising spend, and every
individual step is correct. The only thing that stops it is a counter.

T20 asserts on the *dropped row*, not on the absence of a row. Absence is also what
a bug looks like — a send that silently failed, a recipient nobody matched — and a
test that cannot tell those apart is not testing the hop limit.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text

from runtime.domain.enums import MessageStatus
from runtime.domain.errors import HopLimitExceeded
from runtime.domain.ids import CorrelationId, OrganizationId
from runtime.org.department import CONTENT, HEAD, RESEARCH
from runtime.org.inbox import (
    KIND_NOTE,
    KIND_TASK_ASSIGNED,
    KIND_TASK_CLOSED,
    InboxService,
    dedupe_key,
)
from runtime.persistence.repositories.inbox import MAX_HOP_COUNT
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.runtime.bootstrap import Registrar
from runtime.settings import Settings
from tests.conftest_m1 import build_m1, new_org

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def org(uow_factory: UnitOfWorkFactory) -> AsyncIterator[OrganizationId]:
    organization_id = OrganizationId(uuid.uuid4())
    await Registrar(uow_factory).ensure_organization(organization_id, "acme")
    yield organization_id


@pytest_asyncio.fixture
async def inbox(uow_factory: UnitOfWorkFactory) -> AsyncIterator[InboxService]:
    yield InboxService(uow_factory)


# --- T20 ---------------------------------------------------------------------------


async def test_t20_a_message_loop_stops_at_the_hop_limit(
    inbox: InboxService, org: OrganizationId, uow_factory: UnitOfWorkFactory
) -> None:
    """A→B→A→B… terminates at hop 8, and says so in a row anyone can find."""
    correlation = CorrelationId(uuid.uuid4())
    # Each turn of the chain answers the one before it, so the hop count *is* the
    # index — which is the property that makes the cap terminate a ping-pong rather
    # than merely slow it down.
    sent = 0
    for hop in range(20):
        sender, recipient = (RESEARCH, CONTENT) if hop % 2 == 0 else (CONTENT, RESEARCH)
        result = await inbox.send(
            organization_id=org,
            kind=KIND_NOTE,
            recipient=recipient,
            sender=sender,
            correlation_id=correlation,
            key=dedupe_key("loop", correlation, hop),
            subject=f"ping {hop}",
            hop_count=hop,
        )
        if result.dropped:
            assert hop > MAX_HOP_COUNT
            break
        sent += 1
    else:  # pragma: no cover - the loop must break
        pytest.fail("the chain never hit the hop limit")

    assert sent == MAX_HOP_COUNT + 1, "hops 0..8 go through; the 9th does not"

    async with uow_factory() as uow:
        rows = (
            await uow.session.execute(
                text(
                    "SELECT status, hop_count, drop_reason FROM inbox_messages "
                    "WHERE correlation_id = :c ORDER BY created_at"
                ),
                {"c": correlation},
            )
        ).all()
    delivered = [r for r in rows if r.status != MessageStatus.DROPPED.value]
    dropped = [r for r in rows if r.status == MessageStatus.DROPPED.value]

    assert len(delivered) == MAX_HOP_COUNT + 1
    assert len(dropped) == 1, "the refusal is a row, not an absence"
    assert dropped[0].hop_count == MAX_HOP_COUNT
    assert "hop limit" in dropped[0].drop_reason
    assert str(correlation) in dropped[0].drop_reason, "and it names the chain it cut"


async def test_the_hop_cap_is_enforced_by_the_database_too(
    org: OrganizationId, uow_factory: UnitOfWorkFactory
) -> None:
    """`ck_inbox_hop_cap` under the service check, for the same reason as the
    rework cap: a refactor that dropped the service check must still fail."""
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError, match="ck_inbox_hop_cap"):
        async with uow_factory.transaction() as uow:
            await uow.session.execute(
                text(
                    """
                    INSERT INTO inbox_messages (id, organization_id, correlation_id, kind,
                                                recipient_name, dedupe_key, hop_count)
                    VALUES (:id, :org, :c, 'note', 'research', :k, 9)
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "org": org,
                    "c": uuid.uuid4(),
                    "k": f"over-cap-{uuid.uuid4()}",
                },
            )


async def test_send_or_raise_is_the_form_for_an_escalation(
    inbox: InboxService, org: OrganizationId
) -> None:
    """A dropped escalation is worse than a failed run, so that caller opts in."""
    with pytest.raises(HopLimitExceeded):
        await inbox.send_or_raise(
            organization_id=org,
            kind=KIND_NOTE,
            recipient="operator",
            correlation_id=CorrelationId(uuid.uuid4()),
            key=dedupe_key("escalation", uuid.uuid4()),
            hop_count=MAX_HOP_COUNT + 1,
        )


# --- dedupe -------------------------------------------------------------------------


async def test_the_same_fact_announced_twice_wakes_an_actor_once(
    inbox: InboxService, org: OrganizationId
) -> None:
    correlation = CorrelationId(uuid.uuid4())
    key = dedupe_key(KIND_TASK_ASSIGNED, "some-task")
    first = await inbox.send(
        organization_id=org,
        kind=KIND_TASK_ASSIGNED,
        recipient=RESEARCH,
        correlation_id=correlation,
        key=key,
        subject="a task",
    )
    second = await inbox.send(
        organization_id=org,
        kind=KIND_TASK_ASSIGNED,
        recipient=RESEARCH,
        correlation_id=correlation,
        key=key,
        subject="a task",
    )
    assert first.created is True
    assert second.created is False and second.reason == "duplicate"


async def test_an_unknown_message_kind_is_refused_rather_than_ignored(
    inbox: InboxService, org: OrganizationId
) -> None:
    """An actor that silently ignores kinds it does not recognise is a loop that
    stalls without anybody noticing."""
    with pytest.raises(ValueError, match="unknown message kind"):
        await inbox.send(
            organization_id=org,
            kind="task.invented",
            recipient=RESEARCH,
            correlation_id=CorrelationId(uuid.uuid4()),
            key=dedupe_key("x"),
        )


async def test_recent_messages_are_the_newest_six_in_oldest_first_order(
    inbox: InboxService, org: OrganizationId
) -> None:
    """The §2 context window. Getting the ordering backwards gives the model the
    six *oldest* messages and looks identical in the code."""
    correlation = CorrelationId(uuid.uuid4())
    for i in range(10):
        await inbox.send(
            organization_id=org,
            kind=KIND_NOTE,
            recipient=RESEARCH,
            correlation_id=correlation,
            key=dedupe_key("window", i),
            subject=f"message {i}",
        )

    window = await inbox.recent_for(org, RESEARCH, limit=6)

    assert [m.subject for m in window] == [f"message {i}" for i in range(4, 10)]


# --- the dispatcher ------------------------------------------------------------------


@pytest_asyncio.fixture
async def m1(settings: Settings) -> AsyncIterator[object]:
    async for runtime in build_m1(settings, new_org(), with_triggers=False):
        yield runtime


async def test_the_dispatcher_starts_one_run_per_message_and_settles_it(
    m1, uow_factory: UnitOfWorkFactory
) -> None:
    correlation = CorrelationId(uuid.uuid4())
    await m1.org.inbox.send(
        organization_id=m1.organization_id,
        kind=KIND_TASK_ASSIGNED,
        recipient=RESEARCH,
        correlation_id=correlation,
        key=dedupe_key("dispatch-once", correlation),
        subject="do a thing",
        body={"objective": "a thing"},
    )

    started = await m1.dispatcher.drain()
    again = await m1.dispatcher.drain()

    assert started == 1
    assert again == 0, "a settled message is not re-examined"

    async with uow_factory() as uow:
        row = (
            await uow.session.execute(
                text(
                    "SELECT status, delivered_run_id FROM inbox_messages WHERE correlation_id = :c"
                ),
                {"c": correlation},
            )
        ).one()
        run = (
            await uow.session.execute(
                text("SELECT actor_id, status FROM runs WHERE id = :id"),
                {"id": row.delivered_run_id},
            )
        ).one()
    assert row.status == MessageStatus.DELIVERED.value
    assert row.delivered_run_id is not None
    assert run.status == "QUEUED"


async def test_two_dispatchers_racing_one_message_produce_one_run(m1, settings) -> None:
    """Borrowed idempotency: `inbox:{dedupe_key}` collides on `uq_run_idem`."""
    import asyncio

    from runtime.runtime.dispatcher import Dispatcher

    correlation = CorrelationId(uuid.uuid4())
    await m1.org.inbox.send(
        organization_id=m1.organization_id,
        kind=KIND_TASK_ASSIGNED,
        recipient=RESEARCH,
        correlation_id=correlation,
        key=dedupe_key("contended", correlation),
        subject="contended",
    )
    dispatchers = [Dispatcher(m1.uow, m1.service, settings=settings) for _ in range(4)]

    results = await asyncio.gather(*(d.drain() for d in dispatchers))

    assert sum(results) == 1, f"exactly one run, got {sum(results)}"


async def test_a_notification_is_settled_without_starting_a_run(
    m1, uow_factory: UnitOfWorkFactory
) -> None:
    """A `task.closed` message or a note to a human has nothing to wake.

    Left PENDING it would be re-examined on every tick forever; started as a run it
    would double the loop.
    """
    correlation = CorrelationId(uuid.uuid4())
    await m1.org.inbox.send(
        organization_id=m1.organization_id,
        kind=KIND_TASK_CLOSED,
        recipient=RESEARCH,
        correlation_id=correlation,
        key=dedupe_key("closure", correlation),
        subject="your task was accepted",
    )
    await m1.org.inbox.send(
        organization_id=m1.organization_id,
        kind=KIND_NOTE,
        recipient="operator",
        correlation_id=correlation,
        key=dedupe_key("note", correlation),
        subject="the weekly summary is ready",
    )
    # A kind that *would* start a run, addressed to a human. This is the escalation
    # path — a capped task tells `operator` — and it must settle rather than fail to
    # resolve an actor that does not exist.
    await m1.org.inbox.send(
        organization_id=m1.organization_id,
        kind=KIND_TASK_ASSIGNED,
        recipient="operator",
        correlation_id=correlation,
        key=dedupe_key("escalation", correlation),
        subject="a task needs your attention",
    )

    started = await m1.dispatcher.drain()

    assert started == 0
    async with uow_factory() as uow:
        rows = (
            await uow.session.execute(
                text(
                    "SELECT status, delivered_run_id FROM inbox_messages WHERE correlation_id = :c"
                ),
                {"c": correlation},
            )
        ).all()
    assert len(rows) == 3
    assert {r.status for r in rows} == {MessageStatus.DELIVERED.value}
    assert all(r.delivered_run_id is None for r in rows)
    # Two reasons, and the distinction is worth keeping: a notification is settled
    # because of what it *is*, an escalation because of who it is *for*.
    assert m1.dispatcher.stats.reasons["notification"] == 2
    assert m1.dispatcher.stats.reasons["not-an-actor"] == 1


async def test_a_submitted_message_wakes_the_head_in_evaluate_mode(
    m1, uow_factory: UnitOfWorkFactory
) -> None:
    """The one routing decision the dispatcher makes that is not the identity."""
    from runtime.org.inbox import KIND_TASK_SUBMITTED

    correlation = CorrelationId(uuid.uuid4())
    await m1.org.inbox.send(
        organization_id=m1.organization_id,
        kind=KIND_TASK_SUBMITTED,
        recipient=HEAD,
        correlation_id=correlation,
        key=dedupe_key("submitted", correlation),
        subject="submitted: a thing",
        hop_count=1,
    )

    assert await m1.dispatcher.drain() == 1

    async with uow_factory() as uow:
        spec = (
            await uow.session.execute(
                text(
                    """
                    SELECT rs.spec FROM run_specs rs
                      JOIN runs r ON r.id = rs.run_id
                     WHERE r.organization_id = :org
                    """
                ),
                {"org": m1.organization_id},
            )
        ).scalar_one()
    assert spec["input"]["mode"] == "evaluate"


async def test_a_task_waits_for_the_dependency_it_declares(
    m1, uow_factory: UnitOfWorkFactory
) -> None:
    """The content piece is not dispatched until its research has an output.

    The head plans a week in one pass and assigns every task in it, so the wake-up
    for the piece and the wake-up for the research it is written from land in the
    same tick. Nothing used to hold the second one back: on 2026-08-24 `content`
    started 2m14s ahead of the research it names, found a NULL output and drafted
    anyway. Every content failure that evening was this — and the runs that did
    *not* fail were the worse outcome, because `ContentDraft` requires `sources`
    and the model satisfied it with invented `https://example.com/...` citations.

    Held, not failed: the message stays PENDING and the next tick reconsiders it.
    """
    from runtime.domain.outputs import COMPETITOR_REPORT_V1, CONTENT_DRAFT_V1

    correlation = CorrelationId(uuid.uuid4())
    upstream, _ = await m1.org.tasks.create_and_assign(
        organization_id=m1.organization_id,
        correlation_id=correlation,
        title="Map competitor pricing",
        objective="Establish which vendors publish list pricing.",
        acceptance_criteria=["Every competitor named has a quoted source"],
        assignee_name=RESEARCH,
        output_schema_ref=COMPETITOR_REPORT_V1,
        task_input={"subject": "agent infrastructure"},
        sender=HEAD,
    )
    downstream, _ = await m1.org.tasks.create_and_assign(
        organization_id=m1.organization_id,
        correlation_id=correlation,
        title="Write the pricing piece",
        objective="A blog post from the pricing research.",
        acceptance_criteria=["Every claim cites the research"],
        assignee_name=CONTENT,
        output_schema_ref=CONTENT_DRAFT_V1,
        task_input={"channel": "blog"},
        sender=HEAD,
    )
    assert await m1.org.tasks.set_dependency(downstream, upstream)

    started = await m1.dispatcher.drain()
    assert started == 1, "the research starts; the piece written from it does not"

    async with uow_factory() as uow:
        held = (
            await uow.session.execute(
                text("SELECT status FROM inbox_messages WHERE task_id = :t"),
                {"t": downstream},
            )
        ).scalar_one()
        runs = (
            await uow.session.execute(
                text("SELECT count(*) FROM runs WHERE task_id = :t"), {"t": downstream}
            )
        ).scalar_one()
    assert held == MessageStatus.PENDING.value, "the wake-up is kept, not spent"
    assert runs == 0, "no run was started against an empty dependency"

    async with uow_factory.transaction() as uow:
        await uow.session.execute(
            text("UPDATE tasks SET output = '{}'::jsonb WHERE id = :id"), {"id": upstream}
        )

    assert await m1.dispatcher.drain() == 1, "the output lands and the next tick dispatches"
