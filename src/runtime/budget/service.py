"""Budget service.

M0 shipped one pool per org against the v3 sequencing that puts budgets in M2. The
reason was that I8 is an invariant, and retrofitting reservation points into every
gateway call path later means touching every call site under concurrency. That bet
pays off here: M2 adds the hierarchy, allocations and priority admission on top of a
reservation mechanism that already holds, and no call site moves.

**The hierarchy** is org → department → actor, three levels, and a reservation holds
against every level from the run's leaf to the root simultaneously. The deadlock
argument lives in `BudgetRepository.reserve_chain`, which is the single reservation
path — M2 §10 names bypassing it as the risk, and the import-linter contract
`budget-tables-are-private` is what enforces the ban mechanically rather than by
review.

**Admission** (§4, v3 §8) is two checks at `start_run()`:

    hard:  committed + reserved ≤ limit                        at every level
    soft:  committed + reserved + Σ live allocations
             ≤ limit x (1 + K)          K = 0.30               at every level

The soft one is what lets a department promise more than it has, on the correct
assumption that not every run spends its ceiling. It is safe only because
allocations are advisory: the hard reservation path still bounds actual spend by
`limit`, so 1.3x of *intent* cannot become 1.3x of *money*.

**Priority degradation.** As headroom falls, `LOW` is refused first, then `NORMAL`,
and only `CRITICAL` is admitted. **In-flight reservations are never revoked** — a run
that was admitted keeps its money, because taking it back mid-run produces a failure
that looks like a bug and wastes everything spent so far.

**Refusal is loud.** A refused run is created in `LIMIT_REACHED` with reason
`POOL_EXHAUSTED` and an event is emitted. Silence here is how an organization quietly
stops working (edge case 21), and a system that stops working silently is
indistinguishable from one with nothing to do.

Every method takes the caller's `UnitOfWork`. Transaction control belongs to the
caller — `start_run()` needs the reservation to be part of its one transaction, and a
gateway needs it to be part of nothing at all.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Mapping
from dataclasses import dataclass

from runtime.domain.enums import RunPriority
from runtime.domain.errors import BudgetExceeded
from runtime.domain.ids import (
    ActorId,
    BudgetPoolId,
    OrganizationId,
    ReservationId,
    RunId,
    new_reservation_id,
)
from runtime.persistence.repositories.budget import PoolState
from runtime.persistence.uow import UnitOfWork

DEFAULT_ORG_LIMIT_CENTS = 100_000
"""$1000/month at the root."""

DEFAULT_ROOT_RUN_LIMIT_CENTS = 2_000
"""`[CHOSEN]` M5. What one run *tree* may spend, when the fourth pool level is on.

$20 against an actor's $250 monthly. It is deliberately small: the level exists so a
parent that decomposes badly cannot spend its actor's whole month in one afternoon
(§9 risk 1), and a ceiling set near the actor limit would bound nothing while still
costing a row and a lock per reservation. Set your own, in advance, and set it from the
first week's `cost per accepted outcome` rather than from this number.

Not period-scoped, unlike every pool above it — see `root_run_pool_id`."""

DEFAULT_DEPARTMENT_LIMIT_CENTS = 60_000
DEFAULT_ACTOR_LIMIT_CENTS = 25_000
"""Children may sum to more than their parent — that is the point of a hierarchy
rather than a partition. Four actors at $250 under a $600 department under a $1000
org: any one of them can have a heavy week, and all four cannot."""

OVERSUBSCRIPTION_K = 0.30
"""v3 §8. Allocations may exceed the limit by 30%, because allocations are intent
and most runs do not spend their ceiling. Measured, not guessed at, is the right way
to set this — and the number to revisit first if admission starts refusing runs the
pool could actually have afforded."""

HEADROOM_SHED_LOW = 0.15
HEADROOM_SHED_NORMAL = 0.05
"""§4: refuse LOW below 15% headroom, then NORMAL. `CRITICAL` is admitted while any
headroom remains at all — below that the hard check refuses it like anything else."""

REASON_POOL_EXHAUSTED = "POOL_EXHAUSTED"
REASON_PRIORITY_SHED = "PRIORITY_SHED"
REASON_OVERSUBSCRIBED = "ALLOCATION_CEILING"


def org_pool_id(organization_id: OrganizationId, period_start: dt.date) -> BudgetPoolId:
    """Derive the pool ID rather than storing a pointer to it.

    A derived ID means "the pool for this org this month" is computable without a
    lookup, so a reservation never has to read one row to find another.
    """
    name = f"pool:org:{organization_id}:month:{period_start.isoformat()}"
    return BudgetPoolId(uuid.uuid5(uuid.NAMESPACE_URL, name))


def department_pool_id(
    organization_id: OrganizationId, department: str, period_start: dt.date
) -> BudgetPoolId:
    name = f"pool:dept:{organization_id}:{department}:month:{period_start.isoformat()}"
    return BudgetPoolId(uuid.uuid5(uuid.NAMESPACE_URL, name))


def actor_pool_id(
    organization_id: OrganizationId, actor_id: ActorId, period_start: dt.date
) -> BudgetPoolId:
    name = f"pool:actor:{organization_id}:{actor_id}:month:{period_start.isoformat()}"
    return BudgetPoolId(uuid.uuid5(uuid.NAMESPACE_URL, name))


def root_run_pool_id(organization_id: OrganizationId, root_run_id: RunId) -> BudgetPoolId:
    """The fourth level: one pool for a whole run tree.

    §1 names the hierarchy as `org -> department -> actor -> root run`, and this is
    that last level. It is **off by default** — see `ensure_chain`'s `root_run_id`
    parameter — because until delegation exists a run tree has exactly one member, so
    the pool would bound a single run that `ceilings.max_cost_cents` already bounds,
    while adding a row and a lock to every reservation. §9 measures that.

    It becomes load-bearing in M5, when a parent's children spend against the parent's
    money and the thing that has to be capped is the *tree* rather than any one run in
    it. The mechanism is here and tested so that turning it on is a parameter.

    Not period-scoped, unlike every pool above it: a run tree is not a calendar month,
    and giving it a period would make "which pool did last Tuesday's run use" depend on
    when you asked.
    """
    name = f"pool:root_run:{organization_id}:{root_run_id}"
    return BudgetPoolId(uuid.uuid5(uuid.NAMESPACE_URL, name))


def department_scope_id(organization_id: OrganizationId, department: str) -> uuid.UUID:
    """`budget_pools.scope_id` is a uuid, and a department is a name. Derived rather
    than given a table of its own, because a department is not an entity in M2 — it is
    a string on an actor row, and inventing a table for it would be M4 arriving early."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"department:{organization_id}:{department}")


def current_period_start(now: dt.datetime | None = None) -> dt.date:
    today = (now or dt.datetime.now(dt.UTC)).date()
    return today.replace(day=1)


@dataclass(frozen=True, slots=True)
class AdmissionVerdict:
    """Whether a run may start, and — if not — which level said no and why.

    The level matters. "The org is out of money" and "this actor is out of money" call
    for completely different responses, and a refusal that does not say which one it
    was sends somebody to look at the wrong dashboard.
    """

    admitted: bool
    reason: str | None = None
    pool_id: BudgetPoolId | None = None
    detail: str = ""
    headroom: float = 1.0

    @property
    def refused(self) -> bool:
        return not self.admitted


class BudgetService:
    def __init__(self, *, reservation_ttl_seconds: float = 300.0) -> None:
        self._ttl = reservation_ttl_seconds

    # --- the pool tree ----------------------------------------------------------------

    async def ensure_org_pool(
        self,
        uow: UnitOfWork,
        organization_id: OrganizationId,
        *,
        limit_cents: int = DEFAULT_ORG_LIMIT_CENTS,
        now: dt.datetime | None = None,
    ) -> BudgetPoolId:
        period_start = current_period_start(now)
        return await uow.budget.ensure_pool(
            org_pool_id(organization_id, period_start),
            organization_id,
            scope_type="org",
            scope_id=organization_id,
            period="month",
            period_start=period_start,
            limit_cents=limit_cents,
            parent_id=None,
            depth=0,
        )

    async def ensure_chain(
        self,
        uow: UnitOfWork,
        organization_id: OrganizationId,
        *,
        actor_id: ActorId,
        department: str | None,
        org_limit_cents: int = DEFAULT_ORG_LIMIT_CENTS,
        department_limit_cents: int = DEFAULT_DEPARTMENT_LIMIT_CENTS,
        actor_limit_cents: int = DEFAULT_ACTOR_LIMIT_CENTS,
        root_run_id: RunId | None = None,
        root_run_limit_cents: int | None = None,
        now: dt.datetime | None = None,
    ) -> BudgetPoolId:
        """Ensure org → [department] → actor → [root run] exists; return the **leaf**.

        Two levels are optional and for opposite reasons.

        *Department* is absent when the actor has none. That is not a degenerate case
        to tolerate but the normal one for a runtime with a single department, and
        forcing a synthetic pool in between would put a level in the lock chain that
        means nothing.

        *Root run* is the fourth level §1 names, and it is absent **by default**.
        Until delegation exists a run tree has one member, so the pool would bound a
        single run that `ceilings.max_cost_cents` already bounds — while adding a row
        per run and a lock per reservation, which is exactly the kind of per-call cost
        §9's non-regression gate is watching for. Passing `root_run_id` turns it on,
        and T31's sibling test exercises it, so M5 enables a parameter rather than
        building a level.
        """
        period_start = current_period_start(now)
        parent = await self.ensure_org_pool(
            uow, organization_id, limit_cents=org_limit_cents, now=now
        )
        depth = 1
        if department:
            parent = await uow.budget.ensure_pool(
                department_pool_id(organization_id, department, period_start),
                organization_id,
                scope_type="department",
                scope_id=department_scope_id(organization_id, department),
                period="month",
                period_start=period_start,
                limit_cents=department_limit_cents,
                parent_id=parent,
                depth=1,
            )
            depth = 2
        leaf = await uow.budget.ensure_pool(
            actor_pool_id(organization_id, actor_id, period_start),
            organization_id,
            scope_type="actor",
            scope_id=actor_id,
            period="month",
            period_start=period_start,
            limit_cents=actor_limit_cents,
            parent_id=parent,
            depth=depth,
        )
        if root_run_id is None:
            return leaf
        return await uow.budget.ensure_pool(
            root_run_pool_id(organization_id, root_run_id),
            organization_id,
            scope_type="root_run",
            scope_id=root_run_id,
            period="run",
            period_start=period_start,
            limit_cents=root_run_limit_cents or DEFAULT_ROOT_RUN_LIMIT_CENTS,
            parent_id=leaf,
            depth=depth + 1,
        )

    async def apply_limits(
        self,
        uow: UnitOfWork,
        organization_id: OrganizationId,
        *,
        organization_cents: int,
        departments: Mapping[str, int] | None = None,
        actors: Mapping[ActorId, tuple[str | None, int]] | None = None,
        now: dt.datetime | None = None,
    ) -> list[tuple[str, int, int]]:
        """Set the current period's pool limits from a `BudgetPolicy` (M4).

        Returns `(scope, previous, new)` for every level that moved, so the applier can
        record what it did rather than reporting "budget applied" over a no-op.

        **Lowering a limit below what is already committed is refused** (edge case 79),
        with the current figure in the message. The alternative is a pool whose
        `committed + reserved > limit`, which the CHECK constraint in migration 006
        would then reject on the *next* reservation — turning a config mistake into a
        run that fails for a reason unrelated to anything it did.

        Pools are per calendar month, so this touches the current period only. A limit
        set today does not retroactively rewrite last month's, which is the right
        behaviour for a table the spend report reads.
        """
        changes: list[tuple[str, int, int]] = []

        async def move(pool_id: BudgetPoolId, scope: str, limit_cents: int) -> None:
            previous, held = await uow.budget.set_pool_limit(pool_id, limit_cents)
            if limit_cents < held:
                raise BudgetExceeded(
                    f"{scope}: budget policy sets the limit to {limit_cents}c but "
                    f"{held}c is already committed or reserved this period. Lowering it "
                    "would make the pool refuse every reservation, including ones "
                    "already held."
                )
            if previous != limit_cents:
                changes.append((scope, previous, limit_cents))

        org_pool = await self.ensure_org_pool(
            uow, organization_id, limit_cents=organization_cents, now=now
        )
        await move(org_pool, "org", organization_cents)

        period_start = current_period_start(now)
        for department, cents in sorted((departments or {}).items()):
            pool = await uow.budget.ensure_pool(
                department_pool_id(organization_id, department, period_start),
                organization_id,
                scope_type="department",
                scope_id=department_scope_id(organization_id, department),
                period="month",
                period_start=period_start,
                limit_cents=cents,
                parent_id=org_pool,
                depth=1,
            )
            await move(pool, f"department:{department}", cents)

        for actor_id, (actor_department, cents) in sorted(
            (actors or {}).items(), key=lambda kv: str(kv[0])
        ):
            pool = actor_pool_id(organization_id, actor_id, period_start)
            # The chain has to exist before its leaf can be re-limited, and
            # `ensure_chain` is the one function that knows how to build it in the right
            # order with the right parents. The actor's **department is passed through**:
            # `ensure_pool` is first-writer-wins on conflict, so creating the actor pool
            # here without its department would parent it to the org and leave the
            # department level out of every later reservation chain.
            await self.ensure_chain(
                uow,
                organization_id,
                actor_id=actor_id,
                department=actor_department,
                org_limit_cents=organization_cents,
                department_limit_cents=(departments or {}).get(actor_department or "")
                or DEFAULT_DEPARTMENT_LIMIT_CENTS,
                actor_limit_cents=cents,
                now=now,
            )
            await move(pool, f"actor:{actor_id}", cents)

        return changes

    # --- admission (§4) ----------------------------------------------------------------

    async def admit(
        self,
        uow: UnitOfWork,
        *,
        leaf_pool_id: BudgetPoolId,
        ceiling_cents: int,
        priority: RunPriority,
    ) -> AdmissionVerdict:
        """May a run with this ceiling and priority start?

        Checked at every level of the chain, and the *first* level to refuse is the one
        reported — walking root-first means the answer names the outermost constraint,
        which is the one an operator can act on.

        This is a read; it takes no locks and holds nothing. The reservation that
        follows is what actually claims headroom, and it re-checks the hard bound
        under a lock. A read-then-write race here therefore over-admits by at most one
        run's ceiling and never over-spends, which is the correct trade for keeping the
        admission path off the write lock.
        """
        chain = await uow.budget.chain(leaf_pool_id)
        if not chain:
            return AdmissionVerdict(
                admitted=False,
                reason=REASON_POOL_EXHAUSTED,
                detail=f"no budget pool chain for {leaf_pool_id}",
            )

        for pool in chain:  # root first
            # No headroom at all. Checked before priority, and the ordering is about
            # the *reason* rather than the outcome: an exhausted pool refuses a
            # CRITICAL run too, and reporting that as "priority shed" would tell an
            # operator to raise the priority of something that has nothing to spend.
            if pool.available_cents <= 0:
                return AdmissionVerdict(
                    admitted=False,
                    reason=REASON_POOL_EXHAUSTED,
                    pool_id=pool.id,
                    headroom=pool.headroom,
                    detail=(
                        f"pool {pool.id} (depth {pool.depth}) is at "
                        f"{pool.committed_cents + pool.reserved_cents}/{pool.limit_cents} cents"
                    ),
                )

            soft_ceiling = int(pool.limit_cents * (1 + OVERSUBSCRIPTION_K))
            projected = (
                pool.committed_cents
                + pool.reserved_cents
                + pool.allocated_live_cents
                + ceiling_cents
            )
            if projected > soft_ceiling:
                return AdmissionVerdict(
                    admitted=False,
                    reason=REASON_OVERSUBSCRIBED,
                    pool_id=pool.id,
                    headroom=pool.headroom,
                    detail=(
                        f"pool {pool.id} (depth {pool.depth}) would be allocated "
                        f"{projected} cents against a soft ceiling of {soft_ceiling} "
                        f"({int(OVERSUBSCRIPTION_K * 100)}% over {pool.limit_cents})"
                    ),
                )

            shed = _shed_below(pool.headroom)
            if shed is not None and priority.rank <= shed.rank:
                return AdmissionVerdict(
                    admitted=False,
                    reason=REASON_PRIORITY_SHED,
                    pool_id=pool.id,
                    headroom=pool.headroom,
                    detail=(
                        f"pool {pool.id} (depth {pool.depth}) has "
                        f"{pool.headroom:.1%} headroom; {priority.value} is shed at "
                        f"or below {shed.value}"
                    ),
                )

        return AdmissionVerdict(admitted=True, headroom=min(p.headroom for p in chain))

    async def open_allocation(
        self,
        uow: UnitOfWork,
        *,
        leaf_pool_id: BudgetPoolId,
        run_id: RunId,
        ceiling_cents: int,
        priority: RunPriority,
    ) -> uuid.UUID:
        allocation_id = uuid.uuid5(uuid.NAMESPACE_URL, f"allocation:{run_id}")
        await uow.budget.open_allocation(
            allocation_id, leaf_pool_id, run_id, ceiling_cents, priority.value
        )
        return allocation_id

    async def close_allocation(self, uow: UnitOfWork, run_id: RunId) -> bool:
        return await uow.budget.close_allocation(run_id)

    # --- reservations --------------------------------------------------------------

    async def reserve(
        self,
        uow: UnitOfWork,
        *,
        pool_id: BudgetPoolId,
        run_id: RunId,
        amount_cents: int,
        now: dt.datetime | None = None,
    ) -> ReservationId:
        """Hold headroom against the whole chain before the call that will spend it.

        `pool_id` is the **leaf**; the chain above it is resolved and locked inside
        `reserve_chain`. A caller cannot reserve against one level in isolation, which
        is deliberate — that is precisely the "quick direct UPDATE" M2 §10 warns about,
        and there is no API here that would let it be written by accident.

        A zero-cent reservation still creates a row. That is not waste: it means
        every call has a reservation to reconcile against, so the reconcile path
        has no special case for "free" calls and cannot silently skip recording
        usage for them.
        """
        reservation_id = new_reservation_id()
        expires = (now or dt.datetime.now(dt.UTC)) + dt.timedelta(seconds=self._ttl)
        ok = await uow.budget.reserve_chain(reservation_id, pool_id, run_id, amount_cents, expires)
        if not ok:
            chain = await uow.budget.chain(pool_id)
            tight = min(chain, key=lambda p: p.available_cents, default=None)
            available = tight.available_cents if tight else 0
            where = f" (tightest: pool {tight.id} at depth {tight.depth})" if tight else ""
            raise BudgetExceeded(
                f"chain under pool {pool_id} has {available} cents available, "
                f"{amount_cents} requested{where}"
            )
        return reservation_id

    async def reconcile(
        self, uow: UnitOfWork, reservation_id: ReservationId, actual_cents: int
    ) -> None:
        """Convert a hold into a charge, at every level. Idempotent: a second call is
        a no-op, because the reservation is no longer HELD."""
        await uow.budget.reconcile(reservation_id, actual_cents)

    async def release(self, uow: UnitOfWork, reservation_id: ReservationId) -> None:
        await uow.budget.release(reservation_id)

    async def sweep(self, uow: UnitOfWork) -> int:
        """Reclaim expired holds. Without this every crashed run shrinks the pool —
        and with a hierarchy, every level of it. T28."""
        return await uow.budget.sweep_expired()

    async def chain_state(self, uow: UnitOfWork, leaf_pool_id: BudgetPoolId) -> list[PoolState]:
        return await uow.budget.chain(leaf_pool_id)

    # --- M5: the run tree --------------------------------------------------------------

    async def subtree_usage(self, uow: UnitOfWork, root_run_id: RunId) -> tuple[int, int]:
        """`(cost_cents, model_calls)` spent by a whole run tree.

        The read behind §4's checks 7 and 8. It goes through the service rather than
        letting `DelegationService` hold a `BudgetRepository`, because
        `budget-tables-are-private` says the pool tables are reached only through this
        layer and `runtime.runtime` is on that contract's source list — which is not a
        formality here. `usage_ledger` is the table a "quick direct SELECT" is most
        tempting against, and the moment one exists there are two definitions of what a
        subtree has spent.
        """
        return await uow.budget.subtree_usage(root_run_id)


def _shed_below(headroom: float) -> RunPriority | None:
    """The highest priority that is *refused* at this headroom, or None.

    Reads as "shed everything at or below this". Below 5% headroom that is NORMAL, so
    LOW and NORMAL are both refused and CRITICAL gets through; below 15% it is LOW.

    Only reached when there is *some* headroom — zero is handled earlier as
    `POOL_EXHAUSTED`, because shedding is about which work to prioritise with the
    money that is left, and at zero there is none to prioritise with.
    """
    if headroom < HEADROOM_SHED_NORMAL:
        return RunPriority.NORMAL
    if headroom < HEADROOM_SHED_LOW:
        return RunPriority.LOW
    return None


TOOL_PRICES_CENTS: dict[str, int] = {
    # `web.search@1` is the first tool in the system with a real price. Until M1
    # every entry here was zero and the reservation path was a seam rather than a
    # number; the M0 retro flagged closing that as M1 work, and this is it.
    "web.search@1": 1,
    # A publish is not metered per call by the destination, but it reserves a token
    # amount anyway so that an actor which has exhausted its budget cannot publish.
    # Being unable to pay is a perfectly good reason to refuse an irreversible act.
    "publish.external@1": 1,
}


def estimate_tool_cents(tool_name: str) -> int:
    """What to hold against the budget before a tool call.

    Unknown tools estimate zero, and that is the right default for reads: the
    reservation row is still created, so the reconcile path has no "free call"
    branch to forget about. A tool that costs money belongs in the table above.
    """
    return TOOL_PRICES_CENTS.get(tool_name, 0)
