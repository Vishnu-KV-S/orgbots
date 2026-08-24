"""Budget pools, reservations, allocations, usage.

I8 — `committed + reserved <= limit` — is a CHECK constraint on the table. This
repository's job is to make the common case not violate it and to translate the
violation into a clean `BudgetExceeded` when it does. It is not the enforcement
point; the database is.

**M2 makes the pool a chain, and that is the whole difficulty.** A reservation must
hold against every pool from the run's leaf to the org root *simultaneously*, and the
naive implementation deadlocks: two workers reserving against overlapping chains in
different orders lock each other.

`reserve_chain` is the answer and it is the **only** reservation path in the
codebase. Not two implementations, not an optimised path for the common case — M2 §10
names bypassing it as the risk most likely to reintroduce the deadlock, and the
import-linter contract `budget-tables-are-private` is what stops a "quick" direct
UPDATE elsewhere.

The lock order is `depth ASC, id ASC`: root first, deterministic tiebreak. That
ordering is enforced by `ORDER BY ... FOR UPDATE`, which Postgres plans as
`LockRows → Sort`, so rows are locked in sorted order rather than scan order. If that
plan shape ever changed, T27 is what would notice.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.domain.enums import ReservationStatus
from runtime.domain.errors import ReservationNotFound
from runtime.domain.ids import BudgetPoolId, ReservationId


@dataclass(frozen=True, slots=True)
class PoolState:
    id: BudgetPoolId
    limit_cents: int
    committed_cents: int
    reserved_cents: int
    depth: int = 0
    parent_id: BudgetPoolId | None = None
    allocated_live_cents: int = 0
    """Sum of LIVE allocation ceilings against this pool. Advisory — it holds
    nothing — and it is the term the soft admission check in §4 is written over."""

    @property
    def available_cents(self) -> int:
        return self.limit_cents - self.committed_cents - self.reserved_cents

    @property
    def headroom(self) -> float:
        """Fraction of the limit still unspent and unheld, in `[0, 1]`.

        A zero-limit pool has no headroom by definition rather than by division
        error — a pool with no money cannot admit anything, and answering 1.0 would
        make priority degradation admit everything into an empty pool.
        """
        if self.limit_cents <= 0:
            return 0.0
        return max(0.0, self.available_cents / self.limit_cents)


class BudgetRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def ensure_pool(
        self,
        pool_id: BudgetPoolId,
        organization_id: uuid.UUID,
        scope_type: str,
        scope_id: uuid.UUID,
        period: str,
        period_start: dt.date,
        limit_cents: int,
        parent_id: BudgetPoolId | None = None,
        depth: int = 0,
    ) -> BudgetPoolId:
        """Create the pool if it is not there, and return its id either way.

        **`ON CONFLICT DO NOTHING`, with no constraint named**, and that is a bug fix
        rather than a stylistic choice. It named `uq_budget_pool_scope` until M5, which
        is the *scope* uniqueness — but `id` is a `uuid5` derived from that same scope,
        so two concurrent inserts of the same pool collide on `budget_pools_pkey`
        first, and a clause that names one constraint does not cover a violation of
        another. The insert raised `UniqueViolation` and took the whole admission
        transaction with it.

        M2 never saw it because T27 pre-built every chain before hammering it, so the
        only concurrency was on `UPDATE`. M5 makes pool *creation* the common case —
        one pool per run tree — and T55's second half found it on the first run.

        The unqualified form is safe precisely because of the derivation: there is no
        second way to conflict. Two rows with the same id are two rows with the same
        scope, and both spellings mean "somebody else created it; go and read it".
        """
        existing = (
            await self._s.execute(
                text(
                    """
                    INSERT INTO budget_pools (id, organization_id, scope_type, scope_id,
                                              period, period_start, limit_cents,
                                              parent_id, depth)
                    VALUES (:id, :org, :st, :sid, :period, :ps, :limit, :parent, :depth)
                    ON CONFLICT DO NOTHING
                    RETURNING id
                    """
                ),
                {
                    "id": pool_id,
                    "org": organization_id,
                    "st": scope_type,
                    "sid": scope_id,
                    "period": period,
                    "ps": period_start,
                    "limit": limit_cents,
                    "parent": parent_id,
                    "depth": depth,
                },
            )
        ).scalar_one_or_none()
        if existing is not None:
            return BudgetPoolId(existing)
        found = (
            await self._s.execute(
                text(
                    """
                    SELECT id FROM budget_pools
                     WHERE scope_type = :st AND scope_id = :sid
                       AND period = :period AND period_start = :ps
                    """
                ),
                {"st": scope_type, "sid": scope_id, "period": period, "ps": period_start},
            )
        ).scalar_one()
        return BudgetPoolId(found)

    async def set_pool_limit(self, pool_id: BudgetPoolId, limit_cents: int) -> tuple[int, int]:
        """Raise or lower one pool's limit. Returns `(previous, committed + reserved)`.

        M4 edge case 79: *a budget lowered below current committed is an impossible pool
        state*. This does not enforce that — the caller does, because the caller is the
        one that can say which document asked for it and print the current figure — but
        it returns the number the caller needs in the same round trip, so the check
        cannot be made against a value read a moment earlier.

        The row is locked rather than read plainly: a reservation landing between the
        read and the write would make the figure in the refusal message stale, and a
        refusal that quotes a wrong number is worse than no message.

        **`FOR NO KEY UPDATE`, not `FOR UPDATE`**, and the distinction is the bug T27
        caught. A child pool's foreign key takes `FOR KEY SHARE` on its parent, which
        `FOR UPDATE` conflicts with and `FOR NO KEY UPDATE` does not — and this
        statement runs on a parent pool while `ensure_chain` is inserting children under
        it, which is exactly the shape that deadlocks. `test_m2_governance` asserts the
        absence of `FOR UPDATE` in this file for that reason.
        """
        row = (
            await self._s.execute(
                text(
                    """
                    SELECT limit_cents, committed_cents, reserved_cents
                      FROM budget_pools WHERE id = :id FOR NO KEY UPDATE
                    """
                ),
                {"id": pool_id},
            )
        ).one_or_none()
        if row is None:
            raise ReservationNotFound(f"no budget pool {pool_id}")
        held = int(row.committed_cents) + int(row.reserved_cents)
        await self._s.execute(
            text("UPDATE budget_pools SET limit_cents = :limit WHERE id = :id"),
            {"limit": limit_cents, "id": pool_id},
        )
        return int(row.limit_cents), held

    async def _lock_chain(self, leaf_pool_id: BudgetPoolId) -> list[uuid.UUID]:
        """Lock every pool from `leaf` to the root, root first, deterministic tiebreak.

        **Every** path that mutates `reserved_cents` or `committed_cents` goes through
        here — reserve, reconcile, release and sweep alike. It is not enough for the
        reservation path to be ordered: an unordered reconcile would deadlock against
        an ordered reserve, and the bug would look like a reservation bug.

        Root-first also gives cross-chain safety for free. Two chains in one
        organization always share the root, so a transaction touching several chains
        serialises on that first lock instead of interleaving into a cycle — which is
        what makes the sweeper safe to run against a batch.

        **`FOR NO KEY UPDATE`, not `FOR UPDATE`,** and the difference is not a
        micro-optimisation — `FOR UPDATE` here deadlocks under concurrent admission.

        `budget_pools.parent_id` is a self-referencing foreign key, so inserting a
        *child* pool takes a `FOR KEY SHARE` lock on its parent row, and that lock is
        held for the rest of the inserting transaction. `FOR UPDATE` conflicts with
        `FOR KEY SHARE`. Two `start_run()` calls that each create a pool under the same
        org root therefore each hold key-share on the root and each then ask for
        exclusive on it: a textbook cycle, and one that only appears once a hierarchy
        exists, which is why M0's single-pool version never saw it.

        `FOR NO KEY UPDATE` is the honest lock for what this actually does. We mutate
        `reserved_cents`, `committed_cents` and `allocated_cents` and never a key
        column, so blocking key-share readers buys nothing. It still conflicts with
        itself, which is the only exclusion a reservation needs.
        """
        rows = (
            await self._s.execute(
                text(
                    """
                    WITH RECURSIVE chain AS (
                        SELECT id, parent_id, depth FROM budget_pools WHERE id = :leaf
                        UNION ALL
                        SELECT p.id, p.parent_id, p.depth
                          FROM budget_pools p JOIN chain c ON p.id = c.parent_id
                    )
                    SELECT b.id
                      FROM budget_pools b
                     WHERE b.id IN (SELECT id FROM chain)
                     ORDER BY b.depth ASC, b.id ASC
                       FOR NO KEY UPDATE
                    """
                ),
                {"leaf": leaf_pool_id},
            )
        ).all()
        return [r.id for r in rows]

    async def chain(self, leaf_pool_id: BudgetPoolId) -> list[PoolState]:
        """The pools from `leaf` to the org root, root first.

        Read-only, no locks. Used by admission, which needs to *look* at every level
        before deciding, and by diagnostics. The reservation path does not call this —
        it takes the lock and reads in the same statement, because a read here
        followed by a write there is a race with a name.
        """
        rows = (
            await self._s.execute(
                text(
                    """
                    WITH RECURSIVE chain AS (
                        SELECT id, parent_id, depth FROM budget_pools WHERE id = :leaf
                        UNION ALL
                        SELECT p.id, p.parent_id, p.depth
                          FROM budget_pools p JOIN chain c ON p.id = c.parent_id
                    )
                    SELECT b.id, b.parent_id, b.depth, b.limit_cents, b.committed_cents,
                           b.reserved_cents, b.allocated_cents
                      FROM budget_pools b
                     WHERE b.id IN (SELECT id FROM chain)
                     ORDER BY b.depth ASC, b.id ASC
                    """
                ),
                {"leaf": leaf_pool_id},
            )
        ).all()
        return [
            PoolState(
                id=BudgetPoolId(r.id),
                limit_cents=r.limit_cents,
                committed_cents=r.committed_cents,
                reserved_cents=r.reserved_cents,
                depth=int(r.depth),
                parent_id=BudgetPoolId(r.parent_id) if r.parent_id else None,
                allocated_live_cents=int(r.allocated_cents),
            )
            for r in rows
        ]

    async def reserve_chain(
        self,
        reservation_id: ReservationId,
        leaf_pool_id: BudgetPoolId,
        run_id: uuid.UUID,
        amount_cents: int,
        expires_at: dt.datetime,
    ) -> bool:
        """Hold `amount_cents` against **every** pool from the leaf to the root.

        The single reservation path. Three statements, and the order of the first two
        is the deadlock argument:

        1. Lock the whole chain in one statement, `ORDER BY depth ASC, id ASC`.
           Postgres plans this as `LockRows → Sort`, so the locks are taken in the
           sorted order rather than in whatever order the scan produced. Every
           concurrent reserver therefore takes the same locks in the same sequence,
           which is what makes a cycle impossible rather than unlikely. T27 runs 200
           concurrent chain reservations against overlapping chains and asserts zero
           deadlocks.

        2. One conditional UPDATE across the locked set. The `WHERE` re-checks I8 per
           row, so a chain where *any* level lacks headroom updates fewer rows than it
           locked — and we then report failure rather than leaving a partial hold. The
           CHECK constraint is still the backstop; this is the check that produces a
           good error first.

        3. One reservation row, attributed to the leaf. The reservation is one hold
           against one chain, not N holds against N pools: reconciling it walks the
           same chain, and N rows would make a partial reconcile representable.

        Returns False if any level refused. The caller's transaction is untouched and
        still usable, which matters because `start_run()` is mid-transaction when it
        calls this.
        """
        chain_ids = await self._lock_chain(leaf_pool_id)
        if not chain_ids:
            return False

        updated = (
            await self._s.execute(
                text(
                    """
                    UPDATE budget_pools
                       SET reserved_cents = reserved_cents + :amount
                     WHERE id = ANY(:ids)
                       AND committed_cents + reserved_cents + :amount <= limit_cents
                    RETURNING id
                    """
                ),
                {"ids": chain_ids, "amount": amount_cents},
            )
        ).all()
        if len(updated) != len(chain_ids):
            # At least one level had no headroom. Undo the partial application here
            # rather than raising: the caller may be inside a transaction it intends
            # to commit for other reasons, and a poisoned transaction would take
            # `start_run()`'s run row down with it.
            await self._s.execute(
                text(
                    """
                    UPDATE budget_pools
                       SET reserved_cents = reserved_cents - :amount
                     WHERE id = ANY(:ids)
                    """
                ),
                {"ids": [r.id for r in updated], "amount": amount_cents},
            )
            return False

        await self._s.execute(
            text(
                """
                INSERT INTO budget_reservations (id, pool_id, run_id, amount_cents,
                                                 status, expires_at)
                VALUES (:id, :pool_id, :run_id, :amount, 'HELD', :expires)
                """
            ),
            {
                "id": reservation_id,
                "pool_id": leaf_pool_id,
                "run_id": run_id,
                "amount": amount_cents,
                "expires": expires_at,
            },
        )
        return True

    async def _settle_chain(
        self, leaf_pool_id: BudgetPoolId, held_cents: int, actual_cents: int
    ) -> None:
        """Release a hold and apply a charge across the whole chain, in lock order.

        `GREATEST(…, 0)` on the release side is not defensive padding — it is what
        makes a double settle harmless. A reservation released twice (the sweeper and
        the reconcile racing on a run that came back to life) would otherwise drive
        `reserved_cents` negative, and the CHECK constraint would abort a transaction
        that was trying to *return* money.
        """
        chain_ids = await self._lock_chain(leaf_pool_id)
        if not chain_ids:
            return
        await self._s.execute(
            text(
                """
                UPDATE budget_pools
                   SET reserved_cents = GREATEST(reserved_cents - :held, 0),
                       committed_cents = committed_cents + :actual
                 WHERE id = ANY(:ids)
                """
            ),
            {"ids": chain_ids, "held": held_cents, "actual": actual_cents},
        )

    async def reconcile(self, reservation_id: ReservationId, actual_cents: int) -> bool:
        """Turn a hold into a charge, at every level of the chain.

        Released and committed move together in one statement so a crash between
        them is impossible; a partial application here would either leak headroom
        forever or double-charge — and now it would do so at one level of the tree
        and not the others, which is worse: the levels would silently disagree about
        what had been spent.
        """
        row = (
            await self._s.execute(
                text(
                    """
                    UPDATE budget_reservations
                       SET status = 'RELEASED'
                     WHERE id = :id AND status = 'HELD'
                    RETURNING pool_id, amount_cents
                    """
                ),
                {"id": reservation_id},
            )
        ).one_or_none()
        if row is None:
            return False
        await self._settle_chain(BudgetPoolId(row.pool_id), row.amount_cents, actual_cents)
        return True

    async def release(self, reservation_id: ReservationId) -> bool:
        """Give the hold back without charging — the call never happened."""
        return await self.reconcile(reservation_id, 0)

    async def release_run_holds(self, run_id: uuid.UUID) -> int:
        """Release every hold still outstanding for a finished run.

        The admission reservation taken by `start_run()` has no natural reconcile
        point — it is a hold against the run existing at all, not against a
        specific call — so the run reaching a terminal state is what settles it.
        Without this the sweeper eventually reclaims it, but only after the TTL,
        and until then every completed run is holding headroom it will never use.
        """
        rows = (
            await self._s.execute(
                text(
                    """
                    UPDATE budget_reservations
                       SET status = 'RELEASED'
                     WHERE run_id = :run_id AND status = 'HELD'
                    RETURNING pool_id, amount_cents
                    """
                ),
                {"run_id": run_id},
            )
        ).all()
        for row in rows:
            await self._settle_chain(BudgetPoolId(row.pool_id), row.amount_cents, 0)
        return len(rows)

    async def sweep_expired(self, limit: int = 500) -> int:
        """Reclaim holds whose worker died before reconciling.

        Without this, every crashed run permanently shrinks the pool — and with a
        hierarchy it shrinks every level above it too, so one dead worker takes
        headroom from the whole organization until the TTL. T28 kills a worker
        mid-reservation and asserts the chain recovers.
        """
        rows = (
            await self._s.execute(
                text(
                    """
                    UPDATE budget_reservations
                       SET status = 'EXPIRED'
                     WHERE id IN (
                        SELECT id FROM budget_reservations
                         WHERE status = 'HELD' AND expires_at < now()
                         ORDER BY expires_at LIMIT :limit
                         FOR UPDATE SKIP LOCKED
                     )
                    RETURNING pool_id, amount_cents
                    """
                ),
                {"limit": limit},
            )
        ).all()
        for row in rows:
            await self._settle_chain(BudgetPoolId(row.pool_id), row.amount_cents, 0)
        return len(rows)

    # --- allocations (advisory) ---------------------------------------------------
    #
    # An allocation is intent, not money: it is what the soft ceiling in §4 is
    # written over, and it holds no headroom. The row in `budget_allocations` is the
    # ledger — who declared what, and whether it is still live — and the running total
    # lives in `budget_pools.allocated_cents`, which 006 created and M0/M1 never used.
    #
    # Keeping a denormalised total is worth one paragraph of justification, because
    # normally it would not be. The alternative is a subtree sum per level on the
    # admission path: an ancestor's live allocations are those of *every* descendant,
    # not just of the pool the run attached to, so the honest query is a recursive
    # descent per level of the chain, on every `start_run()`. The counter is
    # maintained by exactly two methods, both of which take the chain lock first, so
    # it cannot drift the way a hand-maintained cache usually does — and
    # `reconcile_allocated_cents` recomputes it from the rows if it ever does.

    async def open_allocation(
        self,
        allocation_id: uuid.UUID,
        leaf_pool_id: BudgetPoolId,
        run_id: uuid.UUID,
        ceiling_cents: int,
        priority: str,
    ) -> bool:
        """Declare a run's intended ceiling against every level of its chain.

        `DO NOTHING` on the run conflict, because a retried admission must not
        double-count its own intent against the ceiling that admitted it — and the
        counter update is skipped with it, which is why the insert comes first.
        """
        row = (
            await self._s.execute(
                text(
                    """
                    INSERT INTO budget_allocations (id, pool_id, run_id, ceiling_cents,
                                                    priority, status)
                    VALUES (:id, :pool, :run, :ceiling, :priority, 'LIVE')
                    ON CONFLICT ON CONSTRAINT uq_alloc_run DO NOTHING
                    RETURNING id
                    """
                ),
                {
                    "id": allocation_id,
                    "pool": leaf_pool_id,
                    "run": run_id,
                    "ceiling": ceiling_cents,
                    "priority": priority,
                },
            )
        ).one_or_none()
        if row is None:
            return False
        chain_ids = await self._lock_chain(leaf_pool_id)
        await self._s.execute(
            text(
                "UPDATE budget_pools SET allocated_cents = allocated_cents + :amount "
                "WHERE id = ANY(:ids)"
            ),
            {"ids": chain_ids, "amount": ceiling_cents},
        )
        return True

    async def close_allocation(self, run_id: uuid.UUID) -> bool:
        """Close a run's allocation. Called when the run reaches a terminal state.

        Without this the soft ceiling ratchets: every finished run keeps declaring
        intent it will never spend, and admission tightens until nothing is admitted
        while the hard numbers say there is money left.
        """
        row = (
            await self._s.execute(
                text(
                    """
                    UPDATE budget_allocations
                       SET status = 'CLOSED', closed_at = now()
                     WHERE run_id = :run AND status = 'LIVE'
                    RETURNING pool_id, ceiling_cents
                    """
                ),
                {"run": run_id},
            )
        ).one_or_none()
        if row is None:
            return False
        chain_ids = await self._lock_chain(BudgetPoolId(row.pool_id))
        await self._s.execute(
            text(
                "UPDATE budget_pools "
                "SET allocated_cents = GREATEST(allocated_cents - :amount, 0) "
                "WHERE id = ANY(:ids)"
            ),
            {"ids": chain_ids, "amount": row.ceiling_cents},
        )
        return True

    async def reconcile_allocated_cents(self, organization_id: uuid.UUID) -> int:
        """Recompute `allocated_cents` from the allocation rows. Returns rows fixed.

        The counter above is the only denormalised number in the budget tables, so
        this is the only repair function. An operator runs it; the tests run it after
        every hierarchy test and assert it changes nothing, which is how a drift bug
        would be caught in the milestone that introduced it rather than in M4.
        """
        rows = (
            await self._s.execute(
                text(
                    """
                    WITH RECURSIVE tree AS (
                        SELECT id AS root, id AS node FROM budget_pools
                         WHERE organization_id = :org
                        UNION ALL
                        SELECT t.root, p.id
                          FROM budget_pools p JOIN tree t ON p.parent_id = t.node
                    ),
                    truth AS (
                        SELECT t.root AS id,
                               COALESCE(sum(a.ceiling_cents) FILTER
                                   (WHERE a.status = 'LIVE'), 0) AS total
                          FROM tree t
                          LEFT JOIN budget_allocations a ON a.pool_id = t.node
                         GROUP BY t.root
                    )
                    UPDATE budget_pools b
                       SET allocated_cents = truth.total
                      FROM truth
                     WHERE b.id = truth.id AND b.allocated_cents <> truth.total
                    RETURNING b.id
                    """
                ),
                {"org": organization_id},
            )
        ).all()
        return len(rows)

    async def pool_state(self, pool_id: BudgetPoolId) -> PoolState | None:
        row = (
            await self._s.execute(
                text(
                    """
                    SELECT id, limit_cents, committed_cents, reserved_cents,
                           allocated_cents, depth, parent_id
                      FROM budget_pools WHERE id = :id
                    """
                ),
                {"id": pool_id},
            )
        ).one_or_none()
        if row is None:
            return None
        return PoolState(
            id=BudgetPoolId(row.id),
            limit_cents=row.limit_cents,
            committed_cents=row.committed_cents,
            reserved_cents=row.reserved_cents,
            depth=int(row.depth),
            parent_id=BudgetPoolId(row.parent_id) if row.parent_id else None,
            allocated_live_cents=int(row.allocated_cents),
        )

    async def reservation_status(self, reservation_id: ReservationId) -> ReservationStatus | None:
        row = (
            await self._s.execute(
                text("SELECT status FROM budget_reservations WHERE id = :id"),
                {"id": reservation_id},
            )
        ).scalar_one_or_none()
        return ReservationStatus(row) if row is not None else None

    async def record_usage(
        self,
        *,
        organization_id: uuid.UUID,
        run_id: uuid.UUID,
        root_run_id: uuid.UUID,
        pool_id: BudgetPoolId | None,
        reservation_id: ReservationId | None,
        kind: str,
        work_class: str | None,
        call_site: str | None,
        provider: str | None,
        model: str | None,
        input_tokens: int,
        output_tokens: int,
        cost_cents: int,
        cached: bool = False,
    ) -> None:
        await self._s.execute(
            text(
                """
                INSERT INTO usage_ledger (
                    organization_id, run_id, root_run_id, pool_id, reservation_id, kind,
                    work_class, call_site, provider, model, input_tokens, output_tokens,
                    cost_cents, cached
                ) VALUES (
                    :org, :run_id, :root_run_id, :pool_id, :res_id, :kind,
                    :work_class, :call_site, :provider, :model, :in_tok, :out_tok,
                    :cost, :cached
                )
                """
            ),
            {
                "org": organization_id,
                "run_id": run_id,
                "root_run_id": root_run_id,
                "pool_id": pool_id,
                "res_id": reservation_id,
                "kind": kind,
                "work_class": work_class,
                "call_site": call_site,
                "provider": provider,
                "model": model,
                "in_tok": input_tokens,
                "out_tok": output_tokens,
                "cost": cost_cents,
                "cached": cached,
            },
        )

    async def subtree_usage(self, root_run_id: uuid.UUID) -> tuple[int, int]:
        """`(cost_cents, llm_calls)` for a whole run tree. M5 checks 7 and 8.

        Keyed on `root_run_id`, which `usage_ledger` has carried since migration 006
        and which nothing has needed until now — every M0-M4 tree had one member, so
        this query and `spend_for_run` returned the same number. They stop agreeing the
        moment a parent has a child, and that divergence is the whole point of the
        column existing.

        One statement for both figures because they are checked together and a second
        round trip would let them disagree by a call. `kind = 'model'` is the count that
        matters for check 8: tool calls and embeddings are bounded by
        `ceilings.max_tool_calls` per run, and a tree of *model* calls is the thing that
        exhausts wall clock and attention without necessarily costing much (edge 24).

        A read, not a lock. It runs inside `start_run()`'s transaction, immediately
        before `reserve_chain` takes the chain lock, so the window is one statement
        wide and the failure mode is over-admitting one child's ceiling — never
        over-spending, because the root-run pool's reservation is still bounded by I8.
        """
        row = (
            await self._s.execute(
                text(
                    """
                    SELECT COALESCE(sum(cost_cents), 0) AS cost,
                           count(*) FILTER (WHERE kind = 'model') AS calls
                      FROM usage_ledger WHERE root_run_id = :root
                    """
                ),
                {"root": root_run_id},
            )
        ).one()
        return int(row.cost), int(row.calls)

    async def spend_for_run(self, run_id: uuid.UUID) -> int:
        return int(
            (
                await self._s.execute(
                    text(
                        "SELECT COALESCE(sum(cost_cents), 0) FROM usage_ledger "
                        "WHERE run_id = :run_id"
                    ),
                    {"run_id": run_id},
                )
            ).scalar_one()
        )
