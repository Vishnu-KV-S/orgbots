"""The §3 weekly loop, end to end.

    MON 08:00  cron → marketing-head.weekly_plan          COORDINATION
    MON-THU    assignees wake on task.assigned            WORK / DETERMINISTIC
               head wakes on task.submitted               EVALUATION
    THU 16:00  publish gate                               no model call
    FRI 16:00  analytics.weekly_metrics                   zero model calls
    FRI 17:00  head.weekly_summary                        SUMMARIZATION

This is the artifact M1 must produce; everything else is scaffolding. So this module
asserts on the *shape of the loop* — what woke what, in what order, at whose expense
— rather than on the content of any output, which is the scripted provider's and not
the runtime's to be right about.

The model is scripted (see `conftest_m1.ScriptedProvider`) for the same reason M0's
provider was an echo: a real model's variance would make this a test of the model
rather than of the organization. What it proves is that the plumbing turns, that the
classes land where §7 needs them, and that nothing in the chain costs a model call
that §3 says should be free.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text

from runtime.domain.enums import TaskStatus, WorkClass
from runtime.domain.specs import StartRunRequest
from runtime.org.department import ANALYTICS, CONTENT, HEAD, RESEARCH, week_of
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.settings import Settings
from tests.conftest_m1 import build_m1, new_org

pytestmark = pytest.mark.integration

MON_0800 = dt.datetime(2026, 8, 17, 8, 0, tzinfo=dt.UTC)


@pytest_asyncio.fixture
async def m1(settings: Settings) -> AsyncIterator[Any]:
    async for runtime in build_m1(settings, new_org()):
        yield runtime


async def test_the_monday_plan_creates_tasks_and_wakes_every_assignee(
    m1: Any, uow_factory: UnitOfWorkFactory
) -> None:
    """One COORDINATION call produces three tasks and three wake-ups.

    The wake-ups cost nothing — §3 lists task assignment among the steps that must
    be rule-based, and that is what licenses the conclusion "if the coordination
    ratio is high, the hierarchy is the problem".
    """
    fired = await m1.scheduler.tick(MON_0800)
    plan_fires = [f for f in fired if f.trigger == "weekly-plan" and not f.skipped]
    assert len(plan_fires) == 1

    await m1.pump(rounds=3)

    async with uow_factory() as uow:
        tasks = (
            await uow.session.execute(
                text(
                    "SELECT assignee_name, output_schema_ref, status FROM tasks "
                    "WHERE organization_id = :org ORDER BY assignee_name"
                ),
                {"org": m1.organization_id},
            )
        ).all()
        assigned = (
            await uow.session.execute(
                text(
                    "SELECT recipient_name, hop_count FROM inbox_messages "
                    "WHERE kind = 'task.assigned' AND organization_id = :org"
                ),
                {"org": m1.organization_id},
            )
        ).all()

    assert {t.assignee_name for t in tasks} == {RESEARCH, CONTENT, ANALYTICS}
    assert {t.output_schema_ref for t in tasks} == {
        "CompetitorReport@1",
        "ContentDraft@1",
        "MetricsReport@1",
    }
    assert {a.recipient_name for a in assigned} == {RESEARCH, CONTENT, ANALYTICS}
    assert all(a.hop_count == 0 for a in assigned), "the chain starts here"

    plan_calls = [c for c in m1.provider.calls if c.call_site == "head.weekly_plan"]
    assert len(plan_calls) == 1, "planning is one call, weekly"
    assert plan_calls[0].work_class == WorkClass.COORDINATION.value


async def test_the_content_task_is_wired_to_the_research_it_depends_on(
    m1: Any, uow_factory: UnitOfWorkFactory
) -> None:
    """Artifact flow between tasks (§2), resolved by the runtime rather than the
    model: the head names the dependency by title, the runtime finds the id."""
    await m1.scheduler.tick(MON_0800)
    await m1.pump(rounds=3)

    async with uow_factory() as uow:
        rows = (
            await uow.session.execute(
                text(
                    "SELECT title, assignee_name, input, parent_task_id FROM tasks "
                    "WHERE organization_id = :org"
                ),
                {"org": m1.organization_id},
            )
        ).all()
    by_who = {r.assignee_name: r for r in rows}

    content_task = by_who[CONTENT]
    assert content_task.input.get("source_task_id"), "the dependency was resolved"
    assert content_task.parent_task_id is not None
    assert str(content_task.parent_task_id) == content_task.input["source_task_id"]


async def test_the_whole_week_turns_and_lands_in_the_metrics(
    m1: Any, uow_factory: UnitOfWorkFactory
) -> None:
    """Plan → work → submit → evaluate → close, then read the numbers off.

    The single most useful assertion in the suite: it is the only one that fails if
    any link in the chain is broken, and it is the shape §12 says M1 must produce.
    """
    await m1.scheduler.tick(MON_0800)
    await m1.pump(rounds=25)

    async with uow_factory() as uow:
        tasks = (
            await uow.session.execute(
                text(
                    "SELECT assignee_name, status, outcome, submitted_at, evaluated_at "
                    "FROM tasks WHERE organization_id = :org"
                ),
                {"org": m1.organization_id},
            )
        ).all()

    submitted = [t for t in tasks if t.submitted_at is not None]
    evaluated = [t for t in tasks if t.outcome is not None]
    assert submitted, "somebody has to have done some work"
    assert evaluated, "and somebody has to have judged it"
    assert all(TaskStatus(t.status) in {TaskStatus.CLOSED, TaskStatus.ASSIGNED} for t in evaluated)

    week = await m1.org.metrics.for_week(m1.organization_id, week_of(MON_0800))
    assert week.submitted_tasks == len(submitted)
    assert week.evaluated_tasks == len(evaluated)
    assert week.total_spend_cents > 0, "and it has to have cost something"
    assert week.coordination_ratio is not None


async def test_a_submission_wakes_the_head_to_evaluate_it(
    m1: Any, uow_factory: UnitOfWorkFactory
) -> None:
    """The EVALUATION half of the loop, and the isolation §10 requires.

    The evaluation prompt must carry the rubric and the artifact, and must *not*
    carry the session summary or the recent messages — which include this same
    actor deciding the task was a good idea.
    """
    await m1.scheduler.tick(MON_0800)
    await m1.pump(rounds=25)

    evaluations = [c for c in m1.provider.calls if c.call_site == "head.evaluate"]
    assert evaluations, "the head must have evaluated something"

    for call in evaluations:
        assert call.work_class == WorkClass.EVALUATION.value
        assert "Rubric for" in call.prompt, "the rubric goes in verbatim (§8.1)"
        assert "What was produced" in call.prompt
        assert "Recent messages" not in call.prompt, (
            "an evaluator that can see itself commissioning the work is not an "
            "independent judge of it (§10)"
        )
        assert "What has happened in this session so far" not in call.prompt


async def test_the_publish_gate_asks_rather_than_publishing(
    m1: Any, uow_factory: UnitOfWorkFactory
) -> None:
    """Thursday. An accepted draft produces an approval request, not a publication.

    And the gate itself costs nothing: deciding whether an accepted draft exists is
    a query, and asking for approval is a write.
    """
    await m1.scheduler.tick(MON_0800)
    await m1.pump(rounds=25)
    calls_before = len(m1.provider.calls)

    result = await m1.service.start_run(
        StartRunRequest(
            organization_id=m1.organization_id,
            actor_name=HEAD,
            input={"mode": "publish_gate"},
            idempotency_key=f"gate-{uuid.uuid4()}",
        )
    )
    await m1.pump(rounds=6)

    async with uow_factory() as uow:
        run = (
            await uow.session.execute(
                text("SELECT status FROM runs WHERE id = :id"), {"id": result.run_id}
            )
        ).scalar_one()
        approvals = await uow.approvals.pending(m1.organization_id)
        published = (
            await uow.session.execute(
                text("SELECT count(*) FROM effect_intents WHERE tool_name = 'publish.external'")
            )
        ).scalar_one()

    assert run == "SUCCESS", "a gate that is waiting on a human is not a failed run"
    assert published == 0, "nothing was published"
    gate_calls = [c for c in m1.provider.calls[calls_before:] if c.call_site.startswith("head.")]
    assert not any(c.call_site == "head.gate" for c in gate_calls), "the gate makes no model call"
    if approvals:
        assert approvals[0].action == "publish_external"
        # M2: the department policy routes first to the director, escalating to the
    # operator. M1 had one approver and no chain.
    assert approvals[0].approver == "director"
    assert approvals[0].escalation_chain == ["director", "operator"]


async def test_the_friday_metrics_run_spends_nothing_on_models(
    m1: Any, uow_factory: UnitOfWorkFactory
) -> None:
    """The control group, on the schedule. §9's numbers come out of this run."""
    await m1.scheduler.tick(MON_0800)
    await m1.pump(rounds=25)

    result = await m1.service.start_run(
        StartRunRequest(
            organization_id=m1.organization_id,
            actor_name=ANALYTICS,
            input={"mode": "weekly_metrics"},
            idempotency_key=f"friday-{uuid.uuid4()}",
        )
    )
    await m1.pump(rounds=4)

    async with uow_factory() as uow:
        spend = (
            await uow.session.execute(
                text(
                    "SELECT COALESCE(sum(cost_cents), 0) FROM usage_ledger "
                    "WHERE run_id = :id AND kind = 'model'"
                ),
                {"id": result.run_id},
            )
        ).scalar_one()
        status = (
            await uow.session.execute(
                text("SELECT status FROM runs WHERE id = :id"), {"id": result.run_id}
            )
        ).scalar_one()
    assert status == "SUCCESS"
    assert spend == 0


async def test_the_friday_summary_produces_the_artifact_a_human_reads(
    m1: Any, uow_factory: UnitOfWorkFactory
) -> None:
    await m1.scheduler.tick(MON_0800)
    await m1.pump(rounds=25)

    await m1.service.start_run(
        StartRunRequest(
            organization_id=m1.organization_id,
            actor_name=HEAD,
            input={"mode": "weekly_summary"},
            idempotency_key=f"summary-{uuid.uuid4()}",
        )
    )
    await m1.pump(rounds=6)

    async with uow_factory() as uow:
        artifacts = (
            await uow.session.execute(
                text(
                    "SELECT kind FROM artifacts WHERE organization_id = :org "
                    "AND kind = 'task_output:WeeklySummary@1'"
                ),
                {"org": m1.organization_id},
            )
        ).all()
        note = (
            await uow.session.execute(
                text(
                    "SELECT subject, artifact_id FROM inbox_messages "
                    "WHERE recipient_name = 'operator' AND kind = 'note'"
                )
            )
        ).all()
    assert artifacts, "the summary is an artifact with a digest, not a log line"
    assert note, "and a human is told it exists"
    assert note[0].artifact_id is not None

    summaries = [c for c in m1.provider.calls if c.call_site == "head.weekly_summary"]
    assert summaries and summaries[0].work_class == WorkClass.SUMMARIZATION.value


async def test_the_loop_is_idempotent_under_replay(m1: Any, uow_factory: UnitOfWorkFactory) -> None:
    """Running the whole crank twice produces one week's work, not two.

    Every link borrows an existing idempotency: `trigger_fires.trigger_key` for the
    cron, `uq_inbox_dedupe` for the wake-ups, derived task ids for the plan, and
    `uq_run_idem` under all of them.
    """
    await m1.scheduler.tick(MON_0800)
    await m1.pump(rounds=25)

    async with uow_factory() as uow:
        first_tasks = (
            await uow.session.execute(
                text("SELECT count(*) FROM tasks WHERE organization_id = :org"),
                {"org": m1.organization_id},
            )
        ).scalar_one()
        first_evals = (
            await uow.session.execute(
                text("SELECT count(*) FROM task_evaluations WHERE organization_id = :org"),
                {"org": m1.organization_id},
            )
        ).scalar_one()

    await m1.scheduler.tick(MON_0800)
    await m1.pump(rounds=25)

    async with uow_factory() as uow:
        second_tasks = (
            await uow.session.execute(
                text("SELECT count(*) FROM tasks WHERE organization_id = :org"),
                {"org": m1.organization_id},
            )
        ).scalar_one()
        second_evals = (
            await uow.session.execute(
                text("SELECT count(*) FROM task_evaluations WHERE organization_id = :org"),
                {"org": m1.organization_id},
            )
        ).scalar_one()

    assert second_tasks == first_tasks
    assert second_evals == first_evals


async def test_every_run_in_the_week_shares_one_correlation_id(
    m1: Any, uow_factory: UnitOfWorkFactory
) -> None:
    """ "Show me everything that happened because of Monday's plan" is one query."""
    await m1.scheduler.tick(MON_0800)
    await m1.pump(rounds=25)

    async with uow_factory() as uow:
        correlations = (
            (
                await uow.session.execute(
                    text("SELECT DISTINCT correlation_id FROM tasks WHERE organization_id = :org"),
                    {"org": m1.organization_id},
                )
            )
            .scalars()
            .all()
        )
        messages = (
            (
                await uow.session.execute(
                    text(
                        "SELECT DISTINCT correlation_id FROM inbox_messages "
                        "WHERE organization_id = :org AND task_id IS NOT NULL"
                    ),
                    {"org": m1.organization_id},
                )
            )
            .scalars()
            .all()
        )

    assert len(correlations) == 1, f"one week, one chain; got {len(correlations)}"
    assert set(messages) == set(correlations)


async def test_no_message_in_the_loop_comes_close_to_the_hop_limit(
    m1: Any, uow_factory: UnitOfWorkFactory
) -> None:
    """plan → assign → submit → evaluate → close is four hops against a cap of
    eight. If the real loop ran near the cap, the cap would be load-bearing on the
    happy path rather than a backstop, and a single extra step would break it."""
    await m1.scheduler.tick(MON_0800)
    await m1.pump(rounds=25)

    async with uow_factory() as uow:
        deepest = (
            await uow.session.execute(
                text(
                    "SELECT COALESCE(max(hop_count), 0) FROM inbox_messages "
                    "WHERE organization_id = :org AND status <> 'DROPPED'"
                ),
                {"org": m1.organization_id},
            )
        ).scalar_one()
        dropped = (
            await uow.session.execute(
                text(
                    "SELECT count(*) FROM inbox_messages "
                    "WHERE organization_id = :org AND status = 'DROPPED'"
                ),
                {"org": m1.organization_id},
            )
        ).scalar_one()
    assert deepest <= 4, f"the happy path reached hop {deepest}"
    assert dropped == 0, "and nothing was cut"
