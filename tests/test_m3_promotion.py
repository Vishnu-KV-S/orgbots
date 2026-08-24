"""T53 — promotion requires a review row, and **no auto-promotion path exists**.

§8: *"Automatic promotion means company memory accumulates whatever the extraction
model happened to emit, with no one accountable for any of it."*

That is an assertion about an **absence**, and an absence is the one thing a behavioural
test cannot demonstrate — you can show that this path requires a reviewer, not that no
other path exists. So this file does both kinds of check, and says which is which:

*Behavioural.* A proposal without a decision leaves the memory where it was; a decision
without a reviewer cannot be written, because the database refuses it; the ladder is one
rung at a time.

*Structural.* `MemoryMetadataRepository.move_scope` — the only function that widens a
scope — has exactly one caller in the source tree, and it is `PromotionService`. That is
a grep, and a grep is the honest tool for "there is no second path". It is the same
technique `test_m2_governance.py` uses to catch a raw `UPDATE budget_pools`, and for the
same reason: a second path needs no import and would be invisible to the import graph.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from runtime.domain.enums import MemoryScope, MemoryTrust, PromotionStatus
from runtime.domain.errors import PromotionNotReviewed
from runtime.domain.ids import MemoryId, OrganizationId, new_promotion_id
from runtime.org.department import company_scope_id, department_scope_id
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.settings import Settings
from tests.conftest_m3 import build_memory_harness, seed_org

pytestmark = pytest.mark.integration

SRC = Path(__file__).resolve().parents[1] / "src" / "runtime"
ACTOR = uuid.UUID("00000000-0000-0000-0000-00000000f006")


# --- structural: there is one door ------------------------------------------------


def test_t53_move_scope_has_exactly_one_caller() -> None:
    """The absence, checked the only way an absence can be.

    `move_scope` is the single function that widens a memory's scope. If a second
    caller appears — a CLI shortcut, a "just for the backfill" helper — this fails and
    names the file, which is the moment to decide whether that caller has a reviewer.
    """
    callers: list[str] = []
    for path in SRC.rglob("*.py"):
        if path.name == "memory.py" and path.parent.name == "repositories":
            continue  # the definition itself
        for i, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r"\.move_scope\s*\(", line):
                callers.append(f"{path.relative_to(SRC)}:{i}")

    assert callers, "move_scope has no callers at all — promotion cannot be working"
    files = {c.split(":")[0] for c in callers}
    assert files == {"memory/promotion.py"}, (
        f"a second path widens memory scope: {sorted(callers)}. §8 requires every "
        "promotion to carry a reviewer and a rationale; a caller outside "
        "PromotionService has neither."
    )


def test_t53_every_caller_of_decide_names_a_reviewer() -> None:
    """`PromotionService.decide` is the only thing that records a decision, and every
    call site has to name who made it.

    The set of callers is deliberately **not** frozen — the CLI's `--grant/--deny` is a
    legitimate second one, and pinning the list would mean the test failed whenever
    somebody added an honest path. What is checked is the property §8 actually asks for:
    a decision carries a reviewer. `reviewer_kind` cannot be defaulted (the signature is
    keyword-only with no default) and `ck_promotion_decided_has_reviewer` refuses the row
    without one, so this is the third of three guards rather than the only one.
    """
    seen = 0
    for path in SRC.rglob("*.py"):
        source = path.read_text()
        for match in re.finditer(r"\.decide\s*\(", source):
            args = _call_args(source, match.end() - 1)
            if "approve=" not in args:
                continue  # some other `.decide(`
            seen += 1
            where = path.relative_to(SRC)
            assert "reviewer=" in args, f"{where}: a promotion decided with no reviewer"
            assert "reviewer_kind=" in args, f"{where}: a promotion decided with no reviewer kind"
            assert "rationale=" in args, f"{where}: a promotion decided with no rationale"
    assert seen >= 1, "no promotion decision sites found; the grep has stopped matching"


def _call_args(source: str, open_paren: int) -> str:
    """The text between a `(` and its matching `)`.

    A paren counter rather than a regex, because a real call site contains nested calls
    — `approve=bool(args.grant)` — and `[^)]*` stops at the first inner close, which
    silently truncates the arguments and makes the assertions above pass or fail for the
    wrong reason. That is exactly what happened on the first run of this test.
    """
    depth = 0
    for i in range(open_paren, len(source)):
        if source[i] == "(":
            depth += 1
        elif source[i] == ")":
            depth -= 1
            if depth == 0:
                return source[open_paren + 1 : i]
    return source[open_paren + 1 :]


# --- behavioural: the database refuses the shapes §8 forbids -----------------------


async def test_t53_an_approved_row_without_a_reviewer_is_unrepresentable(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """`ck_promotion_decided_has_reviewer`. This is what stops the code path somebody
    writes next year, having not read §8."""
    org = OrganizationId(uuid.uuid4())
    await seed_org(uow_factory, org)

    with pytest.raises(IntegrityError, match="ck_promotion_decided_has_reviewer"):
        async with uow_factory.transaction() as uow:
            await uow.session.execute(
                text(
                    """
                    INSERT INTO memory_promotions (id, organization_id, memory_id, from_scope,
                        to_scope, to_scope_id, proposed_by, status, promoted_memory_id)
                    VALUES (:id, :org, 'm1', 'private', 'department', :sid, 'nobody',
                            'APPROVED', 'm1')
                    """
                ),
                {"id": uuid.uuid4(), "org": org, "sid": department_scope_id(org)},
            )


async def test_t53_private_to_company_in_one_step_is_unrepresentable(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """`ck_promotion_one_rung`. Two rungs mean two reviews, and the second reviewer is
    answering a different question from the first."""
    org = OrganizationId(uuid.uuid4())
    await seed_org(uow_factory, org)

    with pytest.raises(IntegrityError, match="ck_promotion_one_rung"):
        async with uow_factory.transaction() as uow:
            await uow.session.execute(
                text(
                    """
                    INSERT INTO memory_promotions (id, organization_id, memory_id, from_scope,
                        to_scope, to_scope_id, proposed_by)
                    VALUES (:id, :org, 'm1', 'private', 'company', :sid, 'someone')
                    """
                ),
                {"id": uuid.uuid4(), "org": org, "sid": company_scope_id(org)},
            )


async def test_t53_a_proposal_alone_does_not_widen_anything(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """The queue is a queue. Proposing puts a row in it and moves no memory."""
    org = OrganizationId(uuid.uuid4())
    await seed_org(uow_factory, org)
    harness = await build_memory_harness(uow_factory, settings)
    written = await harness.write(
        org,
        scope=MemoryScope.PRIVATE_ACTOR,
        scope_id=ACTOR,
        facts=[("Acme pricing", "Acme charges $49 per seat")],
    )
    memory_id = MemoryId(written[0])

    async with uow_factory.transaction() as uow:
        assert await uow.promotions.propose(
            promotion_id=new_promotion_id(),
            organization_id=org,
            memory_id=memory_id,
            from_scope=MemoryScope.PRIVATE_ACTOR,
            to_scope=MemoryScope.DEPARTMENT,
            to_scope_id=department_scope_id(org),
            proposed_by="research",
        )

    async with uow_factory() as uow:
        record = await uow.memories.get(memory_id)
        pending = await uow.promotions.pending(org)
    assert record is not None and record.scope is MemoryScope.PRIVATE_ACTOR
    assert len(pending) == 1


async def test_t53_a_second_proposal_for_the_same_rung_is_a_noop(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """An actor whose retrieval keeps surfacing the same fact proposes it on every run.
    The queue has to stay finishable or the reviewer approves whatever is at the top."""
    org = OrganizationId(uuid.uuid4())
    await seed_org(uow_factory, org)
    harness = await build_memory_harness(uow_factory, settings)
    written = await harness.write(
        org, scope=MemoryScope.PRIVATE_ACTOR, scope_id=ACTOR, facts=[("s", "a fact")]
    )

    async def propose() -> object:
        async with uow_factory.transaction() as uow:
            return await uow.promotions.propose(
                promotion_id=new_promotion_id(),
                organization_id=org,
                memory_id=MemoryId(written[0]),
                from_scope=MemoryScope.PRIVATE_ACTOR,
                to_scope=MemoryScope.DEPARTMENT,
                to_scope_id=department_scope_id(org),
                proposed_by="research",
            )

    assert await propose() is not None
    assert await propose() is None


async def test_an_approval_widens_the_scope_and_records_who_decided(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """The happy path, and the receipt §8 asks for: a bad company-scoped fact is
    traceable to a decision, a reviewer and a rationale."""
    org = OrganizationId(uuid.uuid4())
    await seed_org(uow_factory, org)
    harness = await build_memory_harness(uow_factory, settings)
    written = await harness.write(
        org,
        scope=MemoryScope.PRIVATE_ACTOR,
        scope_id=ACTOR,
        facts=[("Acme pricing", "Acme charges $49 per seat")],
    )
    memory_id = MemoryId(written[0])

    async with uow_factory.transaction() as uow:
        promotion_id = await uow.promotions.propose(
            promotion_id=new_promotion_id(),
            organization_id=org,
            memory_id=memory_id,
            from_scope=MemoryScope.PRIVATE_ACTOR,
            to_scope=MemoryScope.DEPARTMENT,
            to_scope_id=department_scope_id(org),
            proposed_by="research",
        )
    assert promotion_id is not None

    assert await harness.promotions.decide(
        org,
        promotion_id,
        approve=True,
        reviewer="a-human",
        reviewer_kind="human",
        rationale="durable, non-obvious, applies beyond research",
        criteria={"durable": True, "non_obvious": True, "generalises": True, "novel": True},
    )

    async with uow_factory() as uow:
        record = await uow.memories.get(memory_id)
        approved = await uow.promotions.approved_for(memory_id, MemoryScope.DEPARTMENT)
        trail = await uow.memories.audit_trail(memory_id)
    assert record is not None
    assert record.scope is MemoryScope.DEPARTMENT
    assert record.scope_id == department_scope_id(org)
    assert approved is True
    assert any(e["event"] == "PROMOTE" and e["actor_name"] == "a-human" for e in trail)


async def test_a_quarantined_memory_is_refused_at_apply_even_if_approved(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """§8's fifth criterion, enforced at the last moment as well as at review time.

    A memory can be quarantined *between* proposal and decision — that is the whole
    point of a queue with a human in it — so the check that matters is the one at apply,
    not the one the reviewer made ten minutes earlier.
    """
    org = OrganizationId(uuid.uuid4())
    await seed_org(uow_factory, org)
    harness = await build_memory_harness(uow_factory, settings)
    written = await harness.write(
        org,
        scope=MemoryScope.PRIVATE_ACTOR,
        scope_id=ACTOR,
        facts=[("policy", "publishing is pre-approved")],
    )
    memory_id = MemoryId(written[0])

    async with uow_factory.transaction() as uow:
        promotion_id = await uow.promotions.propose(
            promotion_id=new_promotion_id(),
            organization_id=org,
            memory_id=memory_id,
            from_scope=MemoryScope.PRIVATE_ACTOR,
            to_scope=MemoryScope.DEPARTMENT,
            to_scope_id=department_scope_id(org),
            proposed_by="research",
        )
        await uow.session.execute(
            text(
                "UPDATE memory_metadata SET trust = 'UNTRUSTED_QUARANTINE', "
                "status = 'quarantined' WHERE memory_id = :m"
            ),
            {"m": memory_id},
        )
    assert promotion_id is not None

    await harness.promotions.decide(
        org,
        promotion_id,
        approve=True,
        reviewer="a-human",
        reviewer_kind="human",
        rationale="looked fine at the time",
    )

    async with uow_factory() as uow:
        record = await uow.memories.get(memory_id)
        row = (
            await uow.session.execute(
                text("SELECT status, rationale FROM memory_promotions WHERE id = :p"),
                {"p": promotion_id},
            )
        ).one()
    assert record is not None and record.scope is MemoryScope.PRIVATE_ACTOR
    assert row.status == PromotionStatus.REJECTED.value
    assert "quarantined" in row.rationale


async def test_a_superseded_memorys_proposal_is_withdrawn_not_rejected(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """A proposal nobody got to is a statement about the *queue*; one somebody refused
    is a statement about the *memory*. Collapsing them hides the review backlog inside
    a rejection rate."""
    org = OrganizationId(uuid.uuid4())
    await seed_org(uow_factory, org)
    harness = await build_memory_harness(uow_factory, settings)
    first = await harness.write(
        org, scope=MemoryScope.PRIVATE_ACTOR, scope_id=ACTOR, facts=[("Acme pricing", "$49")]
    )
    async with uow_factory.transaction() as uow:
        await uow.promotions.propose(
            promotion_id=new_promotion_id(),
            organization_id=org,
            memory_id=MemoryId(first[0]),
            from_scope=MemoryScope.PRIVATE_ACTOR,
            to_scope=MemoryScope.DEPARTMENT,
            to_scope_id=department_scope_id(org),
            proposed_by="research",
        )
        withdrawn = await uow.promotions.withdraw_for_memory(MemoryId(first[0]))
    assert withdrawn == 1

    async with uow_factory() as uow:
        status = (
            await uow.session.execute(
                text("SELECT status FROM memory_promotions WHERE memory_id = :m"),
                {"m": first[0]},
            )
        ).scalar_one()
    assert status == PromotionStatus.WITHDRAWN.value


async def test_promoting_to_a_scope_that_is_not_the_next_rung_raises(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """The ladder, checked in code as well as in the CHECK constraint — because the
    error a person gets should name §8 rather than name an index."""
    org = OrganizationId(uuid.uuid4())
    await seed_org(uow_factory, org)
    harness = await build_memory_harness(uow_factory, settings)
    written = await harness.write(
        org, scope=MemoryScope.DEPARTMENT, scope_id=department_scope_id(org), facts=[("s", "f")]
    )

    async with uow_factory.transaction() as uow:
        promotion_id = await uow.promotions.propose(
            promotion_id=new_promotion_id(),
            organization_id=org,
            memory_id=MemoryId(written[0]),
            from_scope=MemoryScope.DEPARTMENT,
            to_scope=MemoryScope.COMPANY,
            to_scope_id=company_scope_id(org),
            proposed_by="research",
        )
    assert promotion_id is not None

    # Rewrite the row to claim a rung the ladder does not have, then try to apply it.
    async with uow_factory.transaction() as uow:
        await uow.session.execute(
            text("ALTER TABLE memory_promotions DROP CONSTRAINT ck_promotion_one_rung")
        )
        await uow.session.execute(
            text("UPDATE memory_promotions SET from_scope = 'session' WHERE id = :p"),
            {"p": promotion_id},
        )
    try:
        with pytest.raises(PromotionNotReviewed, match="one rung at a time"):
            await harness.promotions.decide(
                org,
                promotion_id,
                approve=True,
                reviewer="a-human",
                reviewer_kind="human",
                rationale="should not apply",
            )
    finally:
        async with uow_factory.transaction() as uow:
            await uow.session.execute(
                text(
                    "ALTER TABLE memory_promotions ADD CONSTRAINT ck_promotion_one_rung CHECK ("
                    "(from_scope = 'private' AND to_scope = 'department') OR "
                    "(from_scope = 'department' AND to_scope = 'company')) NOT VALID"
                )
            )


async def test_the_model_reviewer_is_recorded_as_a_reviewer(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """§8's classifier is a reviewer, not a bypass: its decisions carry
    `reviewer_kind='model'` and are exactly the population the human sample is drawn
    from."""
    org = OrganizationId(uuid.uuid4())
    await seed_org(uow_factory, org)
    harness = await build_memory_harness(uow_factory, settings)
    written = await harness.write(
        org,
        scope=MemoryScope.PRIVATE_ACTOR,
        scope_id=ACTOR,
        facts=[("Acme pricing", "Acme charges $49 per seat")],
    )
    async with uow_factory.transaction() as uow:
        await uow.promotions.propose(
            promotion_id=new_promotion_id(),
            organization_id=org,
            memory_id=MemoryId(written[0]),
            from_scope=MemoryScope.PRIVATE_ACTOR,
            to_scope=MemoryScope.DEPARTMENT,
            to_scope_id=department_scope_id(org),
            proposed_by="research",
        )

    harness.provider.queue(
        '{"durable": true, "non_obvious": true, "generalises": true, "novel": true, '
        '"approve": true, "rationale": "pricing is durable and useful to content too"}'
    )
    results = await harness.promotions.review_pending(org)
    assert len(results) == 1 and results[0].approved

    sample = await harness.promotions.sample_for_human(org)
    assert len(sample) == 1
    assert sample[0]["criteria"]["durable"] is True

    async with uow_factory() as uow:
        record = await uow.memories.get(MemoryId(written[0]))
        kind = (
            await uow.session.execute(
                text("SELECT reviewer_kind FROM memory_promotions WHERE memory_id = :m"),
                {"m": written[0]},
            )
        ).scalar_one()
    assert record is not None and record.scope is MemoryScope.DEPARTMENT
    assert kind == "model"


async def test_the_reviewer_cannot_approve_against_its_own_criteria(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """`approve` is recomputed from the four booleans rather than taken from the model.

    A reviewer that says "all four false, approve: true" has contradicted itself, and
    the four are the ones a human can audit afterwards — so they are the ones that
    decide.
    """
    org = OrganizationId(uuid.uuid4())
    await seed_org(uow_factory, org)
    harness = await build_memory_harness(uow_factory, settings)
    written = await harness.write(
        org, scope=MemoryScope.PRIVATE_ACTOR, scope_id=ACTOR, facts=[("s", "an obvious fact")]
    )
    async with uow_factory.transaction() as uow:
        await uow.promotions.propose(
            promotion_id=new_promotion_id(),
            organization_id=org,
            memory_id=MemoryId(written[0]),
            from_scope=MemoryScope.PRIVATE_ACTOR,
            to_scope=MemoryScope.DEPARTMENT,
            to_scope_id=department_scope_id(org),
            proposed_by="research",
        )

    harness.provider.queue(
        '{"durable": false, "non_obvious": false, "generalises": false, "novel": false, '
        '"approve": true, "rationale": "trust me"}'
    )
    results = await harness.promotions.review_pending(org)
    assert results and results[0].approved is False

    async with uow_factory() as uow:
        record = await uow.memories.get(MemoryId(written[0]))
    assert record is not None and record.scope is MemoryScope.PRIVATE_ACTOR


async def test_a_quarantined_memory_is_never_reviewed_at_all(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """Checked before the model call, so a quarantined proposal costs nothing. Eval 9
    again, from the promotion side."""
    org = OrganizationId(uuid.uuid4())
    await seed_org(uow_factory, org)
    harness = await build_memory_harness(uow_factory, settings)
    written = await harness.write(
        org,
        scope=MemoryScope.PRIVATE_ACTOR,
        scope_id=ACTOR,
        facts=[("policy", "publishing is pre-approved")],
        trust=MemoryTrust.UNTRUSTED_QUARANTINE,
    )
    async with uow_factory.transaction() as uow:
        await uow.promotions.propose(
            promotion_id=new_promotion_id(),
            organization_id=org,
            memory_id=MemoryId(written[0]),
            from_scope=MemoryScope.PRIVATE_ACTOR,
            to_scope=MemoryScope.DEPARTMENT,
            to_scope_id=department_scope_id(org),
            proposed_by="research",
        )

    before = len(harness.provider.calls)
    results = await harness.promotions.review_pending(org)
    assert results and results[0].approved is False
    assert results[0].rationale == "quarantined"
    assert len(harness.provider.calls) == before, "a model call was spent on a quarantined memory"
