"""Run rows, and the lease protocol they carry.

The lease is three SQL statements — claim, heartbeat, check — and each is a single
conditional statement rather than a read-then-write. That is not style: a
read-then-write here is a lost-update race that hands one run to two workers.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.domain.enums import RunStatus
from runtime.domain.ids import Fence, RunId, WorkerId
from runtime.persistence.models import Run, RunSpecRow


@dataclass(frozen=True, slots=True)
class ClaimResult:
    run_id: RunId
    fence: Fence
    lease_until: dt.datetime


@dataclass(frozen=True, slots=True)
class ReapResult:
    run_id: RunId
    organization_id: uuid.UUID
    root_run_id: uuid.UUID
    new_status: RunStatus
    lease_expiries: int


class RunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def insert_if_absent(self, row: dict[str, Any]) -> RunId | None:
        """Insert a run, or return None if `(organization_id, idempotency_key)` is
        already taken.

        `ON CONFLICT DO NOTHING` rather than a pre-flight SELECT: under 100
        concurrent identical keys (T2) the SELECT would pass for all of them and
        99 would then fail on the unique index. This lets exactly one win at the
        first attempt and tells the other 99 so without an exception path.
        """
        stmt = text(
            """
            INSERT INTO runs (
                id, organization_id, root_run_id, parent_run_id, actor_id,
                actor_version_id, session_id, task_id, thread_id, status,
                idempotency_key, priority, fence, lease_expiries, deadline, depth,
                agent_path, created_at
            ) VALUES (
                :id, :organization_id, :root_run_id, :parent_run_id, :actor_id,
                :actor_version_id, :session_id, :task_id, :thread_id, :status,
                :idempotency_key, :priority, 0, 0, :deadline, :depth,
                :agent_path, now()
            )
            ON CONFLICT ON CONSTRAINT uq_run_idem DO NOTHING
            RETURNING id
            """
        )
        result = await self._s.execute(stmt, row)
        got = result.scalar_one_or_none()
        return RunId(got) if got is not None else None

    async def get(self, run_id: RunId) -> Run | None:
        return await self._s.get(Run, run_id)

    async def get_by_idempotency_key(self, organization_id: uuid.UUID, key: str) -> Run | None:
        stmt = text("SELECT id FROM runs WHERE organization_id = :org AND idempotency_key = :key")
        found = (
            await self._s.execute(stmt, {"org": organization_id, "key": key})
        ).scalar_one_or_none()
        return await self._s.get(Run, found) if found is not None else None

    async def insert_spec(self, run_id: RunId, spec: dict[str, Any], spec_hash: str) -> None:
        self._s.add(RunSpecRow(run_id=run_id, spec=spec, spec_hash=spec_hash))

    async def get_spec(self, run_id: RunId) -> RunSpecRow | None:
        return await self._s.get(RunSpecRow, run_id)

    # --- lease ---------------------------------------------------------------------

    async def claim(
        self, run_id: RunId, worker_id: WorkerId, lease_seconds: float
    ) -> ClaimResult | None:
        """Take the lease if it is free or expired, bumping the fence.

        The fence bump is what invalidates any worker still holding the previous
        lease, whether it knows it lost the run or not.
        """
        stmt = text(
            """
            UPDATE runs
               SET worker_id  = :worker_id,
                   lease_until = now() + make_interval(secs => :lease_seconds),
                   fence      = fence + 1,
                   status     = 'RUNNING',
                   started_at = COALESCE(started_at, now())
             WHERE id = :run_id
               AND status IN ('QUEUED', 'RUNNING', 'WAITING_CHILD')
               AND (lease_until IS NULL OR lease_until < now())
            RETURNING fence, lease_until
            """
        )
        row = (
            await self._s.execute(
                stmt,
                {"run_id": run_id, "worker_id": worker_id, "lease_seconds": lease_seconds},
            )
        ).one_or_none()
        if row is None:
            return None
        return ClaimResult(run_id=run_id, fence=Fence(row.fence), lease_until=row.lease_until)

    async def heartbeat(
        self, run_id: RunId, worker_id: WorkerId, fence: Fence, lease_seconds: float
    ) -> ClaimResult | None:
        """Extend the lease. Returns None if this worker no longer owns the run —
        which the worker must treat as "stop immediately", not "try again"."""
        stmt = text(
            """
            UPDATE runs
               SET lease_until = now() + make_interval(secs => :lease_seconds)
             WHERE id = :run_id
               AND worker_id = :worker_id
               AND fence = :fence
               AND status IN ('RUNNING', 'WAITING_CHILD')
            RETURNING fence, lease_until
            """
        )
        row = (
            await self._s.execute(
                stmt,
                {
                    "run_id": run_id,
                    "worker_id": worker_id,
                    "fence": int(fence),
                    "lease_seconds": lease_seconds,
                },
            )
        ).one_or_none()
        if row is None:
            return None
        return ClaimResult(run_id=run_id, fence=Fence(row.fence), lease_until=row.lease_until)

    async def current_fence(self, run_id: RunId) -> tuple[int, uuid.UUID | None, str] | None:
        """`(fence, worker_id, status)` — the authority the gateway checks against."""
        stmt = text("SELECT fence, worker_id, status FROM runs WHERE id = :run_id")
        row = (await self._s.execute(stmt, {"run_id": run_id})).one_or_none()
        return (row.fence, row.worker_id, row.status) if row is not None else None

    async def claimable(self, limit: int = 32) -> list[RunId]:
        stmt = text(
            """
            SELECT id FROM runs
             WHERE status IN ('QUEUED', 'RUNNING', 'WAITING_CHILD')
               AND (lease_until IS NULL OR lease_until < now())
             ORDER BY priority DESC, created_at
             LIMIT :limit
            """
        )
        rows = (await self._s.execute(stmt, {"limit": limit})).scalars().all()
        return [RunId(r) for r in rows]

    async def reap_expired(self, max_expiries: int) -> list[ReapResult]:
        """Requeue runs whose lease lapsed; abandon those that have lapsed too often.

        A run that keeps losing its lease is usually killing its worker. Requeuing
        it forever turns one poisoned run into an outage, so the cap sends it to a
        human instead.
        """
        stmt = text(
            """
            UPDATE runs
               SET lease_expiries = lease_expiries + 1,
                   worker_id      = NULL,
                   lease_until    = NULL,
                   status = CASE WHEN lease_expiries + 1 >= :max_expiries
                                 THEN 'ABANDONED' ELSE 'QUEUED' END,
                   status_reason = CASE WHEN lease_expiries + 1 >= :max_expiries
                                 THEN 'lease expired ' || (lease_expiries + 1) || ' times'
                                 ELSE status_reason END,
                   ended_at = CASE WHEN lease_expiries + 1 >= :max_expiries
                                 THEN now() ELSE ended_at END
             WHERE status IN ('RUNNING', 'WAITING_CHILD')
               AND lease_until IS NOT NULL
               AND lease_until < now()
            RETURNING id, organization_id, root_run_id, status, lease_expiries
            """
        )
        rows = (await self._s.execute(stmt, {"max_expiries": max_expiries})).all()
        return [
            ReapResult(
                run_id=RunId(r.id),
                organization_id=r.organization_id,
                root_run_id=r.root_run_id,
                new_status=RunStatus(r.status),
                lease_expiries=r.lease_expiries,
            )
            for r in rows
        ]

    async def refuse(self, run_id: RunId, reason: str) -> bool:
        """M2: admission said no. Terminal, unclaimed, and *not* fence-guarded.

        Distinct from `finish` because there is no worker and no fence to guard with —
        the run has never been claimed. The `status = 'QUEUED' AND worker_id IS NULL`
        predicate is the guard instead, and it is what stops this from being usable to
        kill a run that a worker has since picked up.

        The row survives. A refused run that vanished would make "why did nothing
        happen on Monday" unanswerable, and a cron whose runs are all being refused
        looks exactly like a cron that stopped firing (edge case 21).
        """
        row = (
            await self._s.execute(
                text(
                    """
                    UPDATE runs
                       SET status = :status, status_reason = :reason, ended_at = now()
                     WHERE id = :run_id AND status = 'QUEUED' AND worker_id IS NULL
                    RETURNING id
                    """
                ),
                {"run_id": run_id, "status": RunStatus.LIMIT_REACHED.value, "reason": reason},
            )
        ).one_or_none()
        return row is not None

    # --- M5: the run tree ------------------------------------------------------------
    #
    # Four queries and a cascade, all of them over `parent_run_id`. `root_run_id`
    # identifies a *tree* and is what the budget pool and the usage ledger key on;
    # `parent_run_id` identifies an *edge* and is what fan-out and cancellation need.
    # Both columns have been on the table since migration 002 for exactly this.

    async def set_waiting_child(
        self, run_id: RunId, worker_id: WorkerId, fence: Fence, *, waiting: bool
    ) -> bool:
        """Move a run between RUNNING and WAITING_CHILD. Fence-guarded.

        Guarded for the same reason `finish` is: a zombie that thawed mid-wait must not
        be able to move the status of a run somebody else now owns. It returns False
        rather than raising, because the caller is inside a `delegate()` that is about
        to discover the same thing from its next lease check and should report *that*.

        Purely diagnostic — nothing branches on the status — which is why a False here
        is logged and not escalated. The lease is what authorises work; this is what
        makes the wait legible.
        """
        target = RunStatus.WAITING_CHILD if waiting else RunStatus.RUNNING
        source = RunStatus.RUNNING if waiting else RunStatus.WAITING_CHILD
        row = (
            await self._s.execute(
                text(
                    """
                    UPDATE runs SET status = :target
                     WHERE id = :run_id AND worker_id = :worker_id AND fence = :fence
                       AND status = :source
                    RETURNING id
                    """
                ),
                {
                    "run_id": run_id,
                    "worker_id": worker_id,
                    "fence": int(fence),
                    "target": target.value,
                    "source": source.value,
                },
            )
        ).one_or_none()
        return row is not None

    async def lock_for_fanout(self, run_id: RunId) -> bool:
        """Serialise concurrent admissions under one parent. M5 §4, checks 3 and 4.

        Without this the two fan-out checks are a read-then-write race, and it is not a
        theoretical one: a parent that spawns three children with `asyncio.gather` runs
        three `start_run` transactions that each read `live_children = 0` before any of
        them has inserted a row, and all three are admitted against a limit of two. The
        first version of `_check_fanout` argued the race away on the grounds that a
        parent is one run on one worker — which is true and irrelevant, because the
        concurrency is *inside* the node.

        `FOR NO KEY UPDATE` on the parent's own row, not `FOR UPDATE`, for the reason
        the budget chain uses the weaker lock: `runs.parent_run_id` means a child insert
        takes `FOR KEY SHARE` on this very row, and `FOR UPDATE` would conflict with it.
        That is the same shape as T27's deadlock, one table over, and it would have been
        the same afternoon of debugging.

        **Lock order.** This is taken before the budget chain, always, on every path
        that takes both — `start_run` is the only one. A path that locked a pool and
        then a run row would close a cycle with this one.
        """
        row = (
            await self._s.execute(
                text("SELECT id FROM runs WHERE id = :id FOR NO KEY UPDATE"),
                {"id": run_id},
            )
        ).one_or_none()
        return row is not None

    async def live_children(self, parent_run_id: RunId) -> int:
        """Check 3's count. Direct children only."""
        return int(
            (
                await self._s.execute(
                    text(
                        """
                        SELECT count(*) FROM runs
                         WHERE parent_run_id = :parent
                           AND status IN ('QUEUED','RUNNING','WAITING_CHILD')
                        """
                    ),
                    {"parent": parent_run_id},
                )
            ).scalar_one()
        )

    async def live_descendants(self, run_id: RunId) -> int:
        """Check 4's count. Everything below `run_id`, excluding `run_id` itself.

        Recursive over `parent_run_id` rather than a count over `root_run_id`, and the
        difference is the whole reason this limit exists separately from
        `max_children`: a subtree three levels deep has one root and nine live runs,
        and a query keyed on the root would count the same nine for every one of them.
        This counts what is below *this* run, which is what its own limit governs.

        The recursion is bounded by `max_delegation_depth`, which is 2 in M5a. It is
        written as an unbounded `WITH RECURSIVE` anyway because the bound is a policy
        value and a query that silently truncated at a hard-coded depth would report a
        fan-out as being within limits.
        """
        return int(
            (
                await self._s.execute(
                    text(
                        """
                        WITH RECURSIVE tree AS (
                            SELECT id, status FROM runs WHERE parent_run_id = :run_id
                            UNION ALL
                            SELECT r.id, r.status
                              FROM runs r JOIN tree t ON r.parent_run_id = t.id
                        )
                        SELECT count(*) FROM tree
                         WHERE status IN ('QUEUED','RUNNING','WAITING_CHILD')
                        """
                    ),
                    {"run_id": run_id},
                )
            ).scalar_one()
        )

    async def cancel_descendants(self, run_id: RunId, reason: str) -> list[RunId]:
        """Cancel every non-terminal run below `run_id`. Returns what it cancelled.

        **One statement, and it must be.** A read-then-cancel loop races a child that
        is spawning grandchildren: the loop reads the tree, a child adds a level, the
        loop cancels what it read and the new level survives as an orphan spending the
        tree's money with nobody waiting for it. The recursive CTE and the UPDATE are
        evaluated against one snapshot.

        That still leaves a child which starts *after* this statement, and that child
        is not an orphan by accident — it is edge case 29, and it is handled on the
        other side: `DelegationService.attach` sees the parent is terminal, persists
        the result, marks the row late, emits an event, and never resumes anybody. The
        two halves together are the whole answer; either alone is a leak.

        Terminal runs are left exactly as they are. A child that already succeeded did
        succeed, and rewriting its status to CANCELLED because its parent later failed
        would destroy the evidence that the work was done and the money spent.
        """
        rows = (
            await self._s.execute(
                text(
                    """
                    WITH RECURSIVE tree AS (
                        SELECT id FROM runs WHERE parent_run_id = :run_id
                        UNION ALL
                        SELECT r.id FROM runs r JOIN tree t ON r.parent_run_id = t.id
                    )
                    UPDATE runs
                       SET status = 'CANCELLED',
                           status_reason = :reason,
                           ended_at = now(),
                           lease_until = NULL
                     WHERE id IN (SELECT id FROM tree)
                       AND status IN ('QUEUED','RUNNING','WAITING_CHILD')
                    RETURNING id
                    """
                ),
                {"run_id": run_id, "reason": reason[:500]},
            )
        ).all()
        return [RunId(r.id) for r in rows]

    async def cancel_run(self, run_id: RunId, reason: str) -> bool:
        """Cancel one live run. Not fence-guarded, like `refuse` and for the same
        reason: the caller is not the worker holding it, and the whole point is to stop
        a run whose worker may be mid-call. The status predicate is the guard — a
        terminal run is never rewritten — and `finish`'s own status predicate is what
        stops that worker from undoing this a moment later.
        """
        row = (
            await self._s.execute(
                text(
                    """
                    UPDATE runs
                       SET status = 'CANCELLED', status_reason = :reason,
                           ended_at = now(), lease_until = NULL
                     WHERE id = :run_id
                       AND status IN ('QUEUED','RUNNING','WAITING_CHILD')
                    RETURNING id
                    """
                ),
                {"run_id": run_id, "reason": reason[:500]},
            )
        ).one_or_none()
        return row is not None

    async def agent_path(self, run_id: RunId) -> list[str]:
        """The audit copy. Read by diagnostics and by T56, never by the cycle check —
        that runs against the frozen spec, which is the authority."""
        row = (
            await self._s.execute(
                text("SELECT agent_path FROM runs WHERE id = :id"), {"id": run_id}
            )
        ).scalar_one_or_none()
        return list(row or [])

    async def finish(
        self,
        run_id: RunId,
        worker_id: WorkerId,
        fence: Fence,
        status: RunStatus,
        reason: str | None = None,
    ) -> bool:
        """Terminal transition, guarded by the fence *and* by the current status.

        A zombie that thaws after losing the run must not be able to mark it
        SUCCESS. The fence predicate is the guard.

        **M5 adds the status predicate**, and it guards a second attacker the fence
        cannot see. A cascade cancel (T58) writes CANCELLED straight onto a live
        descendant while its worker is still running it — the worker keeps its lease
        and its fence, because nothing was taken from it, and the fence check alone
        would let it come back a second later and overwrite CANCELLED with SUCCESS.
        The subtree ceiling would then have been enforced in the ledger and not in the
        run history, which is the worst of both.

        The two predicates therefore answer different questions: the fence asks *do you
        still own this run*, and the status asks *is it still yours to end*. A run that
        somebody else already ended is neither, and `finish` returning False sends the
        worker down the same path a lost fence does.
        """
        stmt = text(
            """
            UPDATE runs
               SET status = :status,
                   status_reason = :reason,
                   ended_at = now(),
                   lease_until = NULL
             WHERE id = :run_id
               AND worker_id = :worker_id
               AND fence = :fence
               AND status IN ('QUEUED', 'RUNNING', 'WAITING_CHILD')
            RETURNING id
            """
        )
        row = (
            await self._s.execute(
                stmt,
                {
                    "run_id": run_id,
                    "worker_id": worker_id,
                    "fence": int(fence),
                    "status": status.value,
                    "reason": reason,
                },
            )
        ).one_or_none()
        return row is not None
