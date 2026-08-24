"""T55 — the depth-4 deadlock retest, in both pool configurations.

M5 §5 is emphatic about doing this early, *"while M2's reasoning is still fresh"*, and
the reason is that turning the root-run pool on does not merely make the lock chain one
level longer. It changes its shape:

    M2:  three levels, all long-lived. Every reservation *updates* rows that were
         created weeks ago by the first run of the month.
    M5:  four levels, and the fourth is created **per run tree**. A reservation now
         races pool *creation* against pool *update* on the same parent.

That is the shape T27's bug lived in — `budget_pools.parent_id` is a self-referencing
foreign key, so creating a child pool takes `FOR KEY SHARE` on its parent, which
conflicts with `FOR UPDATE` — and it is a shape M2 could only produce by accident.
Here it is the normal case. So the concurrency figure is carried forward from T27
unchanged (200), the overlapping-chain arrangement is carried forward unchanged, and
the whole thing runs **twice**: once with the root pool on and once with it off. Two
runs, comparable numbers, one code path.

`[CHOSEN]` 200-way, matching T27, so the two results are comparable.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text

from runtime.budget.service import (
    BudgetService,
    actor_pool_id,
    current_period_start,
    root_run_pool_id,
)
from runtime.domain.ids import ActorId, BudgetPoolId, OrganizationId, RunId, new_run_id
from runtime.persistence.uow import UnitOfWorkFactory

pytestmark = pytest.mark.integration

RESERVATIONS = 200
"""T27's figure, carried forward unchanged. See the module docstring."""

AMOUNT = 3
DEPARTMENT = "marketing"


async def _org(uow_factory: UnitOfWorkFactory) -> OrganizationId:
    organization_id = OrganizationId(uuid.uuid4())
    async with uow_factory.transaction() as uow:
        await uow.session.execute(
            text("INSERT INTO organizations (id, name) VALUES (:id, :name)"),
            {"id": organization_id, "name": "t55"},
        )
    return organization_id


async def _chain(
    uow_factory: UnitOfWorkFactory,
    organization_id: OrganizationId,
    *,
    actor_id: ActorId,
    root_run_id: RunId | None,
) -> BudgetPoolId:
    async with uow_factory.transaction() as uow:
        return await BudgetService().ensure_chain(
            uow,
            organization_id,
            actor_id=actor_id,
            department=DEPARTMENT,
            org_limit_cents=10_000_000,
            department_limit_cents=10_000_000,
            actor_limit_cents=10_000_000,
            root_run_id=root_run_id,
            root_run_limit_cents=10_000_000,
        )


@pytest.mark.parametrize("root_pool", [False, True], ids=["depth-3", "depth-4"])
async def test_t55_chain_reservation_under_concurrency(
    uow_factory: UnitOfWorkFactory, root_pool: bool
) -> None:
    """200 concurrent reservations over two overlapping chains. Both configurations.

    Three assertions, and they are the three failure modes a broken lock order has:

    *Zero deadlocks* — a `DeadlockDetected` would come back as a failure string, and
    the assertion prints the first three rather than a count, because the exception
    type is the diagnosis.

    *Zero CHECK violations* — I8 is a database constraint, so the thing being ruled out
    is not overspend but a transaction aborting on the constraint instead of being told
    no cleanly. Every level here has ten million cents; nothing should come close.

    *No lost reservations* — exact arithmetic at every level, which is the property the
    row lock actually buys. An implementation with a lock that is merely *usually*
    correct passes the first two and fails this one.
    """
    organization_id = await _org(uow_factory)
    actor_a, actor_b = ActorId(uuid.uuid4()), ActorId(uuid.uuid4())
    root_a, root_b = new_run_id(), new_run_id()

    leaf_a = await _chain(
        uow_factory, organization_id, actor_id=actor_a, root_run_id=root_a if root_pool else None
    )
    leaf_b = await _chain(
        uow_factory, organization_id, actor_id=actor_b, root_run_id=root_b if root_pool else None
    )

    period = current_period_start()
    if root_pool:
        assert leaf_a == root_run_pool_id(organization_id, root_a)
    else:
        assert leaf_a == actor_pool_id(organization_id, actor_a, period)

    service = BudgetService()

    async def reserve(i: int) -> str | None:
        leaf = leaf_a if i % 2 == 0 else leaf_b
        try:
            async with uow_factory.transaction() as uow:
                await service.reserve(uow, pool_id=leaf, run_id=new_run_id(), amount_cents=AMOUNT)
        except Exception as exc:
            return f"{type(exc).__name__}: {exc}"
        return None

    failures = [f for f in await asyncio.gather(*(reserve(i) for i in range(RESERVATIONS))) if f]
    assert failures == [], f"{len(failures)} of {RESERVATIONS} failed: {failures[:3]}"

    async with uow_factory() as uow:
        chain_a = await uow.budget.chain(leaf_a)
        held = (
            await uow.session.execute(
                text("SELECT count(*) FROM budget_reservations WHERE status = 'HELD'")
            )
        ).scalar_one()

    assert held == RESERVATIONS, "no lost reservations"
    assert len(chain_a) == (4 if root_pool else 3), "the chain is as deep as configured"
    # Root first. The org and department saw all 200; the actor pool saw its own half;
    # the root-run pool, when there is one, saw the same half — chain A's reservations
    # all belong to one tree in this arrangement.
    assert chain_a[0].reserved_cents == RESERVATIONS * AMOUNT, "org"
    assert chain_a[1].reserved_cents == RESERVATIONS * AMOUNT, "department"
    assert chain_a[2].reserved_cents == (RESERVATIONS // 2) * AMOUNT, "actor"
    if root_pool:
        assert chain_a[3].reserved_cents == (RESERVATIONS // 2) * AMOUNT, "root run"


@pytest.mark.parametrize("root_pool", [False, True], ids=["depth-3", "depth-4"])
async def test_t55_pool_creation_races_reservation(
    uow_factory: UnitOfWorkFactory, root_pool: bool
) -> None:
    """§5's specific new hazard: *creation* under contention, not just update.

    The test above pre-builds both chains and then hammers them, which is T27's shape.
    This one builds the chain **inside** each concurrent transaction, so half the
    tasks are inserting a pool row under a parent that the other half is locking for
    update. With the root pool on, every single task creates a row — that is what
    "a pool per run tree" means — and it is precisely the interleaving
    `FOR NO KEY UPDATE` exists to survive.

    Fifty tasks over ten trees rather than two hundred over two: the property under
    test is the interleaving of creation and locking, and the number of *distinct
    parents being created under* matters more here than raw volume.
    """
    organization_id = await _org(uow_factory)
    actors = [ActorId(uuid.uuid4()) for _ in range(5)]
    roots = [new_run_id() for _ in range(10)]
    service = BudgetService()

    async def admit(i: int) -> str | None:
        try:
            async with uow_factory.transaction() as uow:
                leaf = await service.ensure_chain(
                    uow,
                    organization_id,
                    actor_id=actors[i % len(actors)],
                    department=DEPARTMENT,
                    org_limit_cents=10_000_000,
                    department_limit_cents=10_000_000,
                    actor_limit_cents=10_000_000,
                    root_run_id=roots[i % len(roots)] if root_pool else None,
                    root_run_limit_cents=10_000_000,
                )
                await service.reserve(uow, pool_id=leaf, run_id=new_run_id(), amount_cents=AMOUNT)
        except Exception as exc:
            return f"{type(exc).__name__}: {exc}"
        return None

    failures = [f for f in await asyncio.gather(*(admit(i) for i in range(50))) if f]
    assert failures == [], f"{len(failures)} of 50 failed: {failures[:3]}"

    async with uow_factory() as uow:
        org_pool = (
            await uow.session.execute(
                text(
                    "SELECT reserved_cents FROM budget_pools "
                    "WHERE organization_id = :org AND scope_type = 'org'"
                ),
                {"org": organization_id},
            )
        ).scalar_one()
        pools = (
            await uow.session.execute(
                text(
                    "SELECT scope_type, count(*) FROM budget_pools "
                    "WHERE organization_id = :org GROUP BY scope_type"
                ),
                {"org": organization_id},
            )
        ).all()

    assert org_pool == 50 * AMOUNT, "every reservation reached the root exactly once"
    by_type = {r.scope_type: r.count for r in pools}
    assert by_type["org"] == 1 and by_type["department"] == 1
    assert by_type["actor"] == len(actors)
    assert by_type.get("root_run", 0) == (len(roots) if root_pool else 0)


def test_the_chain_lock_strength_is_unchanged_by_the_fourth_level() -> None:
    """M5 §5: *"verify M2's chain-lock test still passes and now covers delegation."*

    M2's `test_the_chain_lock_uses_the_documented_order_and_strength` scans the budget
    repository's source for `FOR UPDATE`, because the bug it guards against needs no
    import and would not show up in a dependency graph. That test has caught the same
    class of bug twice now, and M5 adds a **third** place it can be reintroduced:
    `RunRepository.lock_for_fanout`, which locks a `runs` row that a child insert takes
    `FOR KEY SHARE` on through `runs.parent_run_id`. Exactly the same shape, one table
    over.

    So the scan is extended here rather than left implicit. If somebody adds a
    `FOR UPDATE` to either file, one of these two assertions fails and names the file.
    """
    from pathlib import Path

    import runtime.persistence.repositories.budget as budget_repo
    import runtime.persistence.repositories.runs as runs_repo

    for module in (budget_repo, runs_repo):
        source = Path(module.__file__ or "").read_text()
        assert "FOR NO KEY UPDATE" in source, f"{module.__name__} takes no row lock at all"
        # `FOR UPDATE\n` rather than `FOR UPDATE`, matching M2's spelling exactly: the
        # bare substring appears in the prose explaining why it is not used, and a
        # scan that failed on its own docstring would be deleted within a week.
        assert "FOR UPDATE\n" not in source, (
            f"{module.__name__} contains a bare `FOR UPDATE`. It conflicts with the "
            "`FOR KEY SHARE` a foreign-key insert takes on the parent row, which is "
            "the deadlock T27 found and T55 would find again."
        )
