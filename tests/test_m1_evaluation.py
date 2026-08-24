"""T17 — the evaluation deadline, AUTO_ACCEPTED, and what it must not contaminate.

T17 is the one test in M1 whose failure would be invisible in production. An
AUTO_ACCEPTED task that leaked into the acceptance numerator would raise every
acceptance metric and lower the apparent rejection rate, and the system would look
like it was getting better as it stopped being evaluated at all. §7 excludes it from
every numerator for that reason and §9 caps its share at 20%.

The rest of this module covers the outcome state machine around it: applying a
verdict, the rework cap overriding the evaluator, the human sample not rewriting
production outcomes, and the edit-distance number the dashboard trends.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import text

from runtime.artifacts.store import ArtifactStore
from runtime.domain.enums import EvaluatorKind, RejectionReason, TaskOutcome, TaskStatus
from runtime.domain.errors import OutputSchemaViolation, TaskStateError
from runtime.domain.ids import CorrelationId, OrganizationId, TaskId, new_worker_id
from runtime.domain.outputs import COMPETITOR_REPORT_V1, EvaluationVerdict
from runtime.org.department import HEAD, RESEARCH
from runtime.org.evaluation import edit_distance
from runtime.org.services import OrgServices, build_org_services
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.runtime.bootstrap import Registrar
from runtime.settings import Settings
from tests.conftest_m1 import sample_competitor_report, sample_verdict

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def org(uow_factory: UnitOfWorkFactory) -> AsyncIterator[OrganizationId]:
    organization_id = OrganizationId(uuid.uuid4())
    await Registrar(uow_factory).ensure_organization(organization_id, "acme")
    yield organization_id


@pytest_asyncio.fixture
async def services(
    settings: Settings, uow_factory: UnitOfWorkFactory
) -> AsyncIterator[OrgServices]:
    yield build_org_services(uow_factory, ArtifactStore(uow_factory, settings=settings))


async def _submitted_task(
    services: OrgServices, org: OrganizationId, title: str = "Pricing map"
) -> TaskId:
    task_id, _ = await services.tasks.create_and_assign(
        organization_id=org,
        correlation_id=CorrelationId(uuid.uuid4()),
        title=title,
        objective="Establish which vendors publish list pricing and which do not.",
        acceptance_criteria=["Every competitor named has a quoted source"],
        assignee_name=RESEARCH,
        output_schema_ref=COMPETITOR_REPORT_V1,
        task_input={},
    )
    worker = new_worker_id()
    await services.tasks.claim(task_id, worker)
    result = await services.tasks.submit(
        task_id,
        worker_id=worker,
        result=sample_competitor_report(),
        organization_id=org,
        manager_name=HEAD,
    )
    assert result.ok
    return task_id


async def _expire_deadline(uow_factory: UnitOfWorkFactory, task_id: TaskId) -> None:
    async with uow_factory.transaction() as uow:
        await uow.session.execute(
            text("UPDATE tasks SET eval_deadline = now() - interval '1 minute' WHERE id = :id"),
            {"id": task_id},
        )


# --- T17 ---------------------------------------------------------------------------


async def test_t17_a_passed_deadline_auto_accepts_and_records_why(
    services: OrgServices, org: OrganizationId, uow_factory: UnitOfWorkFactory
) -> None:
    task_id = await _submitted_task(services, org)
    await _expire_deadline(uow_factory, task_id)

    closed = await services.evaluation.auto_accept_due(manager_name=HEAD)

    assert closed == [task_id]
    task = await services.tasks.get(task_id)
    assert task is not None
    assert task.status is TaskStatus.CLOSED
    assert task.outcome is TaskOutcome.AUTO_ACCEPTED

    rows = await services.evaluation.for_task(task_id)
    assert [r["evaluator_kind"] for r in rows] == [EvaluatorKind.SYSTEM.value]
    assert "not judged" in rows[0]["reasoning"]


async def test_t17_auto_accepted_is_excluded_from_every_acceptance_metric(
    services: OrgServices, org: OrganizationId, uow_factory: UnitOfWorkFactory
) -> None:
    """The assertion the whole gate rests on.

    One genuinely accepted task and one auto-accepted one. `accepted_tasks` must be
    1, not 2; `auto_accepted_share` must be 0.5, not 0. If this ever flips, every
    number in §9 is wrong in the flattering direction and nothing else would catch
    it.
    """
    judged = await _submitted_task(services, org, title="Judged task")
    unjudged = await _submitted_task(services, org, title="Unjudged task")

    task = await services.tasks.get(judged)
    assert task is not None
    await services.evaluation.apply_verdict(
        task,
        EvaluationVerdict.model_validate(sample_verdict("ACCEPTED")),
        organization_id=org,
        manager_name=HEAD,
    )

    await _expire_deadline(uow_factory, unjudged)
    await services.evaluation.auto_accept_due(manager_name=HEAD)

    async with uow_factory() as uow:
        facts = await uow.metrics.task_facts(org)
    by_title = {f["task_id"]: f for f in facts}
    assert by_title[judged]["is_accepted"] is True
    assert by_title[unjudged]["is_accepted"] is False, "AUTO_ACCEPTED is not accepted"
    assert by_title[unjudged]["is_auto_accepted"] is True

    week = await services.metrics.for_week(org, facts[0]["week_start"])
    assert week.accepted_tasks == 1
    assert week.auto_accepted_tasks == 1
    assert week.auto_accepted_share == pytest.approx(0.5)
    # And the cost-per-accepted denominator is the accepted count, not the evaluated
    # one — an unexamined task must not make outcomes look cheaper.
    assert week.evaluated_tasks == 2


async def test_t17_a_verdict_that_lands_first_beats_the_sweep(
    services: OrgServices, org: OrganizationId, uow_factory: UnitOfWorkFactory
) -> None:
    """The sweep re-checks `status = SUBMITTED AND outcome IS NULL` inside its UPDATE.

    Without that predicate a slow sweep would overwrite genuine evaluations with
    AUTO_ACCEPTED and the acceptance metrics would decay with nothing looking wrong.
    """
    task_id = await _submitted_task(services, org)
    task = await services.tasks.get(task_id)
    assert task is not None
    await services.evaluation.apply_verdict(
        task,
        EvaluationVerdict.model_validate(sample_verdict("ACCEPTED")),
        organization_id=org,
        manager_name=HEAD,
    )
    await _expire_deadline(uow_factory, task_id)

    closed = await services.evaluation.auto_accept_due(manager_name=HEAD)

    assert closed == [], "an evaluated task is not due for auto-acceptance"
    after = await services.tasks.get(task_id)
    assert after is not None
    assert after.outcome is TaskOutcome.ACCEPTED, "the real verdict stands"


async def test_an_evaluator_cannot_return_auto_accepted(
    services: OrgServices, org: OrganizationId
) -> None:
    """AUTO_ACCEPTED means "nobody looked". An evaluator that reached the task did.

    Enforced in the schema so it cannot be produced at all, which keeps the meaning
    of the number exact — if it could arrive by two routes it would mean two things.
    """
    with pytest.raises(ValueError, match="recorded by the deadline sweeper"):
        EvaluationVerdict.model_validate(sample_verdict("AUTO_ACCEPTED"))


# --- the outcome state machine ------------------------------------------------------


async def test_the_rework_cap_overrides_the_evaluators_verdict(
    services: OrgServices, org: OrganizationId, uow_factory: UnitOfWorkFactory
) -> None:
    """An evaluator that could extend its own budget has no budget.

    The verdict is still recorded as history — "the manager wanted another cycle
    and did not get one" is a finding — but the task closes as REWORK_EXHAUSTED.
    """
    task_id = await _submitted_task(services, org)
    async with uow_factory.transaction() as uow:
        await uow.session.execute(
            text("UPDATE tasks SET rework_count = 2 WHERE id = :id"), {"id": task_id}
        )

    task = await services.tasks.get(task_id)
    assert task is not None
    applied = await services.evaluation.apply_verdict(
        task,
        EvaluationVerdict.model_validate(sample_verdict("REWORK_REQUIRED")),
        organization_id=org,
        manager_name=HEAD,
    )

    assert applied.reworked is False
    assert applied.outcome is TaskOutcome.REJECTED
    assert applied.note == RejectionReason.REWORK_EXHAUSTED.value
    assert applied.recorded, "the verdict is still history"

    rows = await services.evaluation.for_task(task_id)
    assert rows[0]["outcome"] == TaskOutcome.REWORK_REQUIRED.value
    after = await services.tasks.get(task_id)
    assert after is not None
    assert after.outcome is TaskOutcome.REJECTED


async def test_a_replayed_evaluation_does_not_double_the_denominator(
    services: OrgServices, org: OrganizationId
) -> None:
    """`uq_evaluation_once` on (task, kind, attempt).

    A retried evaluation run that appended a second row would inflate every
    denominator computed over `task_evaluations`, quietly.
    """
    task_id = await _submitted_task(services, org)
    task = await services.tasks.get(task_id)
    assert task is not None
    verdict = EvaluationVerdict.model_validate(sample_verdict("ACCEPTED"))

    first = await services.evaluation.apply_verdict(
        task, verdict, organization_id=org, manager_name=HEAD
    )
    # Replaying with the same (stale) task row is exactly what a resumed graph node
    # does: it holds the state it read before the crash. The unique constraint is
    # what stops it, not a status check on a row that is by definition out of date.
    second = await services.evaluation.apply_verdict(
        task, verdict, organization_id=org, manager_name=HEAD
    )

    assert first.recorded is True
    assert second.recorded is False
    assert second.note == "already evaluated at this attempt"
    rows = [
        r for r in await services.evaluation.for_task(task_id) if r["evaluator_kind"] == "manager"
    ]
    assert len(rows) == 1, "one verdict per attempt, however many times it is replayed"


async def test_accepted_with_edits_revalidates_the_edit_and_measures_it(
    services: OrgServices, org: OrganizationId
) -> None:
    """An editor that breaks the artifact has not accepted it with edits."""
    task_id = await _submitted_task(services, org)
    task = await services.tasks.get(task_id)
    assert task is not None

    edited = sample_competitor_report()
    edited["summary"] = edited["summary"] + " Orchestra's pricing is genuinely absent."
    applied = await services.evaluation.apply_verdict(
        task,
        EvaluationVerdict.model_validate(
            sample_verdict("ACCEPTED_WITH_EDITS", edited_output=edited)
        ),
        organization_id=org,
        manager_name=HEAD,
    )

    assert applied.outcome is TaskOutcome.ACCEPTED_WITH_EDITS
    assert applied.edit_distance is not None and applied.edit_distance > 0
    after = await services.tasks.get(task_id)
    assert after is not None
    assert after.output is not None
    assert "genuinely absent" in after.output["summary"], "the edit is what ships"


async def test_an_edit_that_breaks_the_schema_is_refused(
    services: OrgServices, org: OrganizationId
) -> None:
    task_id = await _submitted_task(services, org)
    task = await services.tasks.get(task_id)
    assert task is not None
    broken = sample_competitor_report(sources=[])

    with pytest.raises(OutputSchemaViolation, match="does not satisfy"):
        await services.evaluation.apply_verdict(
            task,
            EvaluationVerdict.model_validate(
                sample_verdict("ACCEPTED_WITH_EDITS", edited_output=broken)
            ),
            organization_id=org,
            manager_name=HEAD,
        )


# --- §8.2, the human sample ---------------------------------------------------------


async def test_a_human_review_records_a_second_row_without_rewriting_the_outcome(
    services: OrgServices, org: OrganizationId
) -> None:
    """The sample measures; it does not control.

    A review that could retroactively change production outcomes would stop being a
    measurement and start being a second control loop — and the false-accept rate
    would then be measuring itself.
    """
    task_id = await _submitted_task(services, org)
    task = await services.tasks.get(task_id)
    assert task is not None
    await services.evaluation.apply_verdict(
        task,
        EvaluationVerdict.model_validate(sample_verdict("ACCEPTED")),
        organization_id=org,
        manager_name=HEAD,
    )

    await services.evaluation.record_human_review(
        task_id,
        organization_id=org,
        outcome=TaskOutcome.REJECTED,
        reasoning="Both 'competitors' are the same company under two brand names.",
        rejection_reason=RejectionReason.QUALITY.value,
    )

    after = await services.tasks.get(task_id)
    assert after is not None
    assert after.outcome is TaskOutcome.ACCEPTED, "the manager's decision still stands"
    assert after.human_touched is True, "but a person was involved"

    kinds = {r["evaluator_kind"] for r in await services.evaluation.for_task(task_id)}
    assert kinds == {"manager", "human"}


async def test_a_manager_accept_the_human_rejects_is_counted_as_a_false_accept(
    services: OrgServices, org: OrganizationId, uow_factory: UnitOfWorkFactory
) -> None:
    """§8.2's number to watch, end to end through the view."""
    task_id = await _submitted_task(services, org)
    task = await services.tasks.get(task_id)
    assert task is not None
    await services.evaluation.apply_verdict(
        task,
        EvaluationVerdict.model_validate(sample_verdict("ACCEPTED")),
        organization_id=org,
        manager_name=HEAD,
    )
    await services.evaluation.record_human_review(
        task_id,
        organization_id=org,
        outcome=TaskOutcome.REJECTED,
        reasoning="Sources do not support the claims made from them.",
    )

    async with uow_factory() as uow:
        facts = await uow.metrics.task_facts(org)
    week = await services.metrics.for_week(org, facts[0]["week_start"])

    assert week.human_sampled_tasks == 1
    assert week.false_accepts == 1
    assert week.false_accept_rate == pytest.approx(1.0)
    assert week.human_agreement == pytest.approx(0.0)


# --- edit distance ------------------------------------------------------------------


def test_edit_distance_counts_words_not_characters_and_ignores_key_order() -> None:
    """The number a human can act on, and one that field reordering cannot inflate."""
    before = {"b": "the quick brown fox", "a": 1}
    same_reordered = {"a": 1, "b": "the quick brown fox"}
    assert edit_distance(before, same_reordered) == 0

    after = {"a": 1, "b": "the quick brown dog"}
    assert edit_distance(before, after) == 2, "one word out, one word in"


def test_edit_distance_of_an_unchanged_payload_is_zero() -> None:
    payload = sample_competitor_report()
    assert edit_distance(payload, payload) == 0


async def test_a_human_review_cannot_be_recorded_twice(
    services: OrgServices, org: OrganizationId
) -> None:
    task_id = await _submitted_task(services, org)
    task = await services.tasks.get(task_id)
    assert task is not None
    await services.evaluation.apply_verdict(
        task,
        EvaluationVerdict.model_validate(sample_verdict("ACCEPTED")),
        organization_id=org,
        manager_name=HEAD,
    )
    first = await services.evaluation.record_human_review(
        task_id, organization_id=org, outcome=TaskOutcome.ACCEPTED, reasoning="fine"
    )
    second = await services.evaluation.record_human_review(
        task_id, organization_id=org, outcome=TaskOutcome.REJECTED, reasoning="changed my mind"
    )
    assert first.recorded is True
    assert second.recorded is False, "the first review stands; §8 freezes the protocol"


async def test_a_human_review_of_auto_accepted_is_refused(
    services: OrgServices, org: OrganizationId, uow_factory: UnitOfWorkFactory
) -> None:
    """There is nothing to agree or disagree with — nobody judged it."""
    task_id = await _submitted_task(services, org)
    await _expire_deadline(uow_factory, task_id)
    await services.evaluation.auto_accept_due(manager_name=HEAD)

    with pytest.raises(TaskStateError, match="AUTO_ACCEPTED is its absence"):
        await services.evaluation.record_human_review(
            task_id,
            organization_id=org,
            outcome=TaskOutcome.AUTO_ACCEPTED,
            reasoning="cannot happen",
        )
    _ = dt
