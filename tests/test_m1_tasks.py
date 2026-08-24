"""T14, T15, T16, T21 — the task lifecycle and the caps on it.

These four are the reason PR-19 ships the deterministic actor before any LLM one:
if the lifecycle is wrong, every prompt-tuning cycle afterwards is debugging the
wrong layer.

Each test is red-first in the sense that matters — it asserts on the *state of the
database* after the operation, not on the return value of the method that performed
it. A service that returned the right object while writing the wrong row would pass
a return-value assertion and fail in production a week later.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text

from runtime.artifacts.store import ArtifactStore
from runtime.domain.enums import RejectionReason, TaskOutcome, TaskStatus
from runtime.domain.ids import CorrelationId, OrganizationId, WorkerId, new_worker_id
from runtime.domain.outputs import COMPETITOR_REPORT_V1
from runtime.org.department import HEAD, RESEARCH
from runtime.org.services import OrgServices, build_org_services
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.runtime.bootstrap import Registrar
from runtime.settings import Settings
from tests.conftest_m1 import sample_competitor_report

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def org(settings: Settings, uow_factory: UnitOfWorkFactory) -> AsyncIterator[OrganizationId]:
    organization_id = OrganizationId(uuid.uuid4())
    await Registrar(uow_factory).ensure_organization(organization_id, "acme")
    yield organization_id


@pytest_asyncio.fixture
async def services(
    settings: Settings, uow_factory: UnitOfWorkFactory
) -> AsyncIterator[OrgServices]:
    yield build_org_services(uow_factory, ArtifactStore(uow_factory, settings=settings))


async def _make_task(
    services: OrgServices, org: OrganizationId, title: str = "Map competitor pricing"
) -> tuple[uuid.UUID, WorkerId]:
    correlation = CorrelationId(uuid.uuid4())
    task_id, created = await services.tasks.create_and_assign(
        organization_id=org,
        correlation_id=correlation,
        title=title,
        objective="Establish which vendors publish list pricing and which do not.",
        acceptance_criteria=["Every competitor named has a quoted source"],
        assignee_name=RESEARCH,
        output_schema_ref=COMPETITOR_REPORT_V1,
        task_input={"subject": "agent infrastructure"},
        sender=HEAD,
    )
    assert created
    worker = new_worker_id()
    assert await services.tasks.claim(task_id, worker) is not None
    return task_id, worker


# --- T14 ---------------------------------------------------------------------------


async def test_t14_a_result_failing_its_schema_does_not_reach_submitted(
    services: OrgServices, org: OrganizationId, uow_factory: UnitOfWorkFactory
) -> None:
    """A bad output must not let an assignee mark its own work done.

    The failure mode this prevents is not "an invalid row is stored" — it is the
    manager being handed something it cannot evaluate, spending an EVALUATION call
    on it, and the whole cycle producing a verdict about garbage.
    """
    task_id, worker = await _make_task(services, org)
    broken = sample_competitor_report(competitors=[])  # min_length=2

    result = await services.tasks.submit(
        task_id,
        worker_id=worker,
        result=broken,
        organization_id=org,
        manager_name=HEAD,
    )

    assert result.ok is False
    assert result.closed is False
    # Typed errors, with a pointer the assignee can act on — not a stringified
    # exception. This is what the retry prompt in `call_structured` is built from.
    assert result.errors, "a schema failure must return the errors it found"
    assert all({"pointer", "message", "kind"} <= set(e) for e in result.errors)
    assert any(e["pointer"].startswith("/competitors") for e in result.errors)

    task = await services.tasks.get(task_id)
    assert task is not None
    assert task.status is TaskStatus.IN_PROGRESS, "must not advance to SUBMITTED"
    assert task.submitted_at is None
    assert task.output is None
    assert task.schema_failures == 1

    async with uow_factory() as uow:
        notified = (
            await uow.session.execute(
                text(
                    "SELECT count(*) FROM inbox_messages "
                    "WHERE kind = 'task.submitted' AND task_id = :t"
                ),
                {"t": task_id},
            )
        ).scalar_one()
    assert notified == 0, "the manager must not be woken for work that never submitted"


async def test_a_valid_result_does_reach_submitted_and_notifies(
    services: OrgServices, org: OrganizationId, uow_factory: UnitOfWorkFactory
) -> None:
    """The other half of T14: the happy path really does move."""
    task_id, worker = await _make_task(services, org)

    result = await services.tasks.submit(
        task_id,
        worker_id=worker,
        result=sample_competitor_report(),
        organization_id=org,
        manager_name=HEAD,
    )

    assert result.ok and not result.errors
    assert result.artifact_id is not None, "a task output is always an artifact"

    task = await services.tasks.get(task_id)
    assert task is not None
    assert task.status is TaskStatus.SUBMITTED
    assert task.submitted_at is not None
    assert task.eval_deadline is not None and task.eval_deadline > task.submitted_at
    assert task.output is not None

    async with uow_factory() as uow:
        rows = (
            await uow.session.execute(
                text(
                    "SELECT recipient_name, hop_count FROM inbox_messages "
                    "WHERE kind = 'task.submitted' AND task_id = :t"
                ),
                {"t": task_id},
            )
        ).all()
    assert [(r.recipient_name, r.hop_count) for r in rows] == [(HEAD, 1)]


# --- T15 ---------------------------------------------------------------------------


async def test_t15_the_third_schema_failure_rejects_and_escalates(
    services: OrgServices, org: OrganizationId, uow_factory: UnitOfWorkFactory
) -> None:
    """Three strikes closes the task as SCHEMA_FAILURE and tells a human.

    The cap exists because an assignee that cannot produce a conforming object is
    not going to start on the fourth attempt, and each attempt costs a WORK-class
    call. Escalation is the other half: a task that dies quietly is a task nobody
    learns from.
    """
    task_id, worker = await _make_task(services, org)
    broken = sample_competitor_report(sources=[])  # min_length=3

    for attempt in (1, 2):
        result = await services.tasks.submit(
            task_id,
            worker_id=worker,
            result=broken,
            organization_id=org,
            manager_name=HEAD,
        )
        assert result.schema_failures == attempt
        assert result.closed is False

    final = await services.tasks.submit(
        task_id,
        worker_id=worker,
        result=broken,
        organization_id=org,
        manager_name=HEAD,
    )
    assert final.schema_failures == 3
    assert final.closed is True

    task = await services.tasks.get(task_id)
    assert task is not None
    assert task.status is TaskStatus.CLOSED
    assert task.outcome is TaskOutcome.REJECTED
    assert task.outcome_reason == RejectionReason.SCHEMA_FAILURE.value

    async with uow_factory() as uow:
        escalations = (
            await uow.session.execute(
                text(
                    "SELECT subject, body FROM inbox_messages "
                    "WHERE recipient_name = 'operator' AND task_id = :t"
                ),
                {"t": task_id},
            )
        ).all()
        audits = (
            (
                await uow.session.execute(
                    text(
                        "SELECT severity FROM audit_log WHERE action = 'task.schema_failure' "
                        "ORDER BY id"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(escalations) == 1, "the third failure escalates to a human"
    assert "failed its output schema" in escalations[0].body["message"]
    assert audits[-1] == "high", "the capping failure is audited at elevated severity"


async def test_the_schema_failure_cap_is_enforced_by_the_database(
    services: OrgServices, org: OrganizationId, uow_factory: UnitOfWorkFactory
) -> None:
    """`ck_task_schema_cap` is the backstop under the service logic.

    Application logic that gets the cap wrong should fail a transaction, not run a
    fourth cycle. Asserting this separately means a refactor of `TaskService` that
    dropped the check would still be caught.
    """
    from sqlalchemy.exc import IntegrityError

    task_id, _ = await _make_task(services, org)
    with pytest.raises(IntegrityError, match="ck_task_schema_cap"):
        async with uow_factory.transaction() as uow:
            await uow.session.execute(
                text("UPDATE tasks SET schema_failures = 4 WHERE id = :id"),
                {"id": task_id},
            )


async def test_a_schema_failure_at_the_model_call_lands_on_the_task(
    services: OrgServices, org: OrganizationId
) -> None:
    """The failure that actually happens in production has to reach the same row.

    `call_structured` validates inside the graph node, so an assignee that cannot
    produce a conforming object never reaches `submit` and never touched any of the
    machinery above. Runs failed on `OutputSchemaViolation` all evening on
    2026-08-24 with `last_schema_errors` NULL on every task in the database, which
    made the only useful question — *which field?* — unanswerable, and left the
    correction block both work graphs assemble from that column permanently empty.

    Same cap, deliberately: an assignee that cannot satisfy the schema is not more
    entitled to a fourth attempt because it failed early rather than late.
    """
    task_id, _ = await _make_task(services, org)
    errors = [{"pointer": "/sources", "message": "list too short", "kind": "too_short"}]

    first = await services.tasks.record_schema_failure(
        task_id, errors, organization_id=org, manager_name=HEAD
    )
    assert first is not None
    assert first.schema_failures == 1
    assert first.closed is False

    task = await services.tasks.get(task_id)
    assert task is not None
    assert task.schema_failures == 1
    assert task.last_schema_errors == errors, "the next attempt can be told what was wrong"

    for _ in range(2):
        final = await services.tasks.record_schema_failure(
            task_id, errors, organization_id=org, manager_name=HEAD
        )
    assert final is not None
    assert final.schema_failures == 3
    assert final.closed is True, "the model-side failure shares the three-strike cap"


# --- T16 ---------------------------------------------------------------------------


async def test_t16_the_third_rework_request_rejects_rather_than_cycling(
    services: OrgServices, org: OrganizationId
) -> None:
    """Rework is capped at 2. The third request closes the task instead.

    Note what is being asserted: not that `send_back_for_rework` returns False, but
    that `rework_count` stopped at 2 and the task did not go back to ASSIGNED. A
    service that returned False while still resetting the status would produce a
    task the assignee picks up and nobody ever evaluates.
    """
    task_id, worker = await _make_task(services, org)

    for cycle in (1, 2):
        submitted = await services.tasks.submit(
            task_id,
            worker_id=worker,
            result=sample_competitor_report(),
            organization_id=org,
            manager_name=HEAD,
        )
        assert submitted.ok
        sent = await services.tasks.send_back_for_rework(
            task_id,
            organization_id=org,
            instructions=[f"cycle {cycle}: name the two omitted vendors"],
            manager_name=HEAD,
        )
        assert sent is True
        task = await services.tasks.get(task_id)
        assert task is not None
        assert task.rework_count == cycle
        assert task.status is TaskStatus.ASSIGNED
        assert await services.tasks.claim(task_id, worker) is not None

    await services.tasks.submit(
        task_id,
        worker_id=worker,
        result=sample_competitor_report(),
        organization_id=org,
        manager_name=HEAD,
    )
    refused = await services.tasks.send_back_for_rework(
        task_id,
        organization_id=org,
        instructions=["cycle 3 should never happen"],
        manager_name=HEAD,
    )

    assert refused is False
    task = await services.tasks.get(task_id)
    assert task is not None
    assert task.rework_count == 2, "the counter must not reach 3"
    assert task.status is TaskStatus.SUBMITTED, "and the task must not go back out"


# --- T21 ---------------------------------------------------------------------------


async def test_t21_two_workers_claiming_one_task_produce_exactly_one_winner(
    services: OrgServices, org: OrganizationId
) -> None:
    """The task lease is a conditional UPDATE, not a read-then-write.

    Two runs for one task is the ordinary shape here — a task can be reached from a
    cron fire and from an inbox message at once — so this is not an exotic race.
    """
    import asyncio

    task_id, _ = await _make_task(services, org, title="Contended task")
    async with services.tasks._uow.transaction() as uow:
        await uow.session.execute(
            text("UPDATE tasks SET lease_until = NULL, status = 'ASSIGNED' WHERE id = :id"),
            {"id": task_id},
        )

    workers = [new_worker_id() for _ in range(5)]
    results = await asyncio.gather(*(services.tasks.claim(task_id, w) for w in workers))

    winners = [r for r in results if r is not None]
    assert len(winners) == 1, f"expected one winner, got {len(winners)}"

    task = await services.tasks.get(task_id)
    assert task is not None
    assert task.status is TaskStatus.IN_PROGRESS
    assert task.version >= 1


async def test_a_worker_that_lost_the_task_lease_cannot_submit_over_the_winner(
    services: OrgServices, org: OrganizationId
) -> None:
    """The task-level equivalent of the run fence.

    Without this, a slow worker whose lease expired would overwrite the
    replacement's submission — and because both produce valid output, nothing
    downstream would notice.
    """
    task_id, loser = await _make_task(services, org, title="Stolen task")
    async with services.tasks._uow.transaction() as uow:
        await uow.session.execute(
            text("UPDATE tasks SET lease_until = now() - interval '1 hour' WHERE id = :id"),
            {"id": task_id},
        )
    winner = new_worker_id()
    assert await services.tasks.claim(task_id, winner) is not None

    result = await services.tasks.submit(
        task_id,
        worker_id=loser,
        result=sample_competitor_report(),
        organization_id=org,
        manager_name=HEAD,
    )

    assert result.ok is False
    task = await services.tasks.get(task_id)
    assert task is not None
    assert task.status is TaskStatus.IN_PROGRESS, "the winner still holds it"
    assert task.submitted_at is None


async def test_a_task_pinned_to_an_unregistered_schema_is_refused_at_creation(
    services: OrgServices, org: OrganizationId
) -> None:
    """Fail where the mistake was made, not days later at the assignee.

    A task pinned to a schema that does not exist is unevaluable; discovering that
    at submit time puts the failure on the wrong actor and wastes a WORK call.
    """
    from runtime.domain.errors import OutputSchemaViolation

    with pytest.raises(OutputSchemaViolation, match="not registered"):
        await services.tasks.create_and_assign(
            organization_id=org,
            correlation_id=CorrelationId(uuid.uuid4()),
            title="Impossible task",
            objective="Produce something nobody has defined the shape of.",
            acceptance_criteria=["cannot be checked"],
            assignee_name=RESEARCH,
            output_schema_ref="NoSuchThing@1",
            task_input={},
        )


async def test_creating_the_same_planned_task_twice_is_idempotent(
    services: OrgServices, org: OrganizationId, uow_factory: UnitOfWorkFactory
) -> None:
    """A replayed plan node writes the same tasks, not six more.

    The plan node runs inside a graph that can be replayed after a crash, so task
    ids are derived from `(correlation_id, title)` rather than allocated.
    """
    correlation = CorrelationId(uuid.uuid4())
    kwargs = {
        "organization_id": org,
        "correlation_id": correlation,
        "title": "Map competitor pricing",
        "objective": "Establish which vendors publish list pricing and which do not.",
        "acceptance_criteria": ["Every competitor named has a quoted source"],
        "assignee_name": RESEARCH,
        "output_schema_ref": COMPETITOR_REPORT_V1,
        "task_input": {},
    }
    first_id, first_created = await services.tasks.create_and_assign(**kwargs)  # type: ignore[arg-type]
    second_id, second_created = await services.tasks.create_and_assign(**kwargs)  # type: ignore[arg-type]

    assert first_id == second_id
    assert first_created is True and second_created is False

    async with uow_factory() as uow:
        tasks = (
            await uow.session.execute(
                text("SELECT count(*) FROM tasks WHERE correlation_id = :c"),
                {"c": correlation},
            )
        ).scalar_one()
        messages = (
            await uow.session.execute(
                text(
                    "SELECT count(*) FROM inbox_messages "
                    "WHERE kind = 'task.assigned' AND task_id = :t"
                ),
                {"t": first_id},
            )
        ).scalar_one()
    assert tasks == 1
    assert messages == 1, "and the assignee is woken once, not twice"
