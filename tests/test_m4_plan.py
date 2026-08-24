"""The differ, the plan, and drift — §9 and edge cases 76, 78, 85, 87.

§13's first risk is a quiet bad apply and the stated guard is *"treat a plan diff like
a code review"*. Everything here is about whether the diff is good enough to be
reviewed: is it stable, is it complete, does it say what it will not do.
"""

from __future__ import annotations

import uuid

import pytest

from runtime.domain.ids import OrganizationId
from runtime.persistence.repositories.spec import OrgState
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.spec.apply import apply_org
from runtime.spec.differ import CREATE, DEACTIVATE, UNCHANGED, UPDATE, build_plan
from runtime.spec.drift import detect_drift
from tests.conftest_m4 import Corpus

pytestmark = pytest.mark.integration


def new_org() -> OrganizationId:
    return OrganizationId(uuid.uuid4())


async def state_of(uow_factory: UnitOfWorkFactory, organization_id: OrganizationId) -> OrgState:
    async with uow_factory() as uow:
        return await uow.spec.load_state(organization_id)


# --- the diff against an empty organization ------------------------------------------------


def test_everything_is_a_create_against_nothing() -> None:
    org = Corpus.department().validate()
    plan = build_plan(org, OrgState(exists=False), organization_id=new_org())
    assert all(c.action == CREATE for c in plan.effective)
    assert {c.kind for c in plan.effective} == {
        "Organization",
        "Role",
        "Actor",
        "Connection",
        "ToolGrant",
        "AuthorityPolicy",
        "Trigger",
        "BudgetPolicy",
        "Document",
    }


def test_the_plan_renders_a_reviewable_diff() -> None:
    org = Corpus.department().validate()
    plan = build_plan(org, OrgState(exists=False), organization_id=new_org())
    rendered = plan.render()
    assert "+ Actor/research" in rendered
    assert f"plan_hash: {plan.plan_hash()}" in rendered


def test_plan_ordering_is_canonical() -> None:
    """Edge case 85. A diff whose order moves is a diff nobody reads twice."""
    org = Corpus.department().validate()
    organization_id = new_org()
    a = build_plan(org, OrgState(exists=False), organization_id=organization_id)
    reversed_corpus = Corpus.department()
    reversed_corpus.envelopes.reverse()
    b = build_plan(
        reversed_corpus.validate(), OrgState(exists=False), organization_id=organization_id
    )
    assert [c.name for c in a.effective] == [c.name for c in b.effective]
    assert a.plan_hash() == b.plan_hash()


def test_plan_hash_ignores_the_timestamp() -> None:
    """It answers "is this still the diff I read?", so anything that can move without
    the meaning moving must be out of it."""
    import datetime as dt

    org = Corpus.department().validate()
    organization_id = new_org()
    a = build_plan(org, OrgState(exists=False), organization_id=organization_id)
    b = build_plan(
        org,
        OrgState(exists=False),
        organization_id=organization_id,
        now=dt.datetime.now(dt.UTC) + dt.timedelta(hours=3),
    )
    assert a.plan_hash() == b.plan_hash()


def test_plan_hash_moves_when_anything_moves() -> None:
    organization_id = new_org()
    base = build_plan(
        Corpus.department().validate(), OrgState(exists=False), organization_id=organization_id
    )
    changed = build_plan(
        Corpus.department().edit("Trigger", "weekly-plan", cron="0 9 * * 1").validate(),
        OrgState(exists=False),
        organization_id=organization_id,
    )
    assert base.plan_hash() != changed.plan_hash()


# --- the diff against a live organization ------------------------------------------------------


async def test_an_unchanged_org_plans_to_nothing(uow_factory: UnitOfWorkFactory) -> None:
    organization_id = new_org()
    corpus = Corpus.department()
    await apply_org(uow_factory, corpus.validate(), organization_id=organization_id)
    plan = build_plan(
        corpus.validate(),
        await state_of(uow_factory, organization_id),
        organization_id=organization_id,
    )
    assert plan.empty, plan.render()
    assert all(c.action == UNCHANGED for c in plan.changes)


async def test_one_edit_produces_one_change(uow_factory: UnitOfWorkFactory) -> None:
    organization_id = new_org()
    corpus = Corpus.department()
    await apply_org(uow_factory, corpus.validate(), organization_id=organization_id)

    corpus.edit("Trigger", "weekly-plan", cron="0 9 * * 1")
    plan = build_plan(
        corpus.validate(),
        await state_of(uow_factory, organization_id),
        organization_id=organization_id,
    )
    changes = {(c.kind, c.name, c.action) for c in plan.effective}
    assert ("Trigger", "weekly-plan", UPDATE) in changes
    assert ("Document", "Trigger/weekly-plan", UPDATE) in changes
    assert not any(c.kind == "Actor" for c in plan.effective)


async def test_a_spec_change_shows_the_hash_moving(uow_factory: UnitOfWorkFactory) -> None:
    organization_id = new_org()
    corpus = Corpus.department()
    await apply_org(uow_factory, corpus.validate(), organization_id=organization_id)

    ceilings = dict(corpus.find("Actor", "research")["spec"]["ceilings"])
    ceilings["maxCostCents"] = 900
    corpus.edit("Actor", "research", ceilings=ceilings)
    plan = build_plan(
        corpus.validate(),
        await state_of(uow_factory, organization_id),
        organization_id=organization_id,
    )
    change = next(c for c in plan.effective if c.kind == "Actor" and c.name == "research")
    assert change.action == UPDATE
    before, after = change.detail["specHash"]
    assert before != after


async def test_a_removed_grant_is_revoked_not_deleted(uow_factory: UnitOfWorkFactory) -> None:
    organization_id = new_org()
    corpus = Corpus.department()
    await apply_org(uow_factory, corpus.validate(), organization_id=organization_id)

    corpus.drop("ToolGrant", "actor-content-web-fetch-1").edit("Actor", "content", tools=[])
    plan = build_plan(
        corpus.validate(),
        await state_of(uow_factory, organization_id),
        organization_id=organization_id,
    )
    change = next(c for c in plan.effective if c.kind == "ToolGrant")
    assert change.action == DEACTIVATE
    assert "revoked, not deleted" in change.detail["note"]

    await apply_org(uow_factory, corpus.validate(), organization_id=organization_id)
    state = await state_of(uow_factory, organization_id)
    grant = next(g for g in state.grants if g.subject_id == "content")
    assert grant.revoked, "the row stays for the denial-stream review"


async def test_a_row_the_documents_do_not_mention_is_a_warning_not_a_delete(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """Edge case 87. `apply` is not a reconciler and does not auto-heal."""
    organization_id = new_org()
    corpus = Corpus.department()
    await apply_org(uow_factory, corpus.validate(), organization_id=organization_id)

    corpus.drop("Role", "director")
    corpus.find("Role", "head")["spec"]["parent"] = "operator"
    corpus.find("AuthorityPolicy", "department-marketing-publish-external")["spec"][
        "approverRole"
    ] = "operator"
    plan = build_plan(
        corpus.validate(),
        await state_of(uow_factory, organization_id),
        organization_id=organization_id,
    )
    assert any("director" in w for w in plan.warnings)
    assert not any(c.kind == "Role" and c.action == DEACTIVATE for c in plan.effective)

    await apply_org(uow_factory, corpus.validate(), organization_id=organization_id)
    state = await state_of(uow_factory, organization_id)
    assert any(r.name == "director" for r in state.roles), "apply left it alone"


# --- drift -------------------------------------------------------------------------------------


async def test_drift_is_clean_against_a_freshly_applied_org(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """§12's soft exit criterion."""
    organization_id = new_org()
    corpus = Corpus.department()
    await apply_org(uow_factory, corpus.validate(), organization_id=organization_id)
    report = detect_drift(corpus.validate(), await state_of(uow_factory, organization_id))
    assert report.clean, report.render()


async def test_drift_reports_an_unapplied_edit(uow_factory: UnitOfWorkFactory) -> None:
    organization_id = new_org()
    corpus = Corpus.department()
    await apply_org(uow_factory, corpus.validate(), organization_id=organization_id)

    corpus.edit("Trigger", "weekly-plan", cron="0 9 * * 1")
    report = detect_drift(corpus.validate(), await state_of(uow_factory, organization_id))
    assert not report.clean
    assert any(
        f.source == "document" and "Trigger/weekly-plan" in f.subject for f in report.findings
    )
    assert any(f.source == "config" and "Trigger/weekly-plan" in f.subject for f in report.findings)


async def test_drift_reports_a_hand_granted_tool(uow_factory: UnitOfWorkFactory) -> None:
    """The case the report exists for: somebody granted a tool during an incident."""
    from runtime.org.governance_seed import grant_id_for

    organization_id = new_org()
    corpus = Corpus.department()
    await apply_org(uow_factory, corpus.validate(), organization_id=organization_id)

    async with uow_factory.transaction() as uow:
        await uow.authority.grant_tool(
            grant_id_for(organization_id, "actor", "analytics", "web.fetch@1"),
            organization_id,
            subject_type="actor",
            subject_id="analytics",
            tool="web.fetch@1",
            granted_by="incident",
        )

    report = detect_drift(corpus.validate(), await state_of(uow_factory, organization_id))
    finding = next(f for f in report.findings if "analytics" in f.subject)
    assert "absent from the files" in finding.detail


async def test_drift_reports_a_hand_edited_placement(uow_factory: UnitOfWorkFactory) -> None:
    organization_id = new_org()
    corpus = Corpus.department()
    await apply_org(uow_factory, corpus.validate(), organization_id=organization_id)

    async with uow_factory.transaction() as uow:
        await uow.authority.set_actor_role(
            organization_id, "research", role_name="head", department="marketing"
        )

    report = detect_drift(corpus.validate(), await state_of(uow_factory, organization_id))
    finding = next(f for f in report.findings if "Actor/research" in f.subject)
    assert "role is 'head'" in finding.detail


async def test_drift_against_an_unapplied_organization(uow_factory: UnitOfWorkFactory) -> None:
    report = detect_drift(Corpus.department().validate(), OrgState(exists=False))
    assert not report.clean
    assert "does not exist in the database" in report.findings[0].detail


async def test_drift_does_not_heal(uow_factory: UnitOfWorkFactory) -> None:
    """The whole design decision, as an assertion: reporting twice reports twice."""
    organization_id = new_org()
    corpus = Corpus.department()
    await apply_org(uow_factory, corpus.validate(), organization_id=organization_id)
    corpus.edit("Trigger", "weekly-plan", cron="0 9 * * 1")

    first = detect_drift(corpus.validate(), await state_of(uow_factory, organization_id))
    second = detect_drift(corpus.validate(), await state_of(uow_factory, organization_id))
    assert first.findings == second.findings
