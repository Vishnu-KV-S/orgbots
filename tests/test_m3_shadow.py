"""T54 and shadow mode — §5's phase one, and the PR-35 flag.

§5's claim is unusually strong and worth restating before the assertions:

> *"Zero risk to output quality, zero token cost, and it accumulates real retrievals to
> grade offline."*

Two of those three are checkable mechanically and both are checked here. The prompt is
byte-identical to what M2 would have produced, and the trace is written anyway. The
third — that the retrievals are worth grading — is what the grading harness is for and
no test can substitute for it.

The other thing this file holds is the **build-order guarantee** from §9: *"PR-27
through PR-34 are safe to build at any time, including during an M1 or M2 measurement
window: none of them touches a prompt. PR-35 is the only one that changes what the
models see."* `test_only_the_injection_flag_changes_a_prompt` is that sentence as an
assertion.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import text

from runtime.domain.enums import MemoryScope
from runtime.domain.ids import OrganizationId
from runtime.graphs.common.context import assemble
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.settings import Settings
from tests.conftest_m3 import build_memory_harness, make_memory_ctx, seed_actor, seed_org

pytestmark = pytest.mark.integration

ACTOR = uuid.UUID("00000000-0000-0000-0000-00000000c003")
OTHER = uuid.UUID("00000000-0000-0000-0000-00000000d004")

FACTS = [
    ("Acme pricing", "Acme charges $49 per seat per month with a 20% annual discount"),
    ("Acme positioning", "Acme leads with compliance rather than speed"),
    ("house style", "the blog uses second person and never uses the word 'leverage'"),
]


async def _seeded(uow_factory: UnitOfWorkFactory, settings: Settings, **kw: object):
    org = OrganizationId(uuid.uuid4())
    await seed_org(uow_factory, org)
    await seed_actor(uow_factory, org, ACTOR, "research")
    harness = await build_memory_harness(uow_factory, settings, **kw)  # type: ignore[arg-type]
    await harness.write(
        org, scope=MemoryScope.PRIVATE_ACTOR, scope_id=ACTOR, facts=FACTS, actor_id=ACTOR
    )
    return org, harness


# --- T54: a trace for every retrieval, shadow or live ------------------------------


async def test_t54_a_trace_is_written_in_shadow_mode(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    org, harness = await _seeded(uow_factory, settings)
    ctx = make_memory_ctx(org, actor_id=ACTOR)

    plan = await harness.planner.plan(ctx, node="synthesize", query="what does Acme charge")

    assert plan.shadow_mode is True
    assert plan.injected == [], "shadow mode injected something"
    assert plan.block == "", "shadow mode produced a prompt block"
    assert plan.retrieved, "shadow mode retrieved nothing; the trace would be empty"
    # The counterfactual is still counted. This is eval 8's numerator during phase one
    # — §11: "Measure it in shadow mode (compute the tokens you would have injected)".
    assert plan.would_have_injected_tokens > 0

    async with uow_factory() as uow:
        row = (
            await uow.session.execute(
                text(
                    "SELECT shadow_mode, retrieved_count, injected_count, injected_tokens, "
                    "would_have_injected_tokens, query_text, grade "
                    "FROM context_traces WHERE run_id = :r"
                ),
                {"r": ctx.run_id},
            )
        ).one()
    assert row.shadow_mode is True
    assert row.retrieved_count > 0
    assert row.injected_count == 0
    assert row.injected_tokens == 0
    assert row.would_have_injected_tokens > 0
    assert row.query_text == "what does Acme charge"
    assert row.grade == "ungraded"


async def test_t54_a_trace_is_written_when_live_too(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    org, harness = await _seeded(uow_factory, settings, injection=True)
    ctx = make_memory_ctx(org, actor_id=ACTOR)

    plan = await harness.planner.plan(ctx, node="synthesize", query="what does Acme charge")
    assert plan.injected, "live mode injected nothing; the comparison below is vacuous"

    async with uow_factory() as uow:
        row = (
            await uow.session.execute(
                text(
                    "SELECT shadow_mode, injected_count, injected_tokens, "
                    "would_have_injected_tokens FROM context_traces WHERE run_id = :r"
                ),
                {"r": ctx.run_id},
            )
        ).one()
    assert row.shadow_mode is False
    assert row.injected_count == len(plan.injected)
    assert row.injected_tokens == row.would_have_injected_tokens, (
        "live mode should charge exactly what shadow mode said it would"
    )


async def test_shadow_mode_does_not_touch_access_count(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """A measurement that perturbs the thing it measures is not a measurement.

    Two weeks of shadow retrieval bumping `access_count` would train the rerank's access
    term on memories no model ever saw — so the ordering on the day injection went live
    would be the ordering shadow mode created, and the shadow-mode grades would describe
    a different system.
    """
    org, harness = await _seeded(uow_factory, settings)
    ctx = make_memory_ctx(org, actor_id=ACTOR)

    for _ in range(3):
        await harness.planner.plan(ctx, node="synthesize", query="what does Acme charge")

    async with uow_factory() as uow:
        counts = (
            (
                await uow.session.execute(
                    text("SELECT access_count FROM memory_metadata WHERE organization_id = :o"),
                    {"o": org},
                )
            )
            .scalars()
            .all()
        )
    assert set(counts) == {0}, f"shadow mode moved access_count: {counts}"


async def test_live_mode_counts_only_what_it_injected(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """`access_count` is evidence of usefulness, so a candidate that lost must not earn
    it — otherwise the rerank rewards memories for being nearly chosen, which compounds
    into the "store develops a favourite" failure `RerankWeights` warns about."""
    org, harness = await _seeded(uow_factory, settings, injection=True, memory_inject_k=1)
    ctx = make_memory_ctx(org, actor_id=ACTOR)

    plan = await harness.planner.plan(ctx, node="synthesize", query="what does Acme charge")
    assert len(plan.injected) == 1

    async with uow_factory() as uow:
        rows = (
            await uow.session.execute(
                text(
                    "SELECT memory_id, access_count FROM memory_metadata WHERE organization_id = :o"
                ),
                {"o": org},
            )
        ).all()
    touched = {r.memory_id for r in rows if r.access_count > 0}
    assert touched == {plan.injected[0].memory_id}


# --- the byte-identical guarantee --------------------------------------------------


async def test_shadow_prompt_is_byte_identical_to_the_m2_prompt(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """§5's central claim, as a string comparison.

    The same inputs assembled twice: once with the shadow-mode block and once with no
    memory parameter at all — which is exactly the M2 call. If those differ by a single
    byte then phase one is not free, prompt caching breaks on the transition, and every
    M2 baseline number stops being comparable.
    """
    org, harness = await _seeded(uow_factory, settings)
    ctx = make_memory_ctx(org, actor_id=ACTOR)
    plan = await harness.planner.plan(ctx, node="synthesize", query="what does Acme charge")

    common = {
        "system_prompt": "You are a competitive researcher.",
        "spec": ctx.spec,
        "task_input": {"title": "t", "objective": "o"},
        "session_summary": "last week we looked at pricing",
        "instruction": "Write the report.",
    }
    m2 = assemble(**common)  # type: ignore[arg-type]
    m3_shadow = assemble(**common, memory=plan.block)  # type: ignore[arg-type]

    assert m3_shadow.system == m2.system
    assert m3_shadow.prompt == m2.prompt
    assert m3_shadow.parts == m2.parts


async def test_only_the_injection_flag_changes_a_prompt(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """§9's build-order guarantee: PR-35 is the only one that changes what models see.

    Same organization, same memories, same query — the *only* difference between the
    two planners is `memory_injection_enabled`. One produces an empty block and one does
    not, and nothing else in the subsystem is involved in the difference.
    """
    org, shadow = await _seeded(uow_factory, settings)
    live = await build_memory_harness(uow_factory, settings, injection=True)
    ctx = make_memory_ctx(org, actor_id=ACTOR)

    shadow_plan = await shadow.planner.plan(ctx, node="n", query="what does Acme charge")
    live_plan = await live.planner.plan(ctx, node="n", query="what does Acme charge")

    assert shadow_plan.block == ""
    assert live_plan.block != ""
    assert "Acme" in live_plan.block
    # Same candidates, same order — only the injection decision differs.
    assert [m.memory_id for m in shadow_plan.retrieved] == [
        m.memory_id for m in live_plan.retrieved
    ]


async def test_the_per_actor_flag_is_the_escape_hatch(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """§13 risk 1: *"the per-actor flag in PR-35 is the escape hatch"*.

    Named actors inject; everybody else stays in shadow mode. That is what makes a
    roll-out to one actor for a week possible, and what makes turning memory off for
    one misbehaving actor at 3am an environment variable rather than a redeploy.
    """
    org, harness = await _seeded(uow_factory, settings, injection=True, actors="content,analytics")
    assert harness.planner.injects_for("content") is True
    assert harness.planner.injects_for("research") is False

    ctx = make_memory_ctx(org, actor_id=ACTOR, actor_name="research")
    assert (await harness.planner.plan(ctx, node="n", query="Acme")).block == ""

    ctx2 = make_memory_ctx(org, actor_id=ACTOR, actor_name="content")
    assert (await harness.planner.plan(ctx2, node="n", query="Acme")).block != ""


async def test_memory_off_writes_no_trace_and_costs_nothing(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """`memory_enabled=False` is the M2 configuration exactly: no retrieval, no trace,
    no embedding call. A checkout with M3 merged and the flag off must be indist-
    inguishable from a checkout without it, or the M1 and M2 measurement weeks cannot
    be re-run from this tree."""
    org = OrganizationId(uuid.uuid4())
    await seed_org(uow_factory, org)
    harness = await build_memory_harness(uow_factory, settings, memory_enabled=False)
    ctx = make_memory_ctx(org, actor_id=ACTOR)

    plan = await harness.planner.plan(ctx, node="n", query="anything at all")
    assert plan.retrieved == []
    assert plan.block == ""

    async with uow_factory() as uow:
        traces = (
            await uow.session.execute(
                text("SELECT count(*) FROM context_traces WHERE organization_id = :o"), {"o": org}
            )
        ).scalar_one()
        embeddings = (
            await uow.session.execute(
                text(
                    "SELECT count(*) FROM audit_logs WHERE organization_id = :o "
                    "AND gateway = 'embedding'"
                ),
                {"o": org},
            )
        ).scalar_one()
    assert traces == 0
    assert embeddings == 0


async def test_retrieval_failure_degrades_the_prompt_rather_than_failing_the_run(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """Retrieval is an enrichment. A department that stops working because its memory is
    down has traded a real capability for a hypothetical one."""

    org, harness = await _seeded(uow_factory, settings, injection=True)

    async def explode(**_: object) -> None:
        raise RuntimeError("the store is on fire")

    harness.planner._store.search = explode  # type: ignore[assignment,method-assign]
    ctx = make_memory_ctx(org, actor_id=ACTOR)

    plan = await harness.planner.plan(ctx, node="n", query="what does Acme charge")
    assert plan.retrieved == []
    assert plan.block == ""


async def test_a_run_with_no_query_does_not_retrieve(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """An empty query would match by recency alone, which is the shape of a retrieval
    that always returns the most recent three memories regardless of relevance — the
    quietest way for eval 2 to be bad."""
    org, harness = await _seeded(uow_factory, settings, injection=True)
    ctx = make_memory_ctx(org, actor_id=ACTOR)
    assert (await harness.planner.plan(ctx, node="n", query="   ")).retrieved == []
