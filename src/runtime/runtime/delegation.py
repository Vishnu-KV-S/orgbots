"""Delegation — spawning bounded child work, and collecting it.

**There is no second creation path.** Every child run is created by
`RunService.start_run()`, with `parent_run_id`, `depth + 1` and the parent's frozen
state on the request; this service assembles that request and waits for the answer.
I1 is the reason — one door — and the practical consequence is that the eight checks
in M5 §4 are enforced in one place whether a run was started by a cron, by the API, or
by another run.

What this service adds on top of `start_run` is the part that is not admission:

*Idempotency of the spawn itself.* Spawning a child is a side effect and a replayed
node re-executes its body. The child's `idempotency_key` is a deterministic function of
(parent run, node, checkpoint namespace, ordinal, target) — the same discipline as
`logical_call_id` — so a replay reaches `uq_run_idem`, finds the child it already made,
and waits for that one. §9 risk 2, T67.

*The wait.* The parent keeps its lease, marks itself `WAITING_CHILD`, heartbeats, and
polls. That is a deliberate M5a simplification over suspending the parent and resuming
it on an event: a polled wait makes a worker crash the ordinary lease-expiry case the
reaper has handled since M0, whereas a suspend/resume path would be a new lifecycle
with its own failure modes, shipped dark, and tested by nobody for a fortnight. The
cost is a worker slot held for the child's duration, which is the right trade at depth
2 and the wrong one at depth 5 — one more reason depth > 2 is out of §2's scope.

*The cascade.* When a run reaches a terminal state, everything below it is cancelled
with reason `PARENT_TERMINAL` (T58). And its mirror image: a child that finishes after
its parent is already terminal has its result persisted and attached to the delegation
row, an event is emitted, and **nobody is resumed** (T59). Those two are the same
edge case seen from opposite ends, and either one alone leaks.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from dataclasses import dataclass
from typing import Any

from runtime.budget.service import BudgetService
from runtime.domain.context import RunContext
from runtime.domain.delegation import (
    DelegationOutcome,
    DelegationRequest,
    child_idempotency_key,
    delegation_row_id,
)
from runtime.domain.enums import AuditSeverity, DelegationStatus, ExhaustionPolicy, RunStatus
from runtime.domain.errors import (
    DelegationDisabled,
    DelegationRefused,
    StaleFence,
)
from runtime.domain.ids import RunId
from runtime.domain.specs import StartRunRequest
from runtime.events.topics import TOPIC_DELEGATION_LATE, TOPIC_DELEGATION_REFUSED
from runtime.observability.logging import get_logger
from runtime.persistence.uow import UnitOfWork, UnitOfWorkFactory
from runtime.runtime.run_service import RunService
from runtime.settings import Settings, get_settings

log = get_logger("runtime.delegation")

PARENT_TERMINAL = "PARENT_TERMINAL"
"""The cascade's reason string. A closed value, not a sentence, because §7's exit
criterion asserts on it and a message somebody reworded would fail a test for the
wrong reason."""

SUBTREE_EXHAUSTED = "SUBTREE_EXHAUSTED"
"""`strict` exhaustion cancelled the subtree. T64."""


@dataclass(frozen=True, slots=True)
class SubtreeState:
    """What `drain` needs to know: is the subtree still allowed to grow?"""

    exhausted: bool
    reason: str = ""
    spent_cents: int = 0
    llm_calls: int = 0


class DelegationService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        run_service: RunService,
        *,
        settings: Settings | None = None,
        budget: BudgetService | None = None,
    ) -> None:
        self._uow = uow_factory
        self._runs = run_service
        self._settings = settings or get_settings()
        self._budget = budget or BudgetService(
            reservation_ttl_seconds=self._settings.reservation_ttl_seconds
        )
        self._enabled = self._settings.delegation_enabled
        self._poll = self._settings.delegation_child_poll_seconds
        self._timeout = self._settings.delegation_child_timeout_seconds

    @property
    def enabled(self) -> bool:
        return self._enabled

    # --- spawning ---------------------------------------------------------------------

    async def delegate(self, ctx: RunContext, request: DelegationRequest) -> DelegationOutcome:
        """Spawn a child, wait for it, return what it produced.

        Refusals propagate as `DelegationRefused` subclasses and are recorded in
        `delegations` first, so a refusal is visible to an operator even though it
        created no run. That is the same argument that made a refused run a
        `LIMIT_REACHED` row rather than an exception (edge case 21): a parent that
        caught the exception and carried on would otherwise leave the organization
        quietly not delegating, which looks exactly like an organization with nothing to
        delegate.
        """
        limits = ctx.spec.delegation_limits
        if not self._enabled:
            raise DelegationDisabled(
                "delegation is disabled (RUNTIME_DELEGATION_ENABLED=false); "
                f"{ctx.spec.spec.actor_name} may not spawn {request.target_actor!r}"
            )
        if not limits.enabled:
            raise DelegationDisabled(
                f"actor {ctx.spec.spec.actor_name!r} has no delegation block, or it is "
                "disabled; nothing it does may spawn a child"
            )

        key = child_idempotency_key(
            parent_run_id=ctx.run_id,
            node=request.node or ctx.scope.node,
            checkpoint_ns=request.checkpoint_ns or ctx.scope.checkpoint_ns,
            ordinal=request.ordinal,
            target_actor=request.target_actor,
        )

        try:
            result = await self._runs.start_run(self._child_request(ctx, request, key, limits))
        except DelegationRefused as exc:
            await self._record_refusal(ctx, request, key, exc)
            raise

        child_id = result.run_id
        log.info(
            "delegation.spawned",
            child_run_id=str(child_id),
            target=request.target_actor,
            reused=not result.created,
            **ctx.log_fields(),
        )
        return await self._await_child(ctx, child_id, reused=not result.created)

    def _child_request(
        self,
        ctx: RunContext,
        request: DelegationRequest,
        key: str,
        limits: Any,
    ) -> StartRunRequest:
        """Everything the child is admitted under, in one value.

        Note which fields come from `ctx.spec` — the parent's **frozen** spec — rather
        than from live configuration: the path, the scopes, the authority and the
        limits. A child compared against a re-resolved parent would be compared against
        whatever policy exists now, and a policy loosened this morning would then admit
        a child its parent could not itself have been.
        """
        child = request.context
        return StartRunRequest(
            organization_id=ctx.organization_id,
            actor_name=request.target_actor,
            input=self._child_input(request),
            idempotency_key=key,
            parent_run_id=ctx.run_id,
            root_run_id=ctx.root_run_id,
            # Deliberately **not** the parent's session. A session carries a running
            # summary of a conversation, and handing it over is handing over the
            # history that `ChildContext` exists to withhold (T60).
            session_id=None,
            task_id=ctx.spec.task_id,
            correlation_id=ctx.spec.correlation_id,
            depth=ctx.spec.depth + 1,
            admission_priority=ctx.spec.priority,
            deadline_s=child.deadline_s,
            durability=ctx.spec.durability,
            memory_scopes=child.memory_scopes or None,
            parent_agent_path=ctx.spec.agent_path or (ctx.spec.spec.actor_name,),
            parent_memory_scopes=ctx.spec.memory_scopes,
            parent_authority=ctx.spec.authority,
            parent_limits=limits,
            delegation_node=request.node or ctx.scope.node,
            delegation_ordinal=request.ordinal,
        )

    @staticmethod
    def _child_input(request: DelegationRequest) -> dict[str, Any]:
        """The child's `input`, built from `ChildContext` and from nothing else.

        Assembled here rather than by the caller, and that is the enforcement point for
        §4's isolation rule: a node hands over a `ChildContext`, whose `extra="forbid"`
        and whose fields are a task, a list of extracted sentences and three numbers.
        There is no argument to this function through which a message history could
        arrive, which is a stronger statement than a convention that it must not. T60.
        """
        child = request.context
        return {
            # The task's own input, at the top level, because **a child is an ordinary
            # run**. Any graph that can be started by a cron can be started by a
            # delegation, with no branch for "was I delegated to" — which is what keeps
            # the two paths comparable when M5b asks whether delegation pays for itself.
            **child.task.input,
            # The envelope, under one reserved key so it cannot collide with a task
            # field and so a reader can see at a glance exactly what crossed the
            # boundary. This dict is the entire list; there is no branch above that
            # adds to it and no argument to this function that could.
            "_delegation": {
                "facts": list(child.facts),
                "output_schema_ref": child.task.output_schema_ref,
                "title": child.task.title,
                "objective": child.task.objective,
                "budget_headroom_cents": child.budget_headroom_cents,
                "delegated_by_node": request.node,
            },
        }

    # --- waiting ------------------------------------------------------------------------

    async def _await_child(
        self, ctx: RunContext, child_id: RunId, *, reused: bool
    ) -> DelegationOutcome:
        """Hold the lease, mark WAITING_CHILD, poll until the child is terminal.

        The status flip is diagnostic and its failure is not fatal — see
        `RunRepository.set_waiting_child`. What *is* fatal is losing the lease, and the
        loop notices that the same way every gateway does: the fence moved, so this
        worker no longer owns the parent and must stop rather than keep waiting for a
        child that now belongs to somebody else's replay.
        """
        await self._set_waiting(ctx, waiting=True)
        deadline = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=self._timeout)
        try:
            while True:
                async with self._uow() as uow:
                    row = await uow.runs.get(child_id)
                    status = RunStatus(row.status) if row is not None else None
                    fence = await uow.runs.current_fence(ctx.run_id)
                if fence is not None and fence[0] != int(ctx.lease.fence):
                    raise StaleFence(ctx.run_id, int(ctx.lease.fence), fence[0])
                if status is not None and status.is_terminal:
                    return await self._collect(child_id, status, reused=reused)
                if dt.datetime.now(dt.UTC) >= deadline:
                    return await self._abandon_child(ctx, child_id, reused=reused)
                await asyncio.sleep(self._poll)
        finally:
            await self._set_waiting(ctx, waiting=False)

    async def _set_waiting(self, ctx: RunContext, *, waiting: bool) -> None:
        async with self._uow.transaction() as uow:
            moved = await uow.runs.set_waiting_child(
                ctx.run_id, ctx.worker_id, ctx.lease.fence, waiting=waiting
            )
        if not moved:
            log.debug("delegation.status_flip_rejected", waiting=waiting, **ctx.log_fields())

    async def _abandon_child(
        self, ctx: RunContext, child_id: RunId, *, reused: bool
    ) -> DelegationOutcome:
        """The parent waited longer than it is willing to. Cancel and report.

        `drain` semantics arrived at by the clock: the parent gets an outcome it can
        summarise rather than an exception, because a parent that failed here would
        turn one stuck child into a failed run — and §9 risk 1 is that delegation
        multiplies problems, not that it should invent them.
        """
        cancelled = await self.cancel_subtree(child_id, PARENT_TERMINAL, include_self=True)
        log.warning(
            "delegation.child_timeout",
            child_run_id=str(child_id),
            cancelled=len(cancelled),
            timeout_s=self._timeout,
            **ctx.log_fields(),
        )
        return DelegationOutcome(
            child_run_id=child_id,
            status=RunStatus.CANCELLED.value,
            reason=f"parent stopped waiting after {self._timeout:.0f}s",
            reused=reused,
        )

    async def _collect(
        self, child_id: RunId, status: RunStatus, *, reused: bool
    ) -> DelegationOutcome:
        """Read what the child produced, from the row its own `_finish` attached.

        The delegation row is the primary source and the outbox is the fallback,
        because the row is written inside the child's terminal transaction and the
        outbox is published from it afterwards — so there is a window in which the
        event has not been relayed and the row already has the answer.
        """
        async with self._uow() as uow:
            row = await uow.delegations.get_by_child(child_id)
            cost = await uow.budget.spend_for_run(child_id)
            output = row.result if row is not None and row.result is not None else None
            if output is None:
                output = _output_from_events(await uow.outbox.events_for_run(child_id))
            child = await uow.runs.get(child_id)
        return DelegationOutcome(
            child_run_id=child_id,
            status=status.value,
            output=output or {},
            cost_cents=cost,
            reason=child.status_reason if child is not None else None,
            reused=reused,
        )

    # --- the two halves of edge case 28/29 ------------------------------------------------

    async def cancel_subtree(
        self, run_id: RunId, reason: str, *, include_self: bool = False
    ) -> list[RunId]:
        """Cancel everything below `run_id`. Returns what was cancelled.

        Called from `RunExecutor._finish` for every terminal run, which is why it must
        be cheap when there is nothing to do: with delegation off no run has children,
        the partial index in migration 035 has no matching rows, and the recursive CTE
        returns immediately. That is T68's claim about this path.
        """
        async with self._uow.transaction() as uow:
            cancelled = await uow.runs.cancel_descendants(run_id, reason)
            if include_self and await uow.runs.cancel_run(run_id, reason):
                cancelled.append(run_id)
            if not cancelled:
                return []
            await uow.delegations.mark_cancelled(cancelled)
            # Holds are released and allocations closed per run rather than in one
            # bulk statement: `release_run_holds` walks the pool chain in the documented
            # `depth ASC, id ASC` order, and a bulk update over several chains would be
            # a second reservation path taking locks in scan order — which is exactly
            # the deadlock `budget-tables-are-private` exists to prevent (M2 §10).
            for child in cancelled:
                await uow.budget.release_run_holds(child)
                await uow.budget.close_allocation(child)
        log.info("delegation.cascade", run_id=str(run_id), cancelled=len(cancelled), reason=reason)
        return cancelled

    async def attach_child_result(
        self,
        uow: UnitOfWork,
        *,
        child_run_id: RunId,
        parent_run_id: RunId,
        status: RunStatus,
        output: dict[str, Any],
    ) -> bool:
        """Persist a child's result on its delegation row. Edge case 29.

        Called from the **child's** terminal transaction, so it happens whether or not
        anybody is waiting. That is the whole design of the late case: a child cannot
        know at spawn time that its parent will die, and a mechanism that only ran when
        a parent was listening would lose exactly the results nobody was listening for.

        Returns True when this call is the one that attached — a redelivered event
        attaches once, because `attach_result` predicates on `attached_at IS NULL`.
        """
        parent = await uow.runs.get(parent_run_id)
        if parent is None:  # pragma: no cover - the FK on delegations makes this unreachable
            return False
        late = RunStatus(parent.status).is_terminal
        attached = await uow.delegations.attach_result(
            child_run_id,
            result=output,
            status=DelegationStatus.COMPLETED,
            late=late,
        )
        if attached and late:
            # An event, and then nothing. §2: *persist, attach, event, never resume.*
            # There is deliberately no code path here that requeues the parent — a
            # terminal run is terminal, and resurrecting one because a child arrived
            # late would mean a run's status could go backwards, which every consumer
            # of `run.succeeded` assumes cannot happen.
            await uow.outbox.enqueue(
                organization_id=parent.organization_id,
                topic=TOPIC_DELEGATION_LATE,
                payload={
                    "child_run_id": str(child_run_id),
                    "parent_run_id": str(parent_run_id),
                    "status": status.value,
                    "parent_status": parent.status,
                },
                dedupe_key=f"late:{child_run_id}",
                run_id=child_run_id,
                root_run_id=RunId(parent.root_run_id),
            )
            log.warning(
                "delegation.late_child",
                child_run_id=str(child_run_id),
                parent_run_id=str(parent_run_id),
                parent_status=parent.status,
            )
        return attached

    # --- the exhaustion policy (§2) ---------------------------------------------------------

    async def subtree_state(self, ctx: RunContext) -> SubtreeState:
        """Has this run tree hit a ceiling? The question `drain` is written over.

        A parent asks this between children rather than discovering it as a refusal,
        because the two policies want different things at that moment: `drain` wants to
        stop *asking* and summarise what it has, and `strict` wants everything below it
        stopped. Learning the answer from an exception would work for neither — an
        exception arrives once per attempt, and `drain` is precisely the decision not to
        attempt.
        """
        limits = ctx.spec.delegation_limits
        if not (limits.bounds_cost or limits.bounds_calls):
            return SubtreeState(exhausted=False)
        async with self._uow() as uow:
            spent, calls = await self._budget.subtree_usage(uow, ctx.root_run_id)
        if limits.bounds_cost and spent >= limits.max_subtree_cost_cents:
            return SubtreeState(
                True, f"spent {spent}c of {limits.max_subtree_cost_cents}c", spent, calls
            )
        if limits.bounds_calls and calls >= limits.max_subtree_llm_calls:
            return SubtreeState(
                True, f"made {calls} of {limits.max_subtree_llm_calls} calls", spent, calls
            )
        return SubtreeState(False, "", spent, calls)

    async def enforce_exhaustion(self, ctx: RunContext) -> SubtreeState:
        """Apply the configured policy when the subtree is exhausted.

        `drain` records and returns; the parent is expected to stop delegating and
        summarise. `strict` cancels every live descendant and the parent is expected to
        end `LIMIT_REACHED`. Neither is enforced *on* the parent from here — a service
        that killed the run its caller was executing would be a control-flow surprise
        in the middle of a graph node — so the policy is applied to the subtree and
        reported to the parent, which decides its own ending. T63 and T64.
        """
        state = await self.subtree_state(ctx)
        if not state.exhausted:
            return state
        policy = ctx.spec.delegation_limits.exhaustion_policy
        if policy is ExhaustionPolicy.STRICT:
            cancelled = await self.cancel_subtree(ctx.run_id, SUBTREE_EXHAUSTED)
            log.warning(
                "delegation.exhausted_strict",
                cancelled=len(cancelled),
                reason=state.reason,
                **ctx.log_fields(),
            )
        else:
            log.info("delegation.exhausted_drain", reason=state.reason, **ctx.log_fields())
        return state

    # --- refusals -------------------------------------------------------------------------

    async def _record_refusal(
        self,
        ctx: RunContext,
        request: DelegationRequest,
        key: str,
        exc: DelegationRefused,
    ) -> None:
        """A refusal creates no run, so this row is the only evidence it happened."""
        reason = f"{type(exc).__name__}: {exc}"
        async with self._uow.transaction() as uow:
            await uow.delegations.record_refusal(
                delegation_row_id(ctx.run_id, key),
                organization_id=ctx.organization_id,
                parent_run_id=ctx.run_id,
                root_run_id=ctx.root_run_id,
                target_actor=request.target_actor,
                node=request.node or ctx.scope.node,
                ordinal=request.ordinal,
                idempotency_key=key,
                reason=reason,
            )
            await uow.outbox.enqueue(
                organization_id=ctx.organization_id,
                topic=TOPIC_DELEGATION_REFUSED,
                payload={
                    "parent_run_id": str(ctx.run_id),
                    "target": request.target_actor,
                    "reason": type(exc).__name__,
                    "detail": str(exc)[:500],
                },
                dedupe_key=f"delegation-refused:{key}",
                run_id=ctx.run_id,
                root_run_id=ctx.root_run_id,
            )
            await uow.audit.record(
                organization_id=ctx.organization_id,
                run_id=ctx.run_id,
                root_run_id=ctx.root_run_id,
                actor_id=ctx.actor_id,
                action="delegation.refused",
                target=request.target_actor,
                severity=AuditSeverity.HIGH,
                outcome=type(exc).__name__,
                detail={"detail": str(exc)[:500]},
            )
        log.warning(
            "delegation.refused",
            target=request.target_actor,
            reason=type(exc).__name__,
            detail=str(exc)[:200],
            **ctx.log_fields(),
        )


def _output_from_events(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The child's output, from the last terminal event it published.

    The same walk `GET /v1/runs/{id}` does, and it is a fallback rather than the primary
    read for one reason: an event is published from the outbox *after* the terminal
    transaction commits, and the delegation row is written *inside* it. Preferring the
    row means the parent never sees a completed child with no answer.
    """
    for event in reversed(events):
        if event["topic"] in ("run.succeeded", "run.failed"):
            payload = event["payload"].get("output")
            return payload if isinstance(payload, dict) else {}
    return None
