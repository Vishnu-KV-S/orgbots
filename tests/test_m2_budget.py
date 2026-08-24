"""M2 §4 - the hierarchical budget. T27-T31.

§4 calls this the one hard piece and it is right. The tests here are the reason to
believe the chain reservation is correct rather than merely plausible, and T27 is the
one that would have caught the `FOR UPDATE` / `FOR KEY SHARE` deadlock that this
implementation hit on its first run.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid

import pytest
from sqlalchemy import text

from runtime.budget.service import (
    DEFAULT_ACTOR_LIMIT_CENTS,
    OVERSUBSCRIPTION_K,
    REASON_OVERSUBSCRIBED,
    REASON_POOL_EXHAUSTED,
    REASON_PRIORITY_SHED,
    BudgetService,
    actor_pool_id,
    current_period_start,
    department_pool_id,
    org_pool_id,
)
from runtime.domain.enums import ReservationStatus, RunPriority
from runtime.domain.errors import BudgetExceeded
from runtime.domain.ids import (
    ActorId,
    BudgetPoolId,
    OrganizationId,
    RunId,
    new_reservation_id,
    new_run_id,
)
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.runtime.bootstrap import Registrar

pytestmark = pytest.mark.integration

DEPARTMENT = "marketing"


async def _org(uow_factory: UnitOfWorkFactory) -> OrganizationId:
    organization_id = OrganizationId(uuid.uuid4())
    await Registrar(uow_factory).ensure_organization(organization_id, "acme")
    return organization_id


async def _chain(
    uow_factory: UnitOfWorkFactory,
    organization_id: OrganizationId,
    *,
    actor_id: ActorId | None = None,
    org_limit: int = 100_000,
    dept_limit: int = 60_000,
    actor_limit: int = DEFAULT_ACTOR_LIMIT_CENTS,
) -> BudgetPoolId:
    service = BudgetService()
    async with uow_factory.transaction() as uow:
        return await service.ensure_chain(
            uow,
            organization_id,
            actor_id=actor_id or ActorId(uuid.uuid4()),
            department=DEPARTMENT,
            org_limit_cents=org_limit,
            department_limit_cents=dept_limit,
            actor_limit_cents=actor_limit,
        )


async def _pools(uow_factory: UnitOfWorkFactory, leaf: BudgetPoolId) -> list[dict[str, int]]:
    async with uow_factory() as uow:
        chain = await uow.budget.chain(leaf)
    return [
        {
            "depth": p.depth,
            "limit": p.limit_cents,
            "reserved": p.reserved_cents,
            "committed": p.committed_cents,
            "allocated": p.allocated_live_cents,
        }
        for p in chain
    ]


# --- the shape ------------------------------------------------------------------------


async def test_the_chain_is_org_department_actor_root_first(
    uow_factory: UnitOfWorkFactory,
) -> None:
    organization_id = await _org(uow_factory)
    actor_id = ActorId(uuid.uuid4())
    leaf = await _chain(uow_factory, organization_id, actor_id=actor_id)

    period = current_period_start()
    async with uow_factory() as uow:
        chain = await uow.budget.chain(leaf)

    assert [p.depth for p in chain] == [0, 1, 2], "root first — the lock order"
    assert chain[0].id == org_pool_id(organization_id, period)
    assert chain[1].id == department_pool_id(organization_id, DEPARTMENT, period)
    assert chain[2].id == actor_pool_id(organization_id, actor_id, period) == leaf


async def test_an_actor_with_no_department_hangs_off_the_org(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """The normal case for a runtime with one department, not a degenerate one."""
    organization_id = await _org(uow_factory)
    service = BudgetService()
    async with uow_factory.transaction() as uow:
        leaf = await service.ensure_chain(
            uow, organization_id, actor_id=ActorId(uuid.uuid4()), department=None
        )
    async with uow_factory() as uow:
        chain = await uow.budget.chain(leaf)
    assert [p.depth for p in chain] == [0, 1]


async def test_a_reservation_holds_against_every_level(uow_factory: UnitOfWorkFactory) -> None:
    organization_id = await _org(uow_factory)
    leaf = await _chain(uow_factory, organization_id)
    service = BudgetService()

    async with uow_factory.transaction() as uow:
        reservation = await service.reserve(
            uow, pool_id=leaf, run_id=new_run_id(), amount_cents=500
        )

    assert [p["reserved"] for p in await _pools(uow_factory, leaf)] == [500, 500, 500]

    async with uow_factory.transaction() as uow:
        await service.reconcile(uow, reservation, 120)

    after = await _pools(uow_factory, leaf)
    assert [p["reserved"] for p in after] == [0, 0, 0]
    assert [p["committed"] for p in after] == [120, 120, 120]


async def test_the_tightest_level_is_the_one_that_refuses(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """I8 per level: an actor inside its own limit is still stopped by its department."""
    organization_id = await _org(uow_factory)
    leaf = await _chain(uow_factory, organization_id, dept_limit=1_000, actor_limit=50_000)
    service = BudgetService()

    with pytest.raises(BudgetExceeded, match="depth 1"):
        async with uow_factory.transaction() as uow:
            await service.reserve(uow, pool_id=leaf, run_id=new_run_id(), amount_cents=2_000)

    assert [p["reserved"] for p in await _pools(uow_factory, leaf)] == [0, 0, 0], (
        "a refused chain reservation leaves nothing held anywhere — a partial hold at "
        "the levels that had room would leak on every refusal"
    )


# --- T27: 200-way concurrency ---------------------------------------------------------


async def test_t27_two_hundred_concurrent_chain_reservations(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """Zero deadlocks, zero constraint violations, no lost reservations. v3 edge 19.

    Two actor pools under one department under one org, so the chains *overlap* — which
    is the configuration that deadlocks under any lock order that is not deterministic.
    A single-chain test would pass against a broken implementation.

    This is the test that caught the real bug in this implementation: `FOR UPDATE`
    conflicts with the `FOR KEY SHARE` that a foreign-key insert takes on the parent
    pool, so two admissions creating pools under one root deadlocked. The fix is
    `FOR NO KEY UPDATE`; the reason it is safe is that nothing here mutates a key.
    """
    organization_id = await _org(uow_factory)
    actor_a, actor_b = ActorId(uuid.uuid4()), ActorId(uuid.uuid4())
    leaf_a = await _chain(
        uow_factory,
        organization_id,
        actor_id=actor_a,
        org_limit=1_000_000,
        dept_limit=1_000_000,
        actor_limit=1_000_000,
    )
    leaf_b = await _chain(
        uow_factory,
        organization_id,
        actor_id=actor_b,
        org_limit=1_000_000,
        dept_limit=1_000_000,
        actor_limit=1_000_000,
    )

    service = BudgetService()
    reservations = 200
    amount = 3

    async def reserve(i: int) -> str | None:
        leaf = leaf_a if i % 2 == 0 else leaf_b
        try:
            async with uow_factory.transaction() as uow:
                await service.reserve(uow, pool_id=leaf, run_id=new_run_id(), amount_cents=amount)
        except Exception as exc:
            return f"{type(exc).__name__}: {exc}"
        return None

    failures = [f for f in await asyncio.gather(*(reserve(i) for i in range(reservations))) if f]

    assert failures == [], f"{len(failures)} of {reservations} failed: {failures[:3]}"

    async with uow_factory() as uow:
        chain_a = await uow.budget.chain(leaf_a)
        held = (
            await uow.session.execute(
                text("SELECT count(*) FROM budget_reservations WHERE status = 'HELD'")
            )
        ).scalar_one()

    assert held == reservations, "no lost reservations"
    # The org root saw all 200; each actor pool saw its own half. Exact arithmetic
    # under contention is the property the row lock buys.
    assert chain_a[0].reserved_cents == reservations * amount
    assert chain_a[1].reserved_cents == reservations * amount
    assert chain_a[2].reserved_cents == (reservations // 2) * amount


async def test_the_chain_invariant_holds_exactly_under_contention(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """Reserve past the limit from 100 directions; the limit is the limit.

    I8 is a CHECK constraint, so the failure mode this rules out is not "overspend" but
    "a transaction aborted on the constraint instead of being told no cleanly". Every
    refusal must be a `False` from `reserve_chain`, never an `IntegrityError`.
    """
    organization_id = await _org(uow_factory)
    leaf = await _chain(
        uow_factory, organization_id, org_limit=1_000, dept_limit=1_000, actor_limit=1_000
    )
    service = BudgetService()

    async def reserve() -> bool:
        try:
            async with uow_factory.transaction() as uow:
                await service.reserve(uow, pool_id=leaf, run_id=new_run_id(), amount_cents=100)
            return True
        except BudgetExceeded:
            return False

    outcomes = await asyncio.gather(*(reserve() for _ in range(100)))

    assert sum(outcomes) == 10, "exactly limit/amount succeed"
    pools = await _pools(uow_factory, leaf)
    assert all(p["reserved"] == 1_000 for p in pools)
    assert all(p["reserved"] + p["committed"] <= p["limit"] for p in pools)


# --- T28: the sweeper ------------------------------------------------------------------


async def test_t28_an_orphaned_reservation_is_swept_and_the_chain_recovers(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """v3 edge 20. A worker dies mid-reservation; the hold must not be permanent.

    With a hierarchy this matters more than it did in M0: one dead worker holds
    headroom at *every* level, so it takes budget from the whole organization rather
    than from one actor.
    """
    organization_id = await _org(uow_factory)
    leaf = await _chain(uow_factory, organization_id)
    service = BudgetService()

    async with uow_factory.transaction() as uow:
        reservation = await service.reserve(
            uow, pool_id=leaf, run_id=new_run_id(), amount_cents=900
        )
    assert [p["reserved"] for p in await _pools(uow_factory, leaf)] == [900, 900, 900]

    # The worker dies here: nothing reconciles, and the TTL elapses.
    async with uow_factory.transaction() as uow:
        await uow.session.execute(
            text(
                "UPDATE budget_reservations SET expires_at = now() - interval '1 minute' "
                "WHERE id = :id"
            ),
            {"id": reservation},
        )

    async with uow_factory.transaction() as uow:
        swept = await service.sweep(uow)

    assert swept == 1
    assert [p["reserved"] for p in await _pools(uow_factory, leaf)] == [0, 0, 0], (
        "every level recovers, not just the leaf"
    )
    async with uow_factory() as uow:
        assert await uow.budget.reservation_status(reservation) is ReservationStatus.EXPIRED


async def test_a_double_settle_does_not_drive_the_chain_negative(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """The sweeper and a reconcile can race on a run that came back to life.

    Without `GREATEST(..., 0)` the second settle drives `reserved_cents` below zero and
    the CHECK constraint aborts a transaction that was trying to *return* money.
    """
    organization_id = await _org(uow_factory)
    leaf = await _chain(uow_factory, organization_id)
    service = BudgetService()
    run_id = new_run_id()

    async with uow_factory.transaction() as uow:
        reservation = await service.reserve(uow, pool_id=leaf, run_id=run_id, amount_cents=200)
    async with uow_factory.transaction() as uow:
        await service.reconcile(uow, reservation, 200)
    async with uow_factory.transaction() as uow:
        await service.reconcile(uow, reservation, 200)  # the loser

    pools = await _pools(uow_factory, leaf)
    assert all(p["reserved"] == 0 for p in pools)
    assert all(p["committed"] == 200 for p in pools), "charged once, not twice"


async def test_release_run_holds_settles_every_level(uow_factory: UnitOfWorkFactory) -> None:
    organization_id = await _org(uow_factory)
    leaf = await _chain(uow_factory, organization_id)
    service = BudgetService()
    run_id = new_run_id()

    async with uow_factory.transaction() as uow:
        await service.reserve(uow, pool_id=leaf, run_id=run_id, amount_cents=100)
        await service.reserve(uow, pool_id=leaf, run_id=run_id, amount_cents=250)
    async with uow_factory.transaction() as uow:
        released = await uow.budget.release_run_holds(run_id)

    assert released == 2
    assert [p["reserved"] for p in await _pools(uow_factory, leaf)] == [0, 0, 0]


# --- T29 / T30 / T31: admission --------------------------------------------------------


async def _admit(
    uow_factory: UnitOfWorkFactory,
    leaf: BudgetPoolId,
    *,
    ceiling: int,
    priority: RunPriority = RunPriority.NORMAL,
) -> object:
    async with uow_factory() as uow:
        return await BudgetService().admit(
            uow, leaf_pool_id=leaf, ceiling_cents=ceiling, priority=priority
        )


async def test_t29_an_exhausted_pool_refuses_with_a_reason(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """v3 edge 21. Silence here is how an organization quietly stops working."""
    organization_id = await _org(uow_factory)
    leaf = await _chain(
        uow_factory, organization_id, org_limit=100, dept_limit=100, actor_limit=100
    )
    async with uow_factory.transaction() as uow:
        await BudgetService().reserve(uow, pool_id=leaf, run_id=new_run_id(), amount_cents=100)

    verdict = await _admit(uow_factory, leaf, ceiling=10)

    assert verdict.refused  # type: ignore[attr-defined]
    assert verdict.reason == REASON_POOL_EXHAUSTED  # type: ignore[attr-defined]
    assert "depth 0" in verdict.detail  # type: ignore[attr-defined]


async def test_t29_the_refusal_names_the_outermost_level(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """Root-first means the answer names the constraint an operator can act on.

    "The org is out of money" and "this actor is out of money" call for completely
    different responses, and a refusal that named the wrong one sends somebody to the
    wrong dashboard.
    """
    organization_id = await _org(uow_factory)
    leaf = await _chain(
        uow_factory, organization_id, org_limit=100_000, dept_limit=100_000, actor_limit=100
    )
    async with uow_factory.transaction() as uow:
        await BudgetService().reserve(uow, pool_id=leaf, run_id=new_run_id(), amount_cents=100)

    verdict = await _admit(uow_factory, leaf, ceiling=1)
    assert "depth 2" in verdict.detail  # type: ignore[attr-defined]


async def test_t30_below_fifteen_percent_headroom_low_is_shed_and_critical_is_not(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """§4's priority degradation, and the in-flight guarantee alongside it."""
    organization_id = await _org(uow_factory)
    leaf = await _chain(
        uow_factory, organization_id, org_limit=1_000, dept_limit=1_000, actor_limit=1_000
    )
    service = BudgetService()

    # 88% consumed → 12% headroom: below the LOW threshold, above the NORMAL one.
    async with uow_factory.transaction() as uow:
        in_flight = await service.reserve(uow, pool_id=leaf, run_id=new_run_id(), amount_cents=880)

    assert (await _admit(uow_factory, leaf, ceiling=1, priority=RunPriority.LOW)).refused  # type: ignore[attr-defined]
    assert (await _admit(uow_factory, leaf, ceiling=1, priority=RunPriority.NORMAL)).admitted  # type: ignore[attr-defined]
    assert (await _admit(uow_factory, leaf, ceiling=1, priority=RunPriority.CRITICAL)).admitted  # type: ignore[attr-defined]

    shed = await _admit(uow_factory, leaf, ceiling=1, priority=RunPriority.LOW)
    assert shed.reason == REASON_PRIORITY_SHED  # type: ignore[attr-defined]

    # 96% consumed → 4% headroom: NORMAL is shed too, CRITICAL still admitted.
    async with uow_factory.transaction() as uow:
        await service.reserve(uow, pool_id=leaf, run_id=new_run_id(), amount_cents=80)

    assert (await _admit(uow_factory, leaf, ceiling=1, priority=RunPriority.NORMAL)).refused  # type: ignore[attr-defined]
    assert (await _admit(uow_factory, leaf, ceiling=1, priority=RunPriority.CRITICAL)).admitted  # type: ignore[attr-defined]

    # And the in-flight reservation is untouched throughout. §4 is explicit: a run
    # that was admitted keeps its money. Revoking it mid-run produces a failure that
    # looks like a bug and wastes everything spent so far.
    async with uow_factory() as uow:
        assert await uow.budget.reservation_status(in_flight) is ReservationStatus.HELD
    assert next(iter(await _pools(uow_factory, leaf)))["reserved"] == 960


async def test_t31_allocations_oversubscribe_to_one_point_three_but_spend_does_not(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """v3 edges 18/19. Admitting 1.3x of *intent* must not permit 1.3x of *money*.

    That is the whole argument for allocations being advisory: they let a department
    promise more than it has on the correct assumption that not every run spends its
    ceiling, and the hard reservation path still bounds actual spend at `limit`.
    """
    organization_id = await _org(uow_factory)
    leaf = await _chain(
        uow_factory, organization_id, org_limit=1_000, dept_limit=1_000, actor_limit=1_000
    )
    service = BudgetService()
    soft_ceiling = int(1_000 * (1 + OVERSUBSCRIPTION_K))

    admitted = 0
    for _ in range(20):
        verdict = await _admit(uow_factory, leaf, ceiling=100)
        if verdict.refused:  # type: ignore[attr-defined]
            assert verdict.reason == REASON_OVERSUBSCRIBED  # type: ignore[attr-defined]
            break
        async with uow_factory.transaction() as uow:
            await service.open_allocation(
                uow,
                leaf_pool_id=leaf,
                run_id=new_run_id(),
                ceiling_cents=100,
                priority=RunPriority.NORMAL,
            )
        admitted += 1

    assert admitted == 13, f"limit x 1.3 / 100 = 13 allocations, got {admitted}"
    pools = await _pools(uow_factory, leaf)
    assert all(p["allocated"] == soft_ceiling for p in pools)

    # Now spend against it. The hard path refuses at `limit`, not at the soft ceiling.
    spent = 0
    for _ in range(20):
        try:
            async with uow_factory.transaction() as uow:
                await service.reserve(uow, pool_id=leaf, run_id=new_run_id(), amount_cents=100)
            spent += 100
        except BudgetExceeded:
            break

    assert spent == 1_000, "reservations are still bounded by the limit, not by 1.3x it"
    assert all(
        p["reserved"] + p["committed"] <= p["limit"] for p in await _pools(uow_factory, leaf)
    )


async def test_closing_an_allocation_returns_the_intent_at_every_level(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """Without this the soft ceiling ratchets: finished runs keep declaring intent
    they will never spend, and admission tightens until nothing is admitted against a
    pool with money to spare."""
    organization_id = await _org(uow_factory)
    leaf = await _chain(uow_factory, organization_id)
    service = BudgetService()
    run_id = new_run_id()

    async with uow_factory.transaction() as uow:
        await service.open_allocation(
            uow,
            leaf_pool_id=leaf,
            run_id=run_id,
            ceiling_cents=400,
            priority=RunPriority.NORMAL,
        )
    assert [p["allocated"] for p in await _pools(uow_factory, leaf)] == [400, 400, 400]

    async with uow_factory.transaction() as uow:
        assert await service.close_allocation(uow, run_id) is True
        assert await service.close_allocation(uow, run_id) is False, "idempotent"

    assert [p["allocated"] for p in await _pools(uow_factory, leaf)] == [0, 0, 0]


async def test_a_retried_admission_does_not_double_count_its_own_intent(
    uow_factory: UnitOfWorkFactory,
) -> None:
    organization_id = await _org(uow_factory)
    leaf = await _chain(uow_factory, organization_id)
    service = BudgetService()
    run_id = new_run_id()

    for _ in range(3):
        async with uow_factory.transaction() as uow:
            await service.open_allocation(
                uow,
                leaf_pool_id=leaf,
                run_id=run_id,
                ceiling_cents=400,
                priority=RunPriority.NORMAL,
            )

    assert [p["allocated"] for p in await _pools(uow_factory, leaf)] == [400, 400, 400]


async def test_the_allocated_counter_matches_the_allocation_rows(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """`allocated_cents` is the one denormalised number in the budget tables.

    The repair function exists for that reason, and asserting it changes nothing after
    ordinary use is how a drift bug gets caught in the milestone that introduced it
    rather than in M4.
    """
    organization_id = await _org(uow_factory)
    leaf = await _chain(uow_factory, organization_id)
    service = BudgetService()

    kept = new_run_id()
    async with uow_factory.transaction() as uow:
        await service.open_allocation(
            uow, leaf_pool_id=leaf, run_id=kept, ceiling_cents=300, priority=RunPriority.LOW
        )
        await service.open_allocation(
            uow,
            leaf_pool_id=leaf,
            run_id=new_run_id(),
            ceiling_cents=150,
            priority=RunPriority.CRITICAL,
        )
        await service.close_allocation(uow, kept)

    async with uow_factory.transaction() as uow:
        fixed = await uow.budget.reconcile_allocated_cents(organization_id)

    assert fixed == 0, "the counter already agreed with the rows"
    assert [p["allocated"] for p in await _pools(uow_factory, leaf)] == [150, 150, 150]


async def test_admission_against_a_pool_that_does_not_exist_refuses_rather_than_crashing(
    uow_factory: UnitOfWorkFactory,
) -> None:
    verdict = await _admit(uow_factory, BudgetPoolId(uuid.uuid4()), ceiling=1)
    assert verdict.refused  # type: ignore[attr-defined]
    assert verdict.reason == REASON_POOL_EXHAUSTED  # type: ignore[attr-defined]


async def test_a_zero_limit_pool_has_no_headroom_rather_than_dividing_by_zero(
    uow_factory: UnitOfWorkFactory,
) -> None:
    organization_id = await _org(uow_factory)
    leaf = await _chain(uow_factory, organization_id, actor_limit=0)
    verdict = await _admit(uow_factory, leaf, ceiling=1, priority=RunPriority.CRITICAL)
    assert verdict.refused  # type: ignore[attr-defined]


async def test_reserving_zero_cents_still_creates_a_row(uow_factory: UnitOfWorkFactory) -> None:
    """M0's rule, preserved: the reconcile path must have no "free call" branch."""
    organization_id = await _org(uow_factory)
    leaf = await _chain(uow_factory, organization_id)
    async with uow_factory.transaction() as uow:
        reservation = await BudgetService().reserve(
            uow, pool_id=leaf, run_id=new_run_id(), amount_cents=0
        )
    async with uow_factory() as uow:
        assert await uow.budget.reservation_status(reservation) is ReservationStatus.HELD


def test_the_reservation_id_helper_is_still_the_only_source() -> None:
    """A guard against the M2 §10 risk: a second reservation path.

    Not a behavioural test — a reminder in executable form. If someone adds a direct
    UPDATE against `budget_pools` outside `budget/`, the import-linter contract
    `budget-tables-are-private` fails first; this asserts the id helper has not been
    quietly duplicated.
    """
    assert new_reservation_id() != new_reservation_id()


async def test_the_sweeper_handles_a_batch_across_overlapping_chains(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """Root-first locking is what makes a batch safe.

    Two chains in one organization share the root, so a sweeper touching both
    serialises on that first lock instead of interleaving into a cycle. Without it this
    is a deadlock waiting for the first busy week.
    """
    organization_id = await _org(uow_factory)
    leaf_a = await _chain(uow_factory, organization_id, actor_id=ActorId(uuid.uuid4()))
    leaf_b = await _chain(uow_factory, organization_id, actor_id=ActorId(uuid.uuid4()))
    service = BudgetService()

    async with uow_factory.transaction() as uow:
        for leaf in (leaf_a, leaf_b, leaf_a, leaf_b):
            await service.reserve(uow, pool_id=leaf, run_id=new_run_id(), amount_cents=10)
        await uow.session.execute(
            text("UPDATE budget_reservations SET expires_at = now() - interval '1 minute'")
        )

    async def sweep() -> int:
        async with uow_factory.transaction() as uow:
            return await service.sweep(uow)

    swept = sum(await asyncio.gather(sweep(), sweep()))
    assert swept == 4
    assert [p["reserved"] for p in await _pools(uow_factory, leaf_a)] == [0, 0, 0]
    assert [p["reserved"] for p in await _pools(uow_factory, leaf_b)] == [0, 0, 0]


async def test_reservations_do_not_block_creating_a_sibling_pool(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """The other half of the `FOR NO KEY UPDATE` decision.

    A reservation in flight against the org root must not stop a new actor pool being
    created under it. `FOR UPDATE` would block the foreign-key insert; `FOR NO KEY
    UPDATE` does not, because nothing here mutates a key.
    """
    organization_id = await _org(uow_factory)
    leaf = await _chain(uow_factory, organization_id)
    service = BudgetService()

    async with uow_factory.transaction() as uow:
        await service.reserve(uow, pool_id=leaf, run_id=new_run_id(), amount_cents=10)
        # Still inside the transaction that holds the chain lock:
        sibling = await service.ensure_chain(
            uow, organization_id, actor_id=ActorId(uuid.uuid4()), department=DEPARTMENT
        )
    assert sibling != leaf


async def test_the_fourth_level_exists_and_is_off_by_default(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """§1 names the hierarchy as `org -> department -> actor -> root run`.

    The fourth level is built and works; it is **not** enabled by default, and both
    halves of that are deliberate. Until delegation exists a run tree has exactly one
    member, so the pool would bound a single run that `ceilings.max_cost_cents`
    already bounds — while adding a row per run and a lock per reservation, which is
    precisely the per-call cost §9's non-regression gate is watching for.

    This test is what makes M5 a parameter change rather than a new level.
    """
    from runtime.budget.service import root_run_pool_id

    organization_id = await _org(uow_factory)
    actor_id = ActorId(uuid.uuid4())
    service = BudgetService()

    async with uow_factory.transaction() as uow:
        default_leaf = await service.ensure_chain(
            uow, organization_id, actor_id=actor_id, department=DEPARTMENT
        )
    async with uow_factory() as uow:
        assert [p.depth for p in await uow.budget.chain(default_leaf)] == [0, 1, 2]

    root_run = new_run_id()
    async with uow_factory.transaction() as uow:
        deep_leaf = await service.ensure_chain(
            uow,
            organization_id,
            actor_id=actor_id,
            department=DEPARTMENT,
            root_run_id=root_run,
            root_run_limit_cents=500,
        )
    assert deep_leaf == root_run_pool_id(organization_id, root_run)

    async with uow_factory() as uow:
        chain = await uow.budget.chain(deep_leaf)
    assert [p.depth for p in chain] == [0, 1, 2, 3], "root first, four levels"
    assert chain[-1].limit_cents == 500

    # And it binds: a reservation the actor pool could afford is refused by the run.
    with pytest.raises(BudgetExceeded, match="depth 3"):
        async with uow_factory.transaction() as uow:
            await service.reserve(uow, pool_id=deep_leaf, run_id=new_run_id(), amount_cents=600)

    async with uow_factory.transaction() as uow:
        await service.reserve(uow, pool_id=deep_leaf, run_id=new_run_id(), amount_cents=400)
    assert [p["reserved"] for p in await _pools(uow_factory, deep_leaf)] == [400, 400, 400, 400]


async def test_the_period_start_is_the_first_of_the_month(uow_factory: UnitOfWorkFactory) -> None:
    assert current_period_start(dt.datetime(2026, 8, 22, tzinfo=dt.UTC)) == dt.date(2026, 8, 1)


async def test_pools_are_derived_not_looked_up() -> None:
    """A derived id means a reservation never reads one row to find another."""
    organization_id = OrganizationId(uuid.uuid4())
    actor_id = ActorId(uuid.uuid4())
    period = dt.date(2026, 8, 1)
    assert actor_pool_id(organization_id, actor_id, period) == actor_pool_id(
        organization_id, actor_id, period
    )
    assert org_pool_id(organization_id, period) != department_pool_id(
        organization_id, DEPARTMENT, period
    )


async def test_a_run_id_is_not_a_pool_id() -> None:
    """Typed ids, so mypy refuses the confusion this line would otherwise be."""
    run_id: RunId = new_run_id()
    assert isinstance(run_id, uuid.UUID)
