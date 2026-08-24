"""M2's structural guarantees — the ones that decay silently unless something checks.

Three kinds of thing live here:

- **The §10 risks, made mechanical.** Risk 2 says the single reservation function will
  get bypassed by a "quick" direct UPDATE. The import-linter contract catches an
  import; this catches the SQL, because `session.execute(text("UPDATE budget_pools…"))`
  needs no import at all.
- **The seeded governance configuration**, asserted end to end. A governance test
  against a hand-built role tree passes while the shipped one is broken.
- **Admission through the real `start_run()`**, which is where all of M2 meets M0.
"""

from __future__ import annotations

import pathlib
import re
import uuid

import pytest

from runtime.domain.enums import (
    AuthorityLevel,
    BlastRadius,
    OnExpiry,
    RunPriority,
    RunStatus,
)
from runtime.domain.ids import BudgetPoolId
from runtime.domain.specs import StartRunRequest
from runtime.org.authority import AuthorityResolver
from runtime.org.governance_seed import (
    ACTION_PUBLISH,
    DIRECTOR_ROLE,
    GRANTS,
    HEAD_ROLE,
    IC_ROLE,
    OPERATOR_ROLE,
)
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.runtime.run_service import RunService
from runtime.settings import Settings
from tests.conftest_m2 import new_governed_org

pytestmark = pytest.mark.integration

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "runtime"

BUDGET_SQL = re.compile(
    r"(UPDATE|INSERT\s+INTO|DELETE\s+FROM)\s+budget_(pools|reservations|allocations)",
    re.IGNORECASE,
)
ALLOWED_BUDGET_SQL = {
    "persistence/repositories/budget.py",
    "persistence/migrations/versions/006_budget.py",
    "persistence/migrations/versions/017_budget_tree.py",
}


def test_only_the_budget_repository_writes_to_the_budget_tables() -> None:
    """M2 §10, risk 2, as a scan rather than as a hope.

    The import contract stops a module *holding* `BudgetRepository`. It does not stop
    somebody writing raw SQL against `budget_pools` through the session they already
    have, which is exactly what "a quick direct UPDATE for a special case" looks like
    — and a second write path that did not take the chain lock in `depth ASC, id ASC`
    order would deadlock against `reserve_chain` under the concurrency T27 exercises.

    If this fails, the fix is not to add the file to the allow-list. It is to route the
    write through `BudgetService`.
    """
    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        relative = path.relative_to(SRC).as_posix()
        if relative in ALLOWED_BUDGET_SQL:
            continue
        if BUDGET_SQL.search(path.read_text()):
            offenders.append(relative)
    assert offenders == [], (
        f"raw budget-table writes outside the budget repository: {offenders}. "
        "Route them through BudgetService.reserve/reconcile so they take the chain "
        "lock in the one order everything else uses."
    )


def test_the_chain_lock_uses_the_documented_order_and_strength() -> None:
    """Two properties of one statement, both load-bearing and neither obvious.

    `depth ASC, id ASC` is the deadlock argument. `FOR NO KEY UPDATE` is what stops it
    conflicting with the `FOR KEY SHARE` that a foreign-key insert takes on the parent
    pool — the bug T27 actually caught. A refactor that "tidied" either one would pass
    every other test until the first busy morning.
    """
    source = (SRC / "persistence" / "repositories" / "budget.py").read_text()
    assert "ORDER BY b.depth ASC, b.id ASC" in source
    assert "FOR NO KEY UPDATE" in source
    assert "FOR UPDATE\n" not in source, "FOR UPDATE deadlocks against FK key-share locks"


# --- the shipped configuration --------------------------------------------------------


async def test_the_seeded_department_gates_publishing_and_nothing_else(
    uow_factory: UnitOfWorkFactory,
) -> None:
    organization_id = await new_governed_org(uow_factory)
    resolver = AuthorityResolver(
        uow_factory, action_floors={ACTION_PUBLISH: BlastRadius.IRREVERSIBLE}
    )

    head = await resolver.resolve(organization_id, "marketing-head")
    assert head.role == HEAD_ROLE
    assert head.department == "marketing"
    assert head.gated_actions() == {ACTION_PUBLISH}
    assert head.for_action(ACTION_PUBLISH).approver_chain == ("director", "operator")
    assert head.for_action(ACTION_PUBLISH).max_escalations == 1


async def test_grants_mirror_the_actor_specs(uow_factory: UnitOfWorkFactory) -> None:
    """`allowed_tools` and `tool_grants` answer different questions — admitted-to
    versus may-now — and disagreement between them is a bug, so they are written from
    the same table."""
    from runtime.org.department import DEPARTMENT_SPECS

    organization_id = await new_governed_org(uow_factory)
    resolver = AuthorityResolver(uow_factory)
    by_name = {s.name: s for s in DEPARTMENT_SPECS}

    for actor, spec in by_name.items():
        authority = await resolver.resolve(organization_id, actor)
        assert authority.tool_grants == spec.allowed_tools, (
            f"{actor}: the spec says {sorted(spec.allowed_tools)} but the grants say "
            f"{sorted(authority.tool_grants)}"
        )


async def test_analytics_holds_nothing(uow_factory: UnitOfWorkFactory) -> None:
    """The control actor. Its whole value to the M1 gate is that it touches nothing,
    so a role-scoped grant that swept it in would quietly invalidate the numbers the
    go/no-go decision is read off."""
    organization_id = await new_governed_org(uow_factory)
    authority = await AuthorityResolver(uow_factory).resolve(organization_id, "analytics")
    assert authority.tool_grants == frozenset()
    assert authority.role == IC_ROLE, "same role as research, different grants"


def test_the_grants_are_actor_scoped_on_purpose() -> None:
    """Role grants would give `analytics` a web fetch it must not have. Pinned so the
    next person to "simplify" this has to read the reason first."""
    assert {g.subject_type for g in GRANTS} == {"actor"}


async def test_seeding_twice_produces_one_of_everything(
    uow_factory: UnitOfWorkFactory,
) -> None:
    from sqlalchemy import text

    from runtime.runtime.governance_boot import seed_governance

    organization_id = await new_governed_org(uow_factory)
    await seed_governance(uow_factory, organization_id)

    async with uow_factory() as uow:
        counts = {
            table: (
                await uow.session.execute(
                    text(f"SELECT count(*) FROM {table} WHERE organization_id = :o"),
                    {"o": organization_id},
                )
            ).scalar_one()
            for table in (
                "roles",
                "authority_policies",
                "tool_grants",
                "connections",
                "rate_limit_policies",
            )
        }
    assert counts == {
        "roles": 4,
        "authority_policies": 1,
        "tool_grants": 4,
        "connections": 2,
        # 2 connection + 2 provider (serper, deepseek) + 4 actor.
        "rate_limit_policies": 8,
    }


async def test_the_role_tree_is_the_escalation_order(uow_factory: UnitOfWorkFactory) -> None:
    from runtime.org.authority import ancestors

    organization_id = await new_governed_org(uow_factory)
    async with uow_factory() as uow:
        roles = {r.name: r for r in await uow.authority.roles(organization_id)}

    assert ancestors(IC_ROLE, roles) == [HEAD_ROLE, DIRECTOR_ROLE, OPERATOR_ROLE]
    assert ancestors(OPERATOR_ROLE, roles) == [], "the buck stops somewhere"


# --- admission through the real door ----------------------------------------------------


async def test_a_run_is_admitted_with_a_frozen_authority_and_a_pool_chain(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """Everything M2 adds, meeting M0's one door.

    The properties that matter are all on the spec: it carries the authority that
    governed it, the leaf of the pool chain it was admitted against, and the priority
    admission used — so "what was this run allowed to do and what could it spend" is
    answerable from `run_specs` alone, months later.
    """
    organization_id = await new_governed_org(uow_factory)
    service = RunService(uow_factory, settings=settings)

    result = await service.start_run(
        StartRunRequest(
            organization_id=organization_id,
            actor_name="marketing-head",
            idempotency_key="m2-admission",
            admission_priority=RunPriority.CRITICAL,
        )
    )
    assert result.status is RunStatus.QUEUED
    assert result.admitted

    async with uow_factory() as uow:
        spec = await service.get_run_spec(uow, result.run_id)
        chain = await uow.budget.chain(BudgetPoolId(uuid.UUID(spec.budget_pool_id)))  # type: ignore[union-attr]
    assert spec is not None
    assert spec.priority is RunPriority.CRITICAL
    assert spec.allocation_id is not None
    assert spec.authority.role == HEAD_ROLE
    assert spec.authority.for_action(ACTION_PUBLISH).level is AuthorityLevel.HUMAN
    assert [p.depth for p in chain] == [0, 1, 2], "org -> department -> actor"
    assert all(p.allocated_live_cents > 0 for p in chain), "intent declared at every level"


async def test_admission_refuses_loudly_when_the_pool_is_empty(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """v3 edge 21 through the real door. The run exists, carries a reason, and is
    announced — because an organization that has quietly stopped working looks exactly
    like one with nothing to do."""
    from sqlalchemy import text

    organization_id = await new_governed_org(uow_factory)
    service = RunService(uow_factory, settings=settings)

    # Admit one run so the chain exists, then take all the money away.
    await service.start_run(
        StartRunRequest(
            organization_id=organization_id, actor_name="research", idempotency_key="warm"
        )
    )
    async with uow_factory.transaction() as uow:
        await uow.session.execute(text("UPDATE budget_pools SET limit_cents = 0"))

    result = await service.start_run(
        StartRunRequest(
            organization_id=organization_id, actor_name="research", idempotency_key="cold"
        )
    )

    assert result.status is RunStatus.LIMIT_REACHED
    assert result.refusal_reason == "POOL_EXHAUSTED"
    async with uow_factory() as uow:
        run = await uow.runs.get(result.run_id)
        events = await uow.outbox.events_for_run(result.run_id)
        held = (
            await uow.session.execute(
                text("SELECT count(*) FROM budget_reservations WHERE run_id = :r"),
                {"r": result.run_id},
            )
        ).scalar_one()
    assert run is not None and run.status == RunStatus.LIMIT_REACHED.value
    assert "POOL_EXHAUSTED" in (run.status_reason or "")
    assert [e["topic"] for e in events] == ["run.refused"]
    assert held == 0, "a refused run holds nothing"


async def test_a_refused_run_is_terminal_and_not_claimable(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """It must not sit in the queue looking like work."""
    from sqlalchemy import text

    organization_id = await new_governed_org(uow_factory)
    service = RunService(uow_factory, settings=settings)
    await service.start_run(
        StartRunRequest(
            organization_id=organization_id, actor_name="research", idempotency_key="warm"
        )
    )
    async with uow_factory.transaction() as uow:
        await uow.session.execute(text("UPDATE budget_pools SET limit_cents = 0"))
    refused = await service.start_run(
        StartRunRequest(
            organization_id=organization_id, actor_name="research", idempotency_key="cold"
        )
    )

    assert RunStatus.LIMIT_REACHED.is_terminal
    async with uow_factory() as uow:
        claimable = await uow.runs.claimable(limit=10)
    assert refused.run_id not in claimable


async def test_the_spec_hash_changes_when_the_policy_does(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """The receipt. A policy edit that left `spec_hash` alone would make the run
    history claim two runs were governed identically when they were not."""
    organization_id = await new_governed_org(uow_factory)
    resolver = AuthorityResolver(uow_factory, ttl_seconds=0.0)
    service = RunService(uow_factory, settings=settings, authority=resolver)

    before = await service.start_run(
        StartRunRequest(
            organization_id=organization_id, actor_name="marketing-head", idempotency_key="a"
        )
    )

    async with uow_factory.transaction() as uow:
        await uow.authority.upsert_policy(
            uuid.uuid4(),
            organization_id,
            scope_type="actor",
            scope_id="marketing-head",
            action="send_internal_note",
            level=AuthorityLevel.AUTO,
            approver_role=None,
            max_escalations=0,
            on_expiry=OnExpiry.DENY,
            ttl_seconds=3600,
        )
    resolver.invalidate(organization_id)

    after = await service.start_run(
        StartRunRequest(
            organization_id=organization_id, actor_name="marketing-head", idempotency_key="b"
        )
    )
    assert before.spec_hash != after.spec_hash


async def test_a_pre_m2_spec_still_loads_and_is_governed(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """M0 and M1 specs have no `authority` field. They must load — and resolve to the
    default, which denies — rather than crashing or being ungoverned."""
    from runtime.domain.specs import RunSpec

    payload = {
        "run_id": str(uuid.uuid4()),
        "organization_id": str(uuid.uuid4()),
        "root_run_id": str(uuid.uuid4()),
        "thread_id": "t",
        "spec_hash": "x" * 64,
        "spec": {
            "actor_id": str(uuid.uuid4()),
            "actor_name": "research",
            "actor_version": 1,
            "kind": "llm_agent",
            "graph_ref": "research@1",
            "handler_ref": None,
            "allowed_tools": ["web.fetch@1"],
            "ceilings": {},
            "model_profiles": {"profiles": {}},
            "allowed_model_call_sites": None,
        },
    }
    spec = RunSpec.model_validate(payload)
    assert spec.spec.authority is None
    assert spec.authority.actor_name == "research"
    assert spec.authority.for_action(ACTION_PUBLISH).level is AuthorityLevel.DENIED
    assert spec.priority is RunPriority.NORMAL
