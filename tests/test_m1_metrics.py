"""T26 — the metric views, against numbers computed by hand.

*"T26 matters more than it looks. If the metric SQL is wrong, every decision
downstream is wrong, and nothing else will catch it."*

So the fixture below is written out explicitly rather than produced by running the
system, and every expected value is arithmetic a reader can check in the docstring
next to the assertion. A test that computed its expectations the same way the view
does would agree with the view about anything, including being wrong.

The fixture, once, in full:

| task | outcome              | bounced | human_touched | human review | task cost |
|------|----------------------|---------|---------------|--------------|-----------|
| 1    | ACCEPTED             | no      | no            | —            | 100       |
| 2    | ACCEPTED_WITH_EDITS  | no      | yes           | REJECTED     | 200       |
| 3    | ACCEPTED             | **yes** | yes           | ACCEPTED     | 300       |
| 4    | REJECTED             | yes     | no            | —            | 150       |
| 5    | AUTO_ACCEPTED        | no      | no            | —            | 50        |

Model spend: work 800 (the per-task rows above), coordination 200, evaluation 100,
summarization 50 = 1150. Tool spend: 20. Total: 1170.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text

from runtime.domain.enums import WorkClass
from runtime.domain.ids import OrganizationId
from runtime.domain.outputs import COMPETITOR_REPORT_V1, MetricsReport
from runtime.org.department import RESEARCH
from runtime.org.metrics import MetricsService
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.runtime.bootstrap import Registrar
from runtime.settings import Settings

pytestmark = pytest.mark.integration

WEEK = dt.date(2026, 8, 17)  # a Monday
MIDWEEK = dt.datetime(2026, 8, 19, 12, 0, tzinfo=dt.UTC)


@pytest_asyncio.fixture
async def seeded(
    settings: Settings, uow_factory: UnitOfWorkFactory
) -> AsyncIterator[tuple[OrganizationId, dict[str, uuid.UUID]]]:
    """Insert the table above, directly, with no service layer in between."""
    from runtime.domain.enums import ActorKind
    from runtime.domain.specs import ActorSpec

    org = OrganizationId(uuid.uuid4())
    registrar = Registrar(uow_factory)
    await registrar.ensure_organization(org, "acme")
    registered = await registrar.publish_actor(
        org,
        ActorSpec(name=RESEARCH, kind=ActorKind.LLM_AGENT, graph_ref="research@1"),
    )

    tasks = {name: uuid.uuid4() for name in ("t1", "t2", "t3", "t4", "t5")}
    correlation = uuid.uuid4()

    plan = [
        ("t1", "ACCEPTED", False, 100),
        ("t2", "ACCEPTED_WITH_EDITS", True, 200),
        ("t3", "ACCEPTED", True, 300),
        ("t4", "REJECTED", False, 150),
        ("t5", "AUTO_ACCEPTED", False, 50),
    ]

    async with uow_factory.transaction() as uow:
        for name, outcome, touched, cost in plan:
            await uow.session.execute(
                text(
                    """
                    INSERT INTO tasks (id, organization_id, correlation_id, title, objective,
                                       acceptance_criteria, assignee_name, output_schema_ref,
                                       input, status, outcome, human_touched, created_at,
                                       submitted_at, evaluated_at)
                    VALUES (:id, :org, :corr, :title, 'objective', '[]'::jsonb, :who, :schema,
                            '{}'::jsonb, 'CLOSED', :outcome, :touched, :created, :created,
                            :created)
                    """
                ),
                {
                    "id": tasks[name],
                    "org": org,
                    "corr": correlation,
                    "title": name,
                    "who": RESEARCH,
                    "schema": COMPETITOR_REPORT_V1,
                    "outcome": outcome,
                    "touched": touched,
                    "created": MIDWEEK,
                },
            )
            run_id = uuid.uuid4()
            await uow.session.execute(
                text(
                    """
                    INSERT INTO runs (id, organization_id, root_run_id, actor_id,
                                      actor_version_id, task_id, thread_id, status,
                                      idempotency_key, created_at)
                    VALUES (:id, :org, :id, :actor, :version, :task, CAST(:id AS text), 'SUCCESS',
                            :key, :created)
                    """
                ),
                {
                    "id": run_id,
                    "org": org,
                    "actor": registered.actor_id,
                    "version": registered.version_id,
                    "task": tasks[name],
                    "key": f"seed-{name}-{uuid.uuid4()}",
                    "created": MIDWEEK,
                },
            )
            await _usage(uow, org, run_id, "model", WorkClass.WORK.value, cost, MIDWEEK)

        # Overhead spend, on runs with no task — which is exactly the coordination
        # cost the ratio exists to expose.
        overhead_run = uuid.uuid4()
        await uow.session.execute(
            text(
                """
                INSERT INTO runs (id, organization_id, root_run_id, actor_id,
                                  actor_version_id, thread_id, status, idempotency_key,
                                  created_at)
                VALUES (:id, :org, :id, :actor, :version, CAST(:id AS text), 'SUCCESS',
                        :key, :created)
                """
            ),
            {
                "id": overhead_run,
                "org": org,
                "actor": registered.actor_id,
                "version": registered.version_id,
                "key": f"seed-overhead-{uuid.uuid4()}",
                "created": MIDWEEK,
            },
        )
        await _usage(uow, org, overhead_run, "model", WorkClass.COORDINATION.value, 200, MIDWEEK)
        await _usage(uow, org, overhead_run, "model", WorkClass.EVALUATION.value, 100, MIDWEEK)
        await _usage(uow, org, overhead_run, "model", WorkClass.SUMMARIZATION.value, 50, MIDWEEK)
        await _usage(uow, org, overhead_run, "tool", None, 20, MIDWEEK)

        # Manager verdicts. t3 was bounced once, then accepted on attempt 1.
        for name, attempt, outcome, distance in [
            ("t1", 0, "ACCEPTED", 0),
            ("t2", 0, "ACCEPTED_WITH_EDITS", 12),
            ("t3", 0, "REWORK_REQUIRED", None),
            ("t3", 1, "ACCEPTED", 0),
            ("t4", 0, "REJECTED", None),
        ]:
            await _evaluation(uow, org, tasks[name], attempt, "manager", outcome, distance)
        await _evaluation(uow, org, tasks["t5"], 0, "system", "AUTO_ACCEPTED", None)
        # The 20% sample: disagrees on t2, agrees on t3.
        await _evaluation(uow, org, tasks["t2"], 0, "human", "REJECTED", None)
        await _evaluation(uow, org, tasks["t3"], 1, "human", "ACCEPTED", None)

    yield org, tasks


async def _usage(
    uow: Any,
    org: OrganizationId,
    run_id: uuid.UUID,
    kind: str,
    work_class: str | None,
    cents: int,
    when: dt.datetime,
) -> None:
    await uow.session.execute(
        text(
            """
            INSERT INTO usage_ledger (organization_id, run_id, root_run_id, kind,
                                      work_class, call_site, cost_cents, created_at)
            VALUES (:org, :run, :run, :kind, :wc, :site, :cents, :when)
            """
        ),
        {
            "org": org,
            "run": run_id,
            "kind": kind,
            "wc": work_class,
            "site": f"seed.{work_class or kind}",
            "cents": cents,
            "when": when,
        },
    )


async def _evaluation(
    uow: Any,
    org: OrganizationId,
    task_id: uuid.UUID,
    attempt: int,
    kind: str,
    outcome: str,
    distance: int | None,
) -> None:
    await uow.session.execute(
        text(
            """
            INSERT INTO task_evaluations (id, organization_id, task_id, attempt,
                                          evaluator_kind, outcome, rubric, reasoning,
                                          rework_instructions, edit_distance)
            VALUES (:id, :org, :task, :attempt, :kind, :outcome, '[]'::jsonb, '',
                    '[]'::jsonb, :distance)
            """
        ),
        {
            "id": uuid.uuid4(),
            "org": org,
            "task": task_id,
            "attempt": attempt,
            "kind": kind,
            "outcome": outcome,
            "distance": distance,
        },
    )


# --- T26 -----------------------------------------------------------------------------


async def test_t26_cost_per_accepted_outcome(
    seeded: tuple[OrganizationId, dict[str, uuid.UUID]], uow_factory: UnitOfWorkFactory
) -> None:
    """total spend / accepted outcomes = 800 + 370 over 3 = 390.0 cents.

    Per-task WORK spend is 100+200+300+150+50 = 800; overhead is 200+100+50+20 = 370;
    total 1170. Accepted is t1, t2 and t3 — AUTO_ACCEPTED t5 is deliberately absent
    from the denominator, and REJECTED t4 obviously is.
    """
    org, _ = seeded
    metrics = await MetricsService(uow_factory).for_week(org, WEEK)

    assert metrics.total_spend_cents == 1170
    assert metrics.accepted_tasks == 3
    assert metrics.cost_per_accepted_cents == pytest.approx(1170 / 3)


async def test_t26_auto_accepted_never_enters_the_accepted_count(
    seeded: tuple[OrganizationId, dict[str, uuid.UUID]], uow_factory: UnitOfWorkFactory
) -> None:
    """If this flips, cost per accepted outcome falls by a quarter for free."""
    org, _ = seeded
    metrics = await MetricsService(uow_factory).for_week(org, WEEK)

    assert metrics.accepted_tasks == 3, "not 4"
    assert metrics.auto_accepted_tasks == 1
    assert metrics.evaluated_tasks == 5
    assert metrics.auto_accepted_share == pytest.approx(1 / 5)


async def test_t26_rejection_rate_counts_tasks_that_were_ever_bounced(
    seeded: tuple[OrganizationId, dict[str, uuid.UUID]], uow_factory: UnitOfWorkFactory
) -> None:
    """t3 was reworked and then accepted; t4 was rejected. Both were bounced.

    2/5 = 0.4. Scoring only final outcomes would give 1/5 and hide the rework cycle
    entirely — which is the cost this metric exists to expose.
    """
    org, _ = seeded
    metrics = await MetricsService(uow_factory).for_week(org, WEEK)

    assert metrics.submitted_tasks == 5
    assert metrics.bounced_tasks == 2
    assert metrics.rejected_tasks == 1, "and 'rejected' stays available separately"
    assert metrics.rejection_rate == pytest.approx(2 / 5)


async def test_t26_unassisted_completion_rate(
    seeded: tuple[OrganizationId, dict[str, uuid.UUID]], uow_factory: UnitOfWorkFactory
) -> None:
    """ACCEPTED and untouched by a human: t1 only. 1/5 = 0.2.

    t3 is ACCEPTED but `human_touched`, and t2 is ACCEPTED_WITH_EDITS, which is
    assisted by definition.
    """
    org, _ = seeded
    metrics = await MetricsService(uow_factory).for_week(org, WEEK)

    assert metrics.unassisted_rate == pytest.approx(1 / 5)


async def test_t26_coordination_and_overhead_ratios(
    seeded: tuple[OrganizationId, dict[str, uuid.UUID]], uow_factory: UnitOfWorkFactory
) -> None:
    """Model spend is 800 work + 200 coordination + 100 evaluation + 50 summarization
    = 1150. Coordination ratio = 200/1150. Overhead = 1 - 800/1150.

    Tool spend (20) is reported separately and is *not* in either denominator: a
    tool call is made during work and carries no work class, so including it would
    push cost into "overhead" that is nothing of the kind.
    """
    org, _ = seeded
    metrics = await MetricsService(uow_factory).for_week(org, WEEK)

    assert metrics.model_cost_cents == 1150
    assert metrics.tool_cost_cents == 20
    assert metrics.coordination_ratio == pytest.approx(200 / 1150)
    assert metrics.overhead_ratio == pytest.approx(1 - 800 / 1150)


async def test_t26_the_confusion_matrix(
    seeded: tuple[OrganizationId, dict[str, uuid.UUID]], uow_factory: UnitOfWorkFactory
) -> None:
    """Two sampled: the human rejected t2 (manager accepted it) and accepted t3.

    One agreement out of two; one false accept out of two. §8.2's number.
    """
    org, _ = seeded
    metrics = await MetricsService(uow_factory).for_week(org, WEEK)

    assert metrics.human_sampled_tasks == 2
    assert metrics.human_agreement == pytest.approx(0.5)
    assert metrics.false_accepts == 1
    assert metrics.false_accept_rate == pytest.approx(0.5)


async def test_t26_mean_edit_distance_uses_the_latest_manager_verdict(
    seeded: tuple[OrganizationId, dict[str, uuid.UUID]], uow_factory: UnitOfWorkFactory
) -> None:
    """t1=0, t2=12, t3=0 (its *latest* attempt, not the reworked one), t4/t5 null.

    Mean over the non-null values = 4.0. Taking the earliest attempt for t3 would
    drop it from the average entirely and the trend would move for no reason.
    """
    org, _ = seeded
    metrics = await MetricsService(uow_factory).for_week(org, WEEK)

    assert metrics.mean_edit_distance == pytest.approx(4.0)


async def test_t26_per_task_cost_attribution(
    seeded: tuple[OrganizationId, dict[str, uuid.UUID]], uow_factory: UnitOfWorkFactory
) -> None:
    """`runs.task_id` is what makes "what did this task cost" a join.

    The overhead run has no task, so its 370 cents belong to no task — which is the
    whole point of the column being nullable.
    """
    org, tasks = seeded
    async with uow_factory() as uow:
        facts = {f["task_id"]: f for f in await uow.metrics.task_facts(org)}

    assert facts[tasks["t1"]]["task_cost_cents"] == 100
    assert facts[tasks["t3"]]["task_cost_cents"] == 300
    assert sum(f["task_cost_cents"] for f in facts.values()) == 800


async def test_t26_spend_by_work_class_sums_to_the_total(
    seeded: tuple[OrganizationId, dict[str, uuid.UUID]], uow_factory: UnitOfWorkFactory
) -> None:
    org, _ = seeded
    metrics = await MetricsService(uow_factory).for_week(org, WEEK)

    assert metrics.spend_by_work_class == {
        "work": 800,
        "coordination": 200,
        "evaluation": 100,
        "summarization": 50,
        "(none)": 20,
    }
    assert sum(metrics.spend_by_work_class.values()) == metrics.total_spend_cents


# --- the schema is the last line of defence on the SQL --------------------------------


async def test_the_report_schema_rejects_a_view_whose_arithmetic_drifted(
    seeded: tuple[OrganizationId, dict[str, uuid.UUID]], uow_factory: UnitOfWorkFactory
) -> None:
    """`MetricsReport` cross-validates its rates against its counts.

    So a view that started disagreeing with its own numerator and denominator fails
    validation here rather than being rendered on a dashboard — which is most of
    what T26 buys beyond the assertions above.
    """
    service = MetricsService(uow_factory)
    org, _ = seeded
    metrics = await service.for_week(org, WEEK)

    report = service.to_report(metrics)
    assert isinstance(report, MetricsReport)
    assert report.accepted_tasks == 3
    assert report.rejection_rate == pytest.approx(2 / 5)

    metrics.rejection_rate = 0.05  # a view that lost its way
    with pytest.raises(ValueError, match="rejection_rate"):
        service.to_report(metrics)


async def test_an_empty_week_is_zeros_and_nulls_not_an_exception(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """A rate with no denominator is absent, never 0.0.

    Rendering "we do not know" as 0% is wrong in the most dangerous direction
    available — it reads as a perfect week.
    """
    org = OrganizationId(uuid.uuid4())
    await Registrar(uow_factory).ensure_organization(org, "empty")

    metrics = await MetricsService(uow_factory).for_week(org, WEEK)

    assert metrics.total_spend_cents == 0
    assert metrics.rejection_rate is None
    assert metrics.coordination_ratio is None
    assert metrics.cost_per_accepted_cents is None
    assert "No human sample" in " ".join(MetricsService(uow_factory).to_report(metrics).notes)


async def test_the_gate_thresholds_are_read_off_the_same_numbers(
    seeded: tuple[OrganizationId, dict[str, uuid.UUID]], uow_factory: UnitOfWorkFactory
) -> None:
    """§9's stops and passes, evaluated against the fixture above.

    rejection 40% (< 60, no stop; >= 30, not a pass), auto-accepted 20%
    (< 40, no stop; not < 20, not a pass), coordination 17% (a pass).
    """
    org, _ = seeded
    metrics = await MetricsService(uow_factory).for_week(org, WEEK)

    assert metrics.gate_failures() == [], "nothing here trips a stop threshold"
    misses = metrics.pass_failures()
    assert any("rejection rate" in m for m in misses)
    assert any("auto-accepted share" in m for m in misses)
    assert not any("coordination ratio" in m for m in misses), "17% is under 25%"
    assert any("30" in m for m in misses), "and 5 evaluated tasks is under the 30 §9 wants"
