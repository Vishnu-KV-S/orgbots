"""`apply` — §7, §12 and edge cases 72-79.

Integration: a real Postgres, because every property here is a property of the actual
database. Advisory locks, `ON CONFLICT`, and "one transaction or none of it" are not
things a fake can be wrong about in the right way.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import uuid

import pytest
from sqlalchemy import text

from runtime.domain.errors import EscalationCycle
from runtime.domain.hashing import canonical_hash
from runtime.domain.ids import OrganizationId
from runtime.org.department import DEPARTMENT_SPECS
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.spec.apply import apply_org
from runtime.spec.differ import DEACTIVATE, build_plan
from runtime.spec.errors import ApplyConflict, PlanStale, RenameRefused, SpecValidationError
from tests.conftest_m4 import Corpus

pytestmark = pytest.mark.integration


def new_org() -> OrganizationId:
    return OrganizationId(uuid.uuid4())


async def state_of(uow_factory: UnitOfWorkFactory, organization_id: OrganizationId):
    async with uow_factory() as uow:
        return await uow.spec.load_state(organization_id)


async def plan_for(
    uow_factory: UnitOfWorkFactory, corpus: Corpus, organization_id: OrganizationId, **kwargs
):
    org = corpus.validate()
    return org, build_plan(
        org, await state_of(uow_factory, organization_id), organization_id=organization_id, **kwargs
    )


# --- the happy path -------------------------------------------------------------------


async def test_apply_builds_the_whole_department(uow_factory: UnitOfWorkFactory) -> None:
    organization_id = new_org()
    org = Corpus.department().validate()

    result = await apply_org(uow_factory, org, organization_id=organization_id)

    assert result.created > 0
    assert set(result.versions_published) == {spec.name for spec in DEPARTMENT_SPECS}

    state = await state_of(uow_factory, organization_id)
    assert {a.name for a in state.actors} == {spec.name for spec in DEPARTMENT_SPECS}
    assert {r.name for r in state.roles} == {"operator", "director", "head", "ic"}
    assert len(state.triggers) == 4
    assert len([g for g in state.grants if not g.revoked]) == 4


async def test_the_applied_spec_hash_is_the_python_one(uow_factory: UnitOfWorkFactory) -> None:
    """§8, end to end. Not just "the compiler agrees" but "the row agrees"."""
    organization_id = new_org()
    await apply_org(uow_factory, Corpus.department().validate(), organization_id=organization_id)

    async with uow_factory() as uow:
        hashes = await uow.spec.active_spec_hashes(organization_id)
    for spec in DEPARTMENT_SPECS:
        assert hashes[spec.name] == canonical_hash(spec)


async def test_the_applied_actor_resolves_and_runs_the_same_spec(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """The row the runtime actually reads. `resolve_active` is the one place live
    configuration is consulted, so it is the one that has to agree."""
    from runtime.domain.specs import ActorSpec

    organization_id = new_org()
    await apply_org(uow_factory, Corpus.department().validate(), organization_id=organization_id)
    async with uow_factory() as uow:
        resolved = await uow.actors.resolve_active(organization_id, "research")
    assert ActorSpec.model_validate(resolved.spec) == next(
        s for s in DEPARTMENT_SPECS if s.name == "research"
    )


async def test_apply_is_idempotent(uow_factory: UnitOfWorkFactory) -> None:
    """Running it twice produces one of everything — and, deliberately unlike
    `seed_department`, no second version."""
    organization_id = new_org()
    org = Corpus.department().validate()
    first = await apply_org(uow_factory, org, organization_id=organization_id)
    second = await apply_org(uow_factory, org, organization_id=organization_id)

    assert first.versions_published
    assert second.versions_published == {}, (
        "an apply that churned a version for every actor would make 'nothing changed' "
        "and 'everything changed' look the same in actor_versions (§13 risk 2)"
    )
    assert second.changed == 0

    state = await state_of(uow_factory, organization_id)
    assert len(state.actors) == 4
    assert len(state.roles) == 4


async def test_a_second_apply_plans_to_nothing(uow_factory: UnitOfWorkFactory) -> None:
    organization_id = new_org()
    corpus = Corpus.department()
    await apply_org(uow_factory, corpus.validate(), organization_id=organization_id)
    _, plan = await plan_for(uow_factory, corpus, organization_id)
    assert plan.empty, plan.render()


async def test_a_changed_ceiling_publishes_exactly_one_new_version(
    uow_factory: UnitOfWorkFactory,
) -> None:
    organization_id = new_org()
    corpus = Corpus.department()
    await apply_org(uow_factory, corpus.validate(), organization_id=organization_id)

    ceilings = dict(corpus.find("Actor", "research")["spec"]["ceilings"])
    ceilings["maxToolCalls"] = 30
    corpus.edit("Actor", "research", ceilings=ceilings)

    result = await apply_org(uow_factory, corpus.validate(), organization_id=organization_id)
    assert result.versions_published == {"research": 2}


async def test_documents_are_stored_with_their_source(uow_factory: UnitOfWorkFactory) -> None:
    """§9. The compiled column is a convenience; `source_yaml` is not."""
    organization_id = new_org()
    org = Corpus.department().validate()
    await apply_org(uow_factory, org, organization_id=organization_id, source_ref="deadbeef")

    async with uow_factory() as uow:
        rows = (
            await uow.session.execute(
                text(
                    """
                    SELECT kind, name, source_yaml, spec_hash, source_ref
                      FROM spec_documents WHERE organization_id = :org
                    """
                ),
                {"org": organization_id},
            )
        ).all()
    assert len(rows) == len(org.documents)
    actor_row = next(r for r in rows if r.kind == "Actor" and r.name == "research")
    assert "research@1" in actor_row.source_yaml
    assert actor_row.source_ref == "deadbeef"


async def test_reapplying_an_unchanged_document_writes_no_second_row(
    uow_factory: UnitOfWorkFactory,
) -> None:
    organization_id = new_org()
    org = Corpus.department().validate()
    first = await apply_org(uow_factory, org, organization_id=organization_id)
    second = await apply_org(uow_factory, org, organization_id=organization_id)
    assert first.documents_recorded == len(org.documents)
    assert second.documents_recorded == 0


async def test_apply_records_one_event_per_change(uow_factory: UnitOfWorkFactory) -> None:
    organization_id = new_org()
    await apply_org(uow_factory, Corpus.department().validate(), organization_id=organization_id)
    async with uow_factory() as uow:
        events = await uow.spec.events(organization_id, limit=200)
    assert any(e["kind"] == "Actor" and e["name"] == "research" for e in events)
    assert all(e["applied_by"] == "operator" for e in events)


# --- §7: two phases, one transaction ------------------------------------------------------


async def test_references_survive_a_document_set_in_any_order(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """§7's circular reference: the department names its head and the head names the
    department. Reversing the document order must change nothing."""
    organization_id = new_org()
    corpus = Corpus.department()
    corpus.envelopes.reverse()
    await apply_org(uow_factory, corpus.validate(), organization_id=organization_id)

    state = await state_of(uow_factory, organization_id)
    head = state.actor("marketing-head")
    assert head is not None
    assert head.department == "marketing"
    assert head.role_name == "head"
    research = state.actor("research")
    assert research is not None
    assert research.reports_to == "marketing-head"
    assert {r.name: r.parent for r in state.roles}["ic"] == "head"


async def test_a_failure_in_phase_two_rolls_back_phase_one(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """Edge case 74. There is no half-applied state, ever.

    The failure is injected where §7 says the risk is: after the entities are written
    and while the references are being filled in. A cyclic role tree passes the
    compiler (its checks run on documents) and fails `check_escalation_acyclicity`,
    which the applier runs *inside* the transaction — same placement, same argument, as
    `seed_governance`.
    """
    organization_id = new_org()
    corpus = Corpus.department()
    # Rank says operator is junior to ic while the tree says the opposite. Documents
    # compile; the in-transaction check refuses.
    corpus.find("Role", "operator")["spec"]["rank"] = 99
    org = corpus.compile()

    with pytest.raises(EscalationCycle, match=r"rank|senior|cycle"):
        await apply_org(uow_factory, org, organization_id=organization_id)

    state = await state_of(uow_factory, organization_id)
    assert state.actors == ()
    assert state.roles == ()
    assert state.documents == ()


async def test_a_rolled_back_apply_leaves_no_plan_marked_applied(
    uow_factory: UnitOfWorkFactory,
) -> None:
    organization_id = new_org()
    corpus = Corpus.department()
    corpus.find("Role", "operator")["spec"]["rank"] = 99
    with pytest.raises(EscalationCycle):
        await apply_org(uow_factory, corpus.compile(), organization_id=organization_id)

    async with uow_factory() as uow:
        rows = (
            await uow.session.execute(
                text("SELECT count(*) FROM apply_plans WHERE organization_id = :org"),
                {"org": organization_id},
            )
        ).scalar_one()
    assert rows == 0


# --- edge case 75: two concurrent applies --------------------------------------------------


async def test_a_concurrent_apply_is_refused_not_interleaved(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """The advisory lock. Two concurrent applies must serialise, not interleave."""
    organization_id = new_org()
    org = Corpus.department().validate()

    async with uow_factory.transaction() as holder:
        held = await holder.spec.lock_organization(organization_id)
        assert held
        with pytest.raises(ApplyConflict, match="another apply holds the lock"):
            await apply_org(uow_factory, org, organization_id=organization_id)

    # Released with the transaction, so the retry works.
    await apply_org(uow_factory, org, organization_id=organization_id)


async def test_the_lock_is_per_organization(uow_factory: UnitOfWorkFactory) -> None:
    """An apply to one org must not block an apply to another."""
    a, b = new_org(), new_org()
    org = Corpus.department().validate()
    async with uow_factory.transaction() as holder:
        assert await holder.spec.lock_organization(a)
        await apply_org(uow_factory, org, organization_id=b)


# --- edge case 76: plan/apply skew -----------------------------------------------------------


async def test_applying_a_plan_whose_state_moved_is_refused(
    uow_factory: UnitOfWorkFactory,
) -> None:
    organization_id = new_org()
    corpus = Corpus.department()
    org, plan = await plan_for(uow_factory, corpus, organization_id)

    # Somebody else applies something in between.
    await apply_org(uow_factory, org, organization_id=organization_id)

    with pytest.raises(PlanStale, match="the organization moved"):
        await apply_org(uow_factory, org, organization_id=organization_id, plan=plan)


async def test_a_plan_older_than_the_window_is_stale_regardless(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """`[CHOSEN]` 15 minutes. A plan that is right by luck is not a plan that was
    checked."""
    organization_id = new_org()
    corpus = Corpus.department()
    org, plan = await plan_for(uow_factory, corpus, organization_id)
    old = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=16)
    aged = dataclasses.replace(plan, created_at=old)

    with pytest.raises(PlanStale, match="older than the 15-minute window"):
        await apply_org(uow_factory, org, organization_id=organization_id, plan=aged)


async def test_a_fresh_plan_applies(uow_factory: UnitOfWorkFactory) -> None:
    organization_id = new_org()
    corpus = Corpus.department()
    org, plan = await plan_for(uow_factory, corpus, organization_id)
    result = await apply_org(uow_factory, org, organization_id=organization_id, plan=plan)
    assert result.changed == len(plan.effective)


# --- edge cases 77 and 78: rename and removal ---------------------------------------------------


async def test_removing_an_actor_deactivates_it_and_keeps_its_versions(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """Edge case 78. A hard delete breaks resumed runs; versions are never deleted."""
    organization_id = new_org()
    corpus = Corpus.department()
    await apply_org(uow_factory, corpus.validate(), organization_id=organization_id)

    corpus.drop("Actor", "content").drop("ToolGrant", "actor-content-web-fetch-1")
    corpus.edit("BudgetPolicy", "default", actors={})
    result = await apply_org(
        uow_factory, corpus.validate(), organization_id=organization_id, allow_replace=True
    )
    assert result.deactivated >= 1

    state = await state_of(uow_factory, organization_id)
    content = state.actor("content")
    assert content is not None and not content.active
    async with uow_factory() as uow:
        versions = (
            await uow.session.execute(
                text("SELECT count(*) FROM actor_versions WHERE actor_id = :id"),
                {"id": content.id},
            )
        ).scalar_one()
    assert versions == 1


async def test_a_removal_plus_a_creation_is_refused_without_intent(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """§4b / edge case 77. Nothing in the files can tell a rename from a
    delete-plus-create, so the tool refuses to guess when there is history to lose."""
    organization_id = new_org()
    corpus = Corpus.department()
    await apply_org(uow_factory, corpus.validate(), organization_id=organization_id)
    await _record_a_run(uow_factory, organization_id, "content")

    corpus.rename("Actor", "content", "copywriter")
    corpus.find("ToolGrant", "actor-content-web-fetch-1")["spec"]["subject"] = {
        "actor": "copywriter"
    }
    with pytest.raises(RenameRefused, match="--rename OLD=NEW"):
        await plan_for(uow_factory, corpus, organization_id)


async def test_an_explicit_rename_keeps_the_history(uow_factory: UnitOfWorkFactory) -> None:
    organization_id = new_org()
    corpus = Corpus.department()
    await apply_org(uow_factory, corpus.validate(), organization_id=organization_id)
    before = (await state_of(uow_factory, organization_id)).actor("content")
    assert before is not None
    await _record_a_run(uow_factory, organization_id, "content")

    corpus.rename("Actor", "content", "copywriter")
    corpus.find("ToolGrant", "actor-content-web-fetch-1")["spec"]["subject"] = {
        "actor": "copywriter"
    }
    corpus.find("Trigger", "weekly-plan")  # unchanged; just proving the corpus still loads

    await apply_org(
        uow_factory,
        corpus.validate(),
        organization_id=organization_id,
        renames={"content": "copywriter"},
    )

    state = await state_of(uow_factory, organization_id)
    assert state.actor("content") is None
    after = state.actor("copywriter")
    assert after is not None
    assert after.id == before.id, "a rename keeps the row, its id, its versions and its history"
    assert after.run_count == 1


async def test_a_rename_onto_an_existing_name_is_refused(
    uow_factory: UnitOfWorkFactory,
) -> None:
    organization_id = new_org()
    corpus = Corpus.department()
    await apply_org(uow_factory, corpus.validate(), organization_id=organization_id)
    with pytest.raises(RenameRefused, match="already exists"):
        await plan_for(uow_factory, corpus, organization_id, renames={"content": "research"})


async def test_allow_replace_permits_the_delete_plus_create(
    uow_factory: UnitOfWorkFactory,
) -> None:
    organization_id = new_org()
    corpus = Corpus.department()
    await apply_org(uow_factory, corpus.validate(), organization_id=organization_id)
    await _record_a_run(uow_factory, organization_id, "content")

    corpus.rename("Actor", "content", "copywriter")
    corpus.find("ToolGrant", "actor-content-web-fetch-1")["spec"]["subject"] = {
        "actor": "copywriter"
    }
    _, plan = await plan_for(uow_factory, corpus, organization_id, allow_replace=True)
    actions = {(c.name, c.action) for c in plan.effective if c.kind == "Actor"}
    assert ("content", DEACTIVATE) in actions


# --- edge case 73: deactivating an actor with live runs -------------------------------------------


async def test_deactivating_an_actor_with_live_runs_is_refused(
    uow_factory: UnitOfWorkFactory,
) -> None:
    organization_id = new_org()
    corpus = Corpus.department()
    await apply_org(uow_factory, corpus.validate(), organization_id=organization_id)
    await _record_a_run(uow_factory, organization_id, "content", status="RUNNING")

    corpus.drop("Actor", "content").drop("ToolGrant", "actor-content-web-fetch-1")
    corpus.edit("BudgetPolicy", "default", actors={})
    with pytest.raises(SpecValidationError, match="orphaned"):
        await plan_for(uow_factory, corpus, organization_id, allow_replace=True)


# --- edge case 72: apply while runs are in flight ------------------------------------------------


async def test_an_in_flight_run_keeps_its_pinned_spec(uow_factory: UnitOfWorkFactory) -> None:
    """I2. A run holds the version it was admitted under; a new version affects only
    new runs. No new mechanism — this test says so rather than assuming it."""
    organization_id = new_org()
    corpus = Corpus.department()
    await apply_org(uow_factory, corpus.validate(), organization_id=organization_id)
    async with uow_factory() as uow:
        before = await uow.actors.resolve_active(organization_id, "research")

    ceilings = dict(corpus.find("Actor", "research")["spec"]["ceilings"])
    ceilings["maxToolCalls"] = 30
    corpus.edit("Actor", "research", ceilings=ceilings)
    await apply_org(uow_factory, corpus.validate(), organization_id=organization_id)

    async with uow_factory() as uow:
        after = await uow.actors.resolve_active(organization_id, "research")
        row = (
            await uow.session.execute(
                text("SELECT spec, spec_hash FROM actor_versions WHERE id = :id"),
                {"id": before.actor_version_id},
            )
        ).one()
    assert after.spec_hash != before.spec_hash
    assert row.spec_hash == before.spec_hash, "the old version row is immutable"
    assert row.spec["ceilings"]["max_tool_calls"] == 24


# --- edge case 79: a budget lowered below what is committed --------------------------------------


async def test_lowering_a_budget_below_committed_is_refused_with_the_figure(
    uow_factory: UnitOfWorkFactory,
) -> None:
    from runtime.budget.service import BudgetService, department_scope_id

    organization_id = new_org()
    corpus = Corpus.department()
    await apply_org(uow_factory, corpus.validate(), organization_id=organization_id)

    async with uow_factory.transaction() as uow:
        await BudgetService().apply_limits(
            uow, organization_id, organization_cents=100_000, departments={"marketing": 60_000}
        )
        await uow.session.execute(
            text(
                """
                UPDATE budget_pools SET committed_cents = 40000
                 WHERE organization_id = :org AND scope_type = 'department' AND scope_id = :sid
                """
            ),
            {"org": organization_id, "sid": department_scope_id(organization_id, "marketing")},
        )

    corpus.edit(
        "BudgetPolicy",
        "default",
        organization=100_000,
        departments={"marketing": 1_000},
    )
    with pytest.raises(SpecValidationError, match="40000c is already committed"):
        await plan_for(uow_factory, corpus, organization_id)


async def test_raising_a_budget_is_applied(uow_factory: UnitOfWorkFactory) -> None:
    organization_id = new_org()
    corpus = Corpus.department()
    await apply_org(uow_factory, corpus.validate(), organization_id=organization_id)

    corpus.edit(
        "BudgetPolicy",
        "default",
        organization=200_000,
        departments={"marketing": 90_000},
    )
    result = await apply_org(uow_factory, corpus.validate(), organization_id=organization_id)
    moved = {scope: (before, after) for scope, before, after in result.budget_changes}
    assert moved["org"] == (100_000, 200_000)
    assert moved["department:marketing"] == (60_000, 90_000)


# --- helpers -------------------------------------------------------------------------------------


async def _record_a_run(
    uow_factory: UnitOfWorkFactory,
    organization_id: OrganizationId,
    actor_name: str,
    *,
    status: str = "SUCCESS",
) -> None:
    """One `runs` row for an actor, so the rename and drain guards have history to see.

    Written directly rather than through `RunService` on purpose: what the guards read
    is a count of rows, and going through admission would drag a budget pool, a kill
    switch and an authority resolution into a test about a name.
    """
    async with uow_factory.transaction() as uow:
        actor_id = (await uow.spec.actor_ids(organization_id))[actor_name]
        await uow.session.execute(
            text(
                """
                INSERT INTO runs (id, organization_id, actor_id, actor_version_id, status,
                                  thread_id, idempotency_key, root_run_id)
                SELECT :id, :org, :actor, a.active_version_id, :status, :thread, :key, :id
                  FROM actors a WHERE a.id = :actor
                """
            ),
            {
                "id": uuid.uuid4(),
                "org": organization_id,
                "actor": actor_id,
                "status": status,
                "thread": f"t-{uuid.uuid4()}",
                "key": f"k-{uuid.uuid4()}",
            },
        )
