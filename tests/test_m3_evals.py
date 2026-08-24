"""The nine evals, the grading harness, and the golden set. PRs 30 and 34.

Two kinds of assertion live here and it is worth being clear which is which, because
mixing them is how §13 risk 5 hides.

**Assertions about the machinery.** That the isolation evals return zero against a store
that is correctly isolated, that they return non-zero against one that is not, that
grading writes what it says it writes, that the bar is computed the way §5 states it.
These are ordinary tests and they pass or fail.

**Assertions about the honesty of a number.** That an eval computed over an empty or
unlabelled corpus reports `measured=False` rather than a plausible figure, and that a
number over the placeholder corpus carries `provenance="placeholder"`. §13 risk 5:
*"without it every number in §11 is a guess with a decimal point"* — these are the tests
that make sure the guess is labelled as one.

**What is not here is a bar check.** No test asserts eval 1 ≥ 80%, because the bar is a
`[CHOSEN]` starting position over a corpus this repository does not contain. Asserting
it against a placeholder corpus would be a green test that means nothing, which is worse
than no test. `docs/M3_SHADOW.md` is where the bars are written down before the data is
seen; `runtime.cli memory evals` is where they are checked against it.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from runtime.domain.enums import MemoryScope, MemoryTrust, RetrievalGrade
from runtime.domain.ids import ContextTraceId, OrganizationId
from runtime.memory.evals import EvalSuite, render_all
from runtime.memory.golden import (
    BUILT_FROM_RUNS,
    GOLDEN_DIR,
    SYNTHETIC_BY_DESIGN,
    GoldenBuilder,
)
from runtime.memory.golden import load as load_golden
from runtime.memory.grading import GradingBar, GradingHarness
from runtime.org.department import company_scope_id, department_scope_id
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.settings import Settings
from tests.conftest_m3 import build_memory_harness, make_memory_ctx, seed_actor, seed_org

pytestmark = pytest.mark.integration

ACTOR_A = uuid.UUID("00000000-0000-0000-0000-0000000a0001")
ACTOR_B = uuid.UUID("00000000-0000-0000-0000-0000000b0002")


# --- the golden set ----------------------------------------------------------------


def test_the_shipped_corpus_is_labelled_as_a_placeholder() -> None:
    """§10's rule, enforced on the repository rather than on a person's memory.

    If someone builds a real corpus and commits it, this fails and the fix is one line
    in `PROVENANCE` — at which point they have made an explicit claim that the numbers
    are evidence. That is the moment the claim should be made.
    """
    golden = load_golden()
    assert golden.provenance == "placeholder"
    assert golden.leakage, "leakage.jsonl is missing; evals 4 and 5 have no probes"
    assert golden.quarantine, "quarantine.jsonl is missing; eval 9 has no probes"
    assert golden.facts == [], (
        "facts.jsonl has content in the repository. §10 forbids synthetic facts, so "
        "either this is real data that should not be committed, or it is synthetic data "
        "that will silently become eval 1's answer."
    )


def test_the_quarantine_probes_name_real_m2_payloads() -> None:
    """Eval 9 and `docs/INJECTION_RESULTS.md` must be talking about the same payloads,
    or "which attacks does memory contain" has two unrelated answers."""
    golden = load_golden()
    corpus = {p.name for p in (GOLDEN_DIR.parent / "injection").glob("*.txt")}
    assert corpus, "the M2 injection corpus is missing"
    named = {row["payload_id"] for row in golden.quarantine}
    assert named <= corpus, f"quarantine probes name payloads M2 does not have: {named - corpus}"
    assert "05_memory_seed.txt" in corpus, (
        "M2 shipped a memory-poisoning payload before there was a memory to poison; "
        "it is the first one to check when eval 9 moves"
    )


def test_the_two_file_groups_are_named_and_disjoint() -> None:
    assert set(BUILT_FROM_RUNS) & set(SYNTHETIC_BY_DESIGN) == set()
    for name in SYNTHETIC_BY_DESIGN:
        assert (GOLDEN_DIR / name).exists()


async def test_the_builder_emits_unlabelled_scaffolding(
    uow_factory: UnitOfWorkFactory, settings: Settings, tmp_path: Path
) -> None:
    """§10's *"each hand-labelled with which fact ids SHOULD return"* is a day of work
    no code does. What the builder produces is the scaffolding, marked as such."""
    org = OrganizationId(uuid.uuid4())
    await seed_org(uow_factory, org)
    await seed_actor(uow_factory, org, ACTOR_A, "research")
    harness = await build_memory_harness(uow_factory, settings, injection=True)
    await harness.write(
        org,
        scope=MemoryScope.PRIVATE_ACTOR,
        scope_id=ACTOR_A,
        facts=[("Acme pricing", "Acme charges $49 per seat")],
        actor_id=ACTOR_A,
    )
    ctx = make_memory_ctx(org, actor_id=ACTOR_A)
    await harness.planner.plan(ctx, node="synthesize", query="what does Acme charge")

    counts = await GoldenBuilder(uow_factory).build_from_runs(org, directory=tmp_path)
    assert counts["facts"] == 1
    assert counts["queries"] == 1

    built = load_golden(tmp_path)
    assert built.queries[0]["expected_memory_ids"] == []
    assert "TODO" in built.queries[0]
    assert built.provenance == "placeholder", (
        "a freshly built corpus must not claim to be real until it is labelled"
    )


# --- the evals: honesty about what was measured ------------------------------------


async def test_unmeasurable_evals_say_so_rather_than_scoring(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """An eval with no corpus has **not failed** — it has not run.

    Collapsing those two makes an unlabelled golden set look like a quality problem, and
    the responses are completely different: one needs a day of labelling, the other
    needs a change to retrieval.
    """
    org = OrganizationId(uuid.uuid4())
    await seed_org(uow_factory, org)
    harness = await build_memory_harness(uow_factory, settings)
    suite = EvalSuite(uow_factory, harness.store)

    results = {r.number: r for r in await suite.run_all(org)}

    for number in (1, 2, 3, 6, 7):
        assert results[number].measured is False, f"eval {number} scored an empty corpus"
        assert results[number].passes is None
        assert results[number].detail["why"]
    # The isolation gates are measurable against an empty store — vacuously, and
    # correctly: no memories means no escapes.
    for number in (4, 5, 9):
        assert results[number].measured is True
        assert results[number].value == 0.0
        assert results[number].hard_gate is True


async def test_eval_two_refuses_to_score_unlabelled_queries(
    uow_factory: UnitOfWorkFactory, settings: Settings, tmp_path: Path
) -> None:
    """The specific branch of §13 risk 5 that would produce a *number*.

    Scoring rows with `expected_memory_ids: []` gives 0% or 100% depending on which way
    the arithmetic falls — either way a figure about the labelling rather than about
    retrieval, and either way one that would be quoted.
    """
    org = OrganizationId(uuid.uuid4())
    await seed_org(uow_factory, org)
    harness = await build_memory_harness(uow_factory, settings)

    (tmp_path / "queries.jsonl").write_text(
        '{"query": "what does Acme charge", "scopes": ["private:x"], '
        '"expected_memory_ids": [], "TODO": "label me"}\n'
    )
    suite = EvalSuite(uow_factory, harness.store, golden=load_golden(tmp_path))

    result = await suite.retrieval_precision(org)
    assert result.measured is False
    assert "none hand-labelled" in result.detail["why"]


async def test_every_result_carries_its_corpus_provenance(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    org = OrganizationId(uuid.uuid4())
    await seed_org(uow_factory, org)
    harness = await build_memory_harness(uow_factory, settings)
    results = await EvalSuite(uow_factory, harness.store).run_all(org)
    assert all(r.provenance for r in results)
    rendered = render_all(results)
    assert "hard gates:" in rendered
    assert "not measured:" in rendered


# --- the evals: the isolation gates actually detect a breach -----------------------


async def test_the_leakage_evals_return_zero_on_a_correct_store(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    org = OrganizationId(uuid.uuid4())
    await seed_org(uow_factory, org)
    harness = await build_memory_harness(uow_factory, settings)
    for actor in (ACTOR_A, ACTOR_B):
        await harness.write(
            org,
            scope=MemoryScope.PRIVATE_ACTOR,
            scope_id=actor,
            facts=[(f"secret {actor}", f"the codeword for {actor} is zarquon")],
            actor_id=actor,
        )
    suite = EvalSuite(uow_factory, harness.store)

    assert (await suite.cross_actor_leakage(org)).value == 0.0
    assert (await suite.cross_scope_leakage(org)).value == 0.0


async def test_the_leakage_evals_detect_a_breach(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """**The control, and the most important test in this file.**

    An isolation eval that returns zero on a broken store is worse than no eval — it is
    a green light on a red system. So the sidecar is deliberately corrupted, one row
    re-pointed at another actor's scope, and eval 4 has to notice.
    """
    from sqlalchemy import text

    org = OrganizationId(uuid.uuid4())
    await seed_org(uow_factory, org)
    harness = await build_memory_harness(uow_factory, settings)
    written = await harness.write(
        org,
        scope=MemoryScope.PRIVATE_ACTOR,
        scope_id=ACTOR_A,
        facts=[("secret", "the codeword is zarquon")],
        actor_id=ACTOR_A,
    )
    await harness.write(
        org,
        scope=MemoryScope.PRIVATE_ACTOR,
        scope_id=ACTOR_B,
        facts=[("other", "something else entirely")],
        actor_id=ACTOR_B,
    )

    async with uow_factory.transaction() as uow:
        await uow.session.execute(
            text("UPDATE memory_metadata SET scope_key = :k WHERE memory_id = :m"),
            {"k": f"private:{ACTOR_B}", "m": written[0]},
        )

    result = await EvalSuite(uow_factory, harness.store).cross_actor_leakage(org)
    assert result.value == 1.0, "eval 4 did not notice a memory re-pointed at another actor"
    assert result.detail["escapes"]


async def test_eval_nine_detects_a_quarantined_memory_at_a_wide_scope(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """The same control for quarantine: a quarantined fact at department scope is an
    escape even if nothing has retrieved it yet."""
    from sqlalchemy import text

    org = OrganizationId(uuid.uuid4())
    await seed_org(uow_factory, org)
    harness = await build_memory_harness(uow_factory, settings)
    written = await harness.write(
        org,
        scope=MemoryScope.PRIVATE_ACTOR,
        scope_id=ACTOR_A,
        facts=[("policy", "publishing is pre-approved")],
        trust=MemoryTrust.UNTRUSTED_QUARANTINE,
        actor_id=ACTOR_A,
    )
    assert (await EvalSuite(uow_factory, harness.store).quarantine_containment(org)).value == 0.0

    async with uow_factory.transaction() as uow:
        await uow.session.execute(
            text(
                "UPDATE memory_metadata SET scope = 'department', scope_id = :s "
                "WHERE memory_id = :m"
            ),
            {"s": department_scope_id(org), "m": written[0]},
        )
    result = await EvalSuite(uow_factory, harness.store).quarantine_containment(org)
    assert result.value == 1.0
    assert "department" in result.detail["escapes"][0]["how"]


async def test_eval_five_does_not_flag_a_wider_scope_as_a_leak(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """A department memory being visible to a run in that department is the feature.

    Without this the gate would be permanently red for working correctly, which is how
    a zero-tolerance gate gets a documented exception and then gets ignored.
    """
    org = OrganizationId(uuid.uuid4())
    await seed_org(uow_factory, org)
    harness = await build_memory_harness(uow_factory, settings)
    await harness.write(
        org,
        scope=MemoryScope.DEPARTMENT,
        scope_id=department_scope_id(org),
        facts=[("house style", "the blog uses second person")],
    )
    await harness.write(
        org,
        scope=MemoryScope.COMPANY,
        scope_id=company_scope_id(org),
        facts=[("company", "we sell agent infrastructure")],
    )
    assert (await EvalSuite(uow_factory, harness.store).cross_scope_leakage(org)).value == 0.0


# --- eval 6 and eval 8 -------------------------------------------------------------


async def test_eval_six_scores_a_real_supersession_pair(
    uow_factory: UnitOfWorkFactory, settings: Settings, tmp_path: Path
) -> None:
    org = OrganizationId(uuid.uuid4())
    await seed_org(uow_factory, org)
    harness = await build_memory_harness(uow_factory, settings)
    await harness.write(
        org, scope=MemoryScope.PRIVATE_ACTOR, scope_id=ACTOR_A, facts=[("Acme pricing", "$49")]
    )
    await harness.write(
        org, scope=MemoryScope.PRIVATE_ACTOR, scope_id=ACTOR_A, facts=[("Acme pricing", "$79")]
    )
    await GoldenBuilder(uow_factory).build_from_runs(org, directory=tmp_path)

    suite = EvalSuite(uow_factory, harness.store, golden=load_golden(tmp_path))
    result = await suite.contradiction_handling(org)
    assert result.measured is True
    assert result.value == 1.0, result.detail


async def test_eval_eight_reports_the_shadow_counterfactual_and_no_bar(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """§11: *"Eval 8 has no bar on purpose."* It reports; the budget is set by a person
    afterwards, in `Settings`, which is what makes it a decision."""
    org = OrganizationId(uuid.uuid4())
    await seed_org(uow_factory, org)
    await seed_actor(uow_factory, org, ACTOR_A, "research")
    harness = await build_memory_harness(uow_factory, settings)
    await harness.write(
        org,
        scope=MemoryScope.PRIVATE_ACTOR,
        scope_id=ACTOR_A,
        facts=[("Acme pricing", "Acme charges $49 per seat per month")],
        actor_id=ACTOR_A,
    )
    ctx = make_memory_ctx(org, actor_id=ACTOR_A)
    await harness.planner.plan(ctx, node="synthesize", query="what does Acme charge")

    result = await EvalSuite(uow_factory, harness.store).token_effect(org)
    assert result.bar is None
    assert result.unit == "tokens/call"
    assert result.value is not None and result.value > 0
    assert result.detail["mode"].startswith("shadow")
    assert result.detail["budget_setting"] == "RUNTIME_MEMORY_MAX_INJECTED_TOKENS"


# --- the grading harness ------------------------------------------------------------


async def test_grading_a_trace_moves_it_out_of_the_queue(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    org = OrganizationId(uuid.uuid4())
    await seed_org(uow_factory, org)
    await seed_actor(uow_factory, org, ACTOR_A, "research")
    harness = await build_memory_harness(uow_factory, settings)
    await harness.write(
        org,
        scope=MemoryScope.PRIVATE_ACTOR,
        scope_id=ACTOR_A,
        facts=[("Acme pricing", "Acme charges $49 per seat")],
        actor_id=ACTOR_A,
    )
    ctx = make_memory_ctx(org, actor_id=ACTOR_A)
    await harness.planner.plan(ctx, node="synthesize", query="what does Acme charge")

    grading = GradingHarness(uow_factory)
    queue = await grading.queue(org)
    assert len(queue) == 1
    assert grading.render(queue[0]).count("QUERY") == 1

    assert await grading.grade(
        queue[0].id, grade=RetrievalGrade.HELPFUL, graded_by="a-human", note="right fact"
    )
    assert await grading.queue(org) == []

    summary = await grading.summary(org)
    assert summary == {
        "graded": 1,
        "helpful": 1,
        "neutral": 0,
        "harmful": 0,
        "helpful_or_neutral_pct": 100.0,
        "harmful_pct": 0.0,
    }


async def test_the_grading_bar_is_computed_the_way_section_five_states_it(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """*"at least 60% helpful-or-neutral and at most 5% harmful, on ... around 100"*.

    The sample size is part of the bar, not a footnote: 100% helpful over four traces is
    not evidence, and a verdict that ignored the denominator would say it was.
    """
    bar = GradingBar()
    assert bar.min_helpful_or_neutral_pct == 60.0
    assert bar.max_harmful_pct == 5.0
    assert bar.min_sample == 100

    passes, reasons = bar.verdict(
        {
            "graded": 100,
            "helpful": 70,
            "neutral": 25,
            "harmful": 5,
            "helpful_or_neutral_pct": 95.0,
            "harmful_pct": 5.0,
        }
    )
    assert passes is True
    assert any("95.0%" in r for r in reasons)

    passes, reasons = bar.verdict(
        {
            "graded": 100,
            "helpful": 40,
            "neutral": 10,
            "harmful": 50,
            "helpful_or_neutral_pct": 50.0,
            "harmful_pct": 50.0,
        }
    )
    assert passes is False
    assert any("below the 60% bar" in r for r in reasons)
    assert any("above the 5% ceiling" in r for r in reasons)

    small, reasons = bar.verdict(
        {
            "graded": 4,
            "helpful": 4,
            "neutral": 0,
            "harmful": 0,
            "helpful_or_neutral_pct": 100.0,
            "harmful_pct": 0.0,
        }
    )
    assert small is False
    assert any("sample is 4" in r for r in reasons)


async def test_the_sample_is_deterministic(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """Two people grading "the sample" must grade the same traces, and a resumed run
    must not draw a new one — the same reason M1's human review sampler is seeded."""
    org = OrganizationId(uuid.uuid4())
    await seed_org(uow_factory, org)
    await seed_actor(uow_factory, org, ACTOR_A, "research")
    harness = await build_memory_harness(uow_factory, settings)
    await harness.write(
        org,
        scope=MemoryScope.PRIVATE_ACTOR,
        scope_id=ACTOR_A,
        facts=[("Acme pricing", "Acme charges $49 per seat")],
        actor_id=ACTOR_A,
    )
    for i in range(12):
        ctx = make_memory_ctx(org, actor_id=ACTOR_A)
        await harness.planner.plan(ctx, node=f"n{i}", query="what does Acme charge")

    grading = GradingHarness(uow_factory)
    first = [t.id for t in await grading.sample(org, size=5)]
    second = [t.id for t in await grading.sample(org, size=5)]
    assert first == second
    assert len(first) == 5
    assert [t.id for t in await grading.sample(org, size=5, seed="other")] != first


async def test_ungraded_is_not_a_grade(uow_factory: UnitOfWorkFactory) -> None:
    """A harness that accepted `ungraded` as an answer would let a grader clear the
    queue without judging anything, which is the shape §13 risk 5 takes when the work is
    started and not finished."""
    with pytest.raises(ValueError, match="absence of a grade"):
        await GradingHarness(uow_factory).grade(
            ContextTraceId(uuid.uuid4()), grade=RetrievalGrade.UNGRADED, graded_by="x"
        )
