"""T24, T25 — what the four actors are allowed to do.

T24 is the control experiment. `analytics` produces the numbers the go/no-go
decision is read off, so it must have no language model anywhere in its provenance.
That is enforced twice and both are tested: `max_llm_calls = 0` at the gateway, and
no model profiles at all, so even a raised ceiling would resolve to nothing.

T25 is the one that keeps §7's coordination ratio meaningful. `overhead_ratio` is
`1 - work_share`, so a call outside the M1 vocabulary lands in overhead and makes
the ratio wrong in a direction nobody would question. Three layers stop that: the
per-actor profile allow-list, the check in `call_structured`, and this test running
the actual graphs and auditing what every call declared.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text

from runtime.domain.enums import M1_WORK_CLASSES, ActorKind, WorkClass
from runtime.domain.errors import ModelCallNotAllowed, SpecError
from runtime.domain.ids import CorrelationId
from runtime.domain.outputs import (
    COMPETITOR_REPORT_V1,
    CONTENT_DRAFT_V1,
    METRICS_REPORT_V1,
)
from runtime.domain.specs import StartRunRequest
from runtime.org.department import (
    ANALYTICS,
    ASSIGNEES,
    CONTENT,
    DEPARTMENT_SPECS,
    HEAD,
    RESEARCH,
    SCHEMA_FOR_ASSIGNEE,
)
from runtime.settings import Settings
from tests.conftest_m1 import build_m1, new_org

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def m1(settings: Settings) -> AsyncIterator[Any]:
    async for runtime in build_m1(settings, new_org(), with_triggers=False):
        yield runtime


# --- the specs, before anything runs ------------------------------------------------


def test_the_department_is_four_actors_and_only_one_is_deterministic() -> None:
    by_name = {s.name: s for s in DEPARTMENT_SPECS}
    assert set(by_name) == {HEAD, RESEARCH, CONTENT, ANALYTICS}
    assert by_name[ANALYTICS].kind is ActorKind.DETERMINISTIC_WORKER
    assert all(by_name[n].kind is ActorKind.LLM_AGENT for n in (HEAD, RESEARCH, CONTENT))
    assert set(ASSIGNEES) == {RESEARCH, CONTENT, ANALYTICS}


def test_t24_analytics_is_refused_a_model_twice_over() -> None:
    """Belt and braces, and both are load-bearing.

    `max_llm_calls = 0` is what the gateway checks. No profiles at all is what makes
    a raised ceiling still resolve to nothing — so an edit that loosened the ceiling
    without also adding a profile does not quietly give the control group a model.
    """
    spec = {s.name: s for s in DEPARTMENT_SPECS}[ANALYTICS]
    assert spec.ceilings.max_llm_calls == 0
    assert spec.ceilings.max_cost_cents == 0
    assert spec.model_profiles.profiles == {}
    assert spec.allowed_tools == frozenset(), "and no tools to reach the world with"


def test_every_llm_actor_has_a_profile_for_exactly_the_classes_it_uses() -> None:
    """The per-actor allow-list. An actor with no profile for a class cannot make a
    call in it — `for_work_class` raises at the gateway."""
    by_name = {s.name: s for s in DEPARTMENT_SPECS}
    assert set(by_name[HEAD].model_profiles.profiles) == {
        WorkClass.COORDINATION,
        WorkClass.EVALUATION,
        WorkClass.SUMMARIZATION,
    }
    assert set(by_name[RESEARCH].model_profiles.profiles) == {
        WorkClass.WORK,
        WorkClass.SUMMARIZATION,
    }
    assert set(by_name[CONTENT].model_profiles.profiles) == {
        WorkClass.WORK,
        WorkClass.SUMMARIZATION,
    }
    for spec in by_name.values():
        assert set(spec.model_profiles.profiles) <= M1_WORK_CLASSES


def test_only_the_head_holds_the_irreversible_tool() -> None:
    """An assignee that could publish would make the gate advisory."""
    by_name = {s.name: s for s in DEPARTMENT_SPECS}
    assert by_name[HEAD].allowed_tools == frozenset({"publish.external@1"})
    for name in ASSIGNEES:
        assert "publish.external@1" not in by_name[name].allowed_tools


def test_each_assignee_is_pinned_to_the_schema_it_produces() -> None:
    assert SCHEMA_FOR_ASSIGNEE == {
        RESEARCH: COMPETITOR_REPORT_V1,
        CONTENT: CONTENT_DRAFT_V1,
        ANALYTICS: METRICS_REPORT_V1,
    }


# --- T24, at runtime ------------------------------------------------------------------


async def test_t24_analytics_attempting_a_model_call_is_rejected_by_the_gateway(
    m1: Any, settings: Settings
) -> None:
    """The refusal is a property of the system, not of the handler's source.

    The handler is asked to do the one thing it must not, using the real gateway
    under the real frozen spec.
    """
    from runtime.domain.context import RunContext
    from runtime.domain.specs import RunSpec
    from runtime.gateway.models import ModelRequest

    result = await m1.service.start_run(
        StartRunRequest(
            organization_id=m1.organization_id,
            actor_name=ANALYTICS,
            input={"mode": "weekly_metrics"},
            idempotency_key=f"t24-{uuid.uuid4()}",
        )
    )
    async with m1.uow() as uow:
        row = await uow.runs.get_spec(result.run_id)
    assert row is not None
    spec = RunSpec.model_validate(row.spec)

    # A real claim, so the lease carries a fence the gateway will accept. The
    # gateway checks the fence *before* anything else, so a synthetic lease would
    # fail on StaleFence and this test would pass for the wrong reason.
    lease = await m1.worker.leases.claim(result.run_id, m1.worker.worker_id)
    assert lease is not None

    ctx = RunContext(
        run_id=spec.run_id,
        organization_id=spec.organization_id,
        root_run_id=spec.root_run_id,
        actor_id=spec.spec.actor_id,
        actor_version=spec.spec.actor_version,
        spec=spec,
        lease=lease,
        worker_id=m1.worker.worker_id,
        trace_id="0" * 32,
    )
    _ = dt

    with pytest.raises(ModelCallNotAllowed, match="max_llm_calls=0"):
        await m1.worker.executor.models.complete(
            ctx,
            ModelRequest(prompt="just this once"),
            work_class=WorkClass.WORK,
            call_site="analytics.cheating",
        )

    # And the second refusal, independently: even with the ceiling lifted there is
    # no profile for the call to resolve.
    object.__setattr__(spec.spec.ceilings, "max_llm_calls", 4)
    with pytest.raises(SpecError, match="no model profile"):
        await m1.worker.executor.models.complete(
            ctx,
            ModelRequest(prompt="just this once"),
            work_class=WorkClass.WORK,
            call_site="analytics.cheating",
        )


async def test_analytics_produces_a_metrics_report_end_to_end(m1: Any, uow_factory: Any) -> None:
    """PR-19's whole point: the deterministic path produces a validated artifact
    before any prompt exists to blame."""
    result = await m1.service.start_run(
        StartRunRequest(
            organization_id=m1.organization_id,
            actor_name=ANALYTICS,
            input={"mode": "weekly_metrics"},
            idempotency_key=f"analytics-{uuid.uuid4()}",
        )
    )
    await m1.pump()

    async with uow_factory() as uow:
        run = (
            await uow.session.execute(
                text("SELECT status FROM runs WHERE id = :id"), {"id": result.run_id}
            )
        ).scalar_one()
        usage = (
            await uow.session.execute(
                text("SELECT count(*) FROM usage_ledger WHERE run_id = :id AND kind = 'model'"),
                {"id": result.run_id},
            )
        ).scalar_one()
    assert run == "SUCCESS"
    assert usage == 0, "the control group spends nothing on models, ever"
    assert m1.provider.calls == [], "and reaches no provider at all"


# --- T25 ------------------------------------------------------------------------------


async def test_t25_every_model_call_in_all_four_actors_carries_an_m1_work_class(
    m1: Any, uow_factory: Any
) -> None:
    """Run the real graphs; audit what every call declared.

    Both halves are checked, because they can disagree: what the *provider* was
    told (the recording) and what the *ledger* recorded (I12's aggregation unit).
    A gateway that classified for billing differently from how it classified for
    routing would pass one and fail the other.
    """
    org = m1.organization_id
    correlation = CorrelationId(uuid.uuid4())

    # The head plans; the assignees work; the head evaluates and summarises. That is
    # every model call site M1 has.
    plan = await m1.service.start_run(
        StartRunRequest(
            organization_id=org,
            actor_name=HEAD,
            input={"mode": "weekly_plan"},
            idempotency_key=f"t25-plan-{uuid.uuid4()}",
            correlation_id=str(correlation),
        )
    )
    await m1.pump(rounds=10)

    summary = await m1.service.start_run(
        StartRunRequest(
            organization_id=org,
            actor_name=HEAD,
            input={"mode": "weekly_summary"},
            idempotency_key=f"t25-summary-{uuid.uuid4()}",
            correlation_id=str(correlation),
        )
    )
    await m1.pump(rounds=10)

    assert plan.created and summary.created

    recorded = m1.provider.calls
    assert recorded, "the actors must actually have called a model"
    for call in recorded:
        assert call.work_class, f"{call.call_site} reached the provider with no work class"
        assert WorkClass(call.work_class) in M1_WORK_CLASSES, (
            f"{call.call_site} declared {call.work_class!r}, outside the M1 vocabulary; "
            "overhead_ratio is 1 - work_share, so this would silently distort it"
        )

    async with uow_factory() as uow:
        rows = (
            await uow.session.execute(
                text(
                    """
                    SELECT work_class, call_site, count(*) AS n
                      FROM usage_ledger
                     WHERE organization_id = :org AND kind = 'model'
                     GROUP BY 1, 2
                    """
                ),
                {"org": org},
            )
        ).all()
    assert rows, "and the ledger must have recorded them"
    for row in rows:
        assert row.work_class is not None, f"{row.call_site} has no work_class in the ledger"
        assert WorkClass(row.work_class) in M1_WORK_CLASSES
        assert row.call_site, "I12: a model call names where it came from"

    sites = {r.call_site for r in rows}
    assert "head.weekly_plan" in sites
    assert any(s.startswith("head.") for s in sites)


async def test_t25_the_plan_call_is_coordination_and_the_work_calls_are_work(
    m1: Any, uow_factory: Any
) -> None:
    """The classes are not interchangeable labels — they are the numerator and the
    denominator of the metric the milestone exists to produce."""
    org = m1.organization_id
    await m1.service.start_run(
        StartRunRequest(
            organization_id=org,
            actor_name=HEAD,
            input={"mode": "weekly_plan"},
            idempotency_key=f"t25b-{uuid.uuid4()}",
        )
    )
    await m1.pump(rounds=12)

    async with uow_factory() as uow:
        rows = (
            await uow.session.execute(
                text(
                    "SELECT DISTINCT call_site, work_class FROM usage_ledger "
                    "WHERE organization_id = :org AND kind = 'model'"
                ),
                {"org": org},
            )
        ).all()
    by_site = {r.call_site: r.work_class for r in rows}

    assert by_site.get("head.weekly_plan") == WorkClass.COORDINATION.value
    if "research.synthesize" in by_site:
        assert by_site["research.synthesize"] == WorkClass.WORK.value
    if "content.draft" in by_site:
        assert by_site["content.draft"] == WorkClass.WORK.value
    if "head.evaluate" in by_site:
        assert by_site["head.evaluate"] == WorkClass.EVALUATION.value


async def test_a_call_outside_the_m1_vocabulary_is_refused_at_the_call_site(m1: Any) -> None:
    """`call_structured` refuses rather than letting it through to be mis-counted."""
    from runtime.graphs.common.context import AssembledContext
    from runtime.graphs.common.structured import call_structured

    ctx = object()  # never reached; the check is first
    with pytest.raises(ValueError, match="outside the M1 vocabulary"):
        await call_structured(
            ctx,  # type: ignore[arg-type]
            m1.worker.executor.models,
            AssembledContext(system="s", prompt="p"),
            COMPETITOR_REPORT_V1,
            work_class=WorkClass.GENERATION,
            call_site="somebody.improvising",
        )


def test_the_work_class_vocabulary_is_exactly_four() -> None:
    assert {
        WorkClass.WORK,
        WorkClass.COORDINATION,
        WorkClass.EVALUATION,
        WorkClass.SUMMARIZATION,
    } == M1_WORK_CLASSES
    assert WorkClass.WORK.value == "work", "the views filter on these strings"
    assert WorkClass.COORDINATION.value == "coordination"
