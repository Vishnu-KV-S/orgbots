"""T11 — the budget invariant holds under concurrency.

I8 is `committed + reserved <= limit`. The database enforces it with a CHECK
constraint, so the question this test actually answers is not "can we overspend"
— we cannot — but "does the application handle the contention without losing
reservations, double-charging, or turning a full pool into an exception storm".

200 concurrent reservations against a pool sized for 100 is the shape that finds
read-then-write bugs: every caller reads the same headroom, every caller decides
it fits.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid

import pytest
from sqlalchemy import text

from runtime.budget.service import BudgetService
from runtime.domain.errors import BudgetExceeded
from runtime.domain.ids import BudgetPoolId, OrganizationId, RunId, new_budget_pool_id
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.settings import Settings

pytestmark = pytest.mark.integration

UNIT = 10
"""Cents per reservation. A pool sized for 100 of them is 1000 cents."""


async def _pool(
    uow_factory: UnitOfWorkFactory, organization_id: OrganizationId, limit_cents: int
) -> BudgetPoolId:
    pool_id = new_budget_pool_id()
    async with uow_factory.transaction() as uow:
        return await uow.budget.ensure_pool(
            pool_id,
            organization_id,
            scope_type="org",
            scope_id=organization_id,
            period="month",
            period_start=dt.date.today().replace(day=1),
            limit_cents=limit_cents,
        )


async def _reserve(
    uow_factory: UnitOfWorkFactory, service: BudgetService, pool_id: BudgetPoolId
) -> bool:
    try:
        async with uow_factory.transaction() as uow:
            await service.reserve(
                uow, pool_id=pool_id, run_id=RunId(uuid.uuid4()), amount_cents=UNIT
            )
    except BudgetExceeded:
        return False
    return True


async def test_200_concurrent_reservations_on_a_pool_sized_for_100(
    uow_factory: UnitOfWorkFactory, organization_id: OrganizationId, settings: Settings
) -> None:
    """T11."""
    pool_id = await _pool(uow_factory, organization_id, UNIT * 100)
    service = BudgetService(reservation_ttl_seconds=settings.reservation_ttl_seconds)

    results = await asyncio.gather(
        *(_reserve(uow_factory, service, pool_id) for _ in range(200)),
        return_exceptions=True,
    )

    unexpected = [r for r in results if isinstance(r, BaseException)]
    assert unexpected == [], (
        f"reservation raised something other than BudgetExceeded: {unexpected[:3]}"
    )

    granted = sum(1 for r in results if r is True)
    assert granted == 100, f"{granted} reservations granted against a pool sized for 100"

    async with uow_factory() as uow:
        state = await uow.budget.pool_state(pool_id)
        held = (
            await uow.session.execute(
                text(
                    "SELECT count(*), COALESCE(sum(amount_cents), 0) FROM budget_reservations "
                    "WHERE pool_id = :p AND status = 'HELD'"
                ),
                {"p": pool_id},
            )
        ).one()
    assert state is not None
    assert state.reserved_cents == UNIT * 100
    assert state.available_cents == 0
    assert (held[0], held[1]) == (100, UNIT * 100), "a reservation row went missing"


async def test_the_database_refuses_an_overspend_even_if_the_code_asks_for_one(
    uow_factory: UnitOfWorkFactory, organization_id: OrganizationId
) -> None:
    """The CHECK constraint is the backstop. If application logic ever regresses,
    this is what turns it into a failed transaction rather than a bill."""
    from sqlalchemy.exc import IntegrityError

    pool_id = await _pool(uow_factory, organization_id, 100)
    with pytest.raises(IntegrityError, match="ck_budget_invariant"):
        async with uow_factory.transaction() as uow:
            await uow.session.execute(
                text("UPDATE budget_pools SET committed_cents = 101 WHERE id = :p"),
                {"p": pool_id},
            )


async def test_reconcile_moves_a_hold_to_a_charge(
    uow_factory: UnitOfWorkFactory, organization_id: OrganizationId, settings: Settings
) -> None:
    pool_id = await _pool(uow_factory, organization_id, 1000)
    service = BudgetService()
    run_id = RunId(uuid.uuid4())

    async with uow_factory.transaction() as uow:
        reservation = await service.reserve(uow, pool_id=pool_id, run_id=run_id, amount_cents=500)
    async with uow_factory.transaction() as uow:
        await service.reconcile(uow, reservation, 120)

    async with uow_factory() as uow:
        state = await uow.budget.pool_state(pool_id)
        status = await uow.budget.reservation_status(reservation)
    assert state is not None
    assert (state.reserved_cents, state.committed_cents) == (0, 120)
    assert status is not None and status.value == "RELEASED"


async def test_reconcile_is_idempotent(
    uow_factory: UnitOfWorkFactory, organization_id: OrganizationId
) -> None:
    """A retried reconcile must not charge twice. It is the operation most likely
    to be retried, because it runs after the thing that might have crashed."""
    pool_id = await _pool(uow_factory, organization_id, 1000)
    service = BudgetService()

    async with uow_factory.transaction() as uow:
        reservation = await service.reserve(
            uow, pool_id=pool_id, run_id=RunId(uuid.uuid4()), amount_cents=500
        )
    for _ in range(3):
        async with uow_factory.transaction() as uow:
            await service.reconcile(uow, reservation, 120)

    async with uow_factory() as uow:
        state = await uow.budget.pool_state(pool_id)
    assert state is not None
    assert (state.reserved_cents, state.committed_cents) == (0, 120)


async def test_the_sweeper_reclaims_holds_whose_worker_died(
    uow_factory: UnitOfWorkFactory, organization_id: OrganizationId
) -> None:
    """Without this, every crashed run permanently shrinks the pool."""
    pool_id = await _pool(uow_factory, organization_id, 1000)
    service = BudgetService(reservation_ttl_seconds=-1)  # already expired on creation

    async with uow_factory.transaction() as uow:
        await service.reserve(uow, pool_id=pool_id, run_id=RunId(uuid.uuid4()), amount_cents=800)
    async with uow_factory() as uow:
        before = await uow.budget.pool_state(pool_id)
    assert before is not None and before.reserved_cents == 800

    async with uow_factory.transaction() as uow:
        swept = await service.sweep(uow)
    assert swept == 1

    async with uow_factory() as uow:
        after = await uow.budget.pool_state(pool_id)
    assert after is not None
    assert after.reserved_cents == 0
    assert after.available_cents == 1000


async def test_a_full_pool_reports_what_is_available(
    uow_factory: UnitOfWorkFactory, organization_id: OrganizationId
) -> None:
    pool_id = await _pool(uow_factory, organization_id, 100)
    service = BudgetService()
    async with uow_factory.transaction() as uow:
        await service.reserve(uow, pool_id=pool_id, run_id=RunId(uuid.uuid4()), amount_cents=90)

    with pytest.raises(BudgetExceeded, match="10 cents available, 50 requested"):
        async with uow_factory.transaction() as uow:
            await service.reserve(uow, pool_id=pool_id, run_id=RunId(uuid.uuid4()), amount_cents=50)


async def test_a_zero_cent_reservation_still_creates_a_row(
    uow_factory: UnitOfWorkFactory, organization_id: OrganizationId
) -> None:
    """So the reconcile path has no "free call" branch that could skip recording
    usage."""
    pool_id = await _pool(uow_factory, organization_id, 100)
    service = BudgetService()
    async with uow_factory.transaction() as uow:
        reservation = await service.reserve(
            uow, pool_id=pool_id, run_id=RunId(uuid.uuid4()), amount_cents=0
        )
    async with uow_factory() as uow:
        assert await uow.budget.reservation_status(reservation) is not None
