"""RunService — the only door.

`start_run()` is the PR to review hardest in M0. If it is not a single transaction
and not idempotent, everything downstream inherits the defect: the outbox relay
republishes a run that does not exist, the worker claims a run with no spec, the
budget holds cents against a run nobody will ever reconcile.

Two properties, both tested:

*Atomic* — runs, run_specs, the budget reservation and the outbox row are written
in one transaction. Either the run exists, is announced, and has a frozen spec and
a hold against its budget, or none of that happened.

*Idempotent* — `(organization_id, idempotency_key)` is unique, and a repeat
returns the existing Run unchanged rather than raising. That single constraint is
what makes cron dedupe, stream redelivery and API retry all safe at once, which is
why it is neither partial nor nullable.

Note what this method does *not* do: it does not run the graph. It resolves,
admits, writes and returns — about 15ms — and the worker picks the run up from the
stream. A `start_run()` that executed anything would put graph latency on the API
path and, worse, would give the caller's connection ownership of a run nobody else
could recover.

**M2 adds admission, and admission can say no.** A refused run is still *created*, in
`LIMIT_REACHED` with a reason and an event. That is the whole design decision: the
alternative is returning an error to a caller that may be a cron trigger nobody is
watching, and an organization that has quietly stopped working looks exactly like one
with nothing to do (edge case 21). A row and an event mean somebody can tell.

Three other things join the transaction, and each one is here rather than later for a
reason:

*Authority is resolved and frozen* into the compiled spec, so `spec_hash` covers it. A
run executes under one authority for its whole life.

*The budget chain is ensured and reserved against* — org → department → actor — in the
one locked statement that `reserve_chain` provides.

*An advisory allocation is opened*, declaring what this run intends to spend. It is
closed when the run reaches a terminal state; §4's soft ceiling counts the live ones.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from runtime.budget.service import (
    BudgetService,
    estimate_tool_cents,
)
from runtime.domain.authority import ResolvedAuthority
from runtime.domain.delegation import (
    DelegationLimits,
    check_authority_subset,
    check_depth,
    check_scope_subset,
    delegation_row_id,
    extend_agent_path,
)
from runtime.domain.enums import ActorKind, AuditSeverity, BlastRadius, RunStatus
from runtime.domain.errors import (
    FanoutExceeded,
    SpecError,
    SubtreeBudgetExceeded,
    SubtreeCallsExceeded,
)
from runtime.domain.hashing import canonical_json
from runtime.domain.ids import ActorId, RunId, new_run_id
from runtime.domain.specs import (
    ActorSpec,
    CompiledSpec,
    RunSpec,
    StartRunRequest,
    compile_actor_spec,
)
from runtime.events.topics import TOPIC_RUN_QUEUED, TOPIC_RUN_REFUSED
from runtime.gateway.builtin import default_action_floors
from runtime.observability.logging import get_logger
from runtime.org.authority import AuthorityResolver
from runtime.org.killswitch import KillSwitchService
from runtime.persistence.uow import UnitOfWork, UnitOfWorkFactory
from runtime.settings import Settings, get_settings

log = get_logger("runtime.run_service")


@dataclass(frozen=True, slots=True)
class StartRunResult:
    run_id: RunId
    status: RunStatus
    created: bool
    """False when an existing run was returned for a repeated idempotency key."""
    spec_hash: str
    refusal_reason: str | None = None
    """Set when `status is LIMIT_REACHED`. `POOL_EXHAUSTED`, `PRIORITY_SHED`,
    `ALLOCATION_CEILING` or `KILL_SWITCH` — the caller is told which, because "no"
    without a reason sends an operator to the wrong dashboard."""

    @property
    def admitted(self) -> bool:
        return self.status is not RunStatus.LIMIT_REACHED


class RunService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        *,
        settings: Settings | None = None,
        budget: BudgetService | None = None,
        authority: AuthorityResolver | None = None,
        action_floors: Mapping[str, BlastRadius] | None = None,
        kill_switches: KillSwitchService | None = None,
    ) -> None:
        self._uow = uow_factory
        self._settings = settings or get_settings()
        self._budget = budget or BudgetService(
            reservation_ttl_seconds=self._settings.reservation_ttl_seconds
        )
        # The floors come from the tool registry — which tools perform which authority
        # actions, and how bad each one is. Passed in rather than looked up, because
        # `runtime.org` sits below the gateway and a resolver that reached upward for
        # the registry would invert the layer contract that I3/I4 rest on.
        self._authority = authority or AuthorityResolver(
            uow_factory, action_floors=action_floors or default_action_floors()
        )
        self._kill_switches = kill_switches or KillSwitchService(uow_factory)
        # M5, read once at construction. Nothing below consults live settings during a
        # run — these decide what a *new* run is admitted under, which is exactly what
        # `start_run` is for.
        self._delegation_enabled = self._settings.delegation_enabled
        self._root_pool_enabled = (
            self._settings.delegation_enabled and self._settings.delegation_root_pool_enabled
        )
        self._max_depth = self._settings.delegation_max_depth

    async def start_run(self, req: StartRunRequest) -> StartRunResult:
        """Resolve spec, admit, write runs + run_specs + budget + outbox in ONE
        transaction, return.

        Idempotent on `(organization_id, idempotency_key)`: a repeat returns the
        existing Run unchanged.
        """
        # Resolved before the transaction opens. It is a cached read of four tables
        # that change by the week, and holding a write transaction open across it
        # would put the admission lock behind a cache miss.
        authority = await self._authority.resolve(req.organization_id, req.actor_name)
        kill = await self._kill_switches.admits_runs(req.organization_id, req.actor_name)

        async with self._uow.transaction() as uow:
            resolved = await uow.actors.resolve_active(req.organization_id, req.actor_name)
            compiled, spec_hash = self._compile(
                resolved.spec, resolved.actor_id, resolved.version, authority
            )
            limits = _limits_from(resolved.delegation)

            # --- M5 §4, checks 1 to 6 ----------------------------------------------
            #
            # Done here — before the run row is written, and long before anything
            # touches a pool row under a lock. §4's ordering is not stylistic: checks 7
            # and 8 read a table the reservation is about to contend on, so a privilege
            # escalation must be refused *before* we get there rather than after.
            #
            # `agent_path` is derived, never taken from the request. That is what makes
            # check 1 unforgeable: a caller cannot hand in a path that omits the actor
            # it is about to spawn.
            memory_scopes = req.memory_scopes
            agent_path: tuple[str, ...] = ()
            if self._delegation_enabled:
                agent_path = extend_agent_path(req.parent_agent_path, req.actor_name)
            if req.parent_run_id is not None:
                parent_limits = req.parent_limits or DelegationLimits()
                check_depth(req.depth - 1, min(parent_limits.max_depth, self._max_depth))
                await self._check_fanout(uow, req.parent_run_id, parent_limits)
                parent_authority = req.parent_authority or ResolvedAuthority(actor_name="<unknown>")
                check_authority_subset(authority, parent_authority)
                memory_scopes = check_scope_subset(
                    req.memory_scopes,
                    req.parent_memory_scopes,
                    child_actor=req.actor_name,
                    parent_actor=parent_authority.actor_name,
                )

            run_id = new_run_id()
            root_run_id = req.root_run_id or run_id
            deadline = (
                dt.datetime.now(dt.UTC) + dt.timedelta(seconds=req.deadline_s)
                if req.deadline_s
                else None
            )

            # ON CONFLICT DO NOTHING, not a pre-flight SELECT: under 100 concurrent
            # identical keys the SELECT passes for all of them and 99 then collide.
            inserted = await uow.runs.insert_if_absent(
                {
                    "id": run_id,
                    "organization_id": req.organization_id,
                    "root_run_id": root_run_id,
                    "parent_run_id": req.parent_run_id,
                    "actor_id": resolved.actor_id,
                    "actor_version_id": resolved.actor_version_id,
                    "session_id": req.session_id,
                    # M1. Every §7 metric is attributed through this column, so it
                    # is written on the run row as well as frozen into the spec —
                    # the views join `usage_ledger → runs → tasks` and never parse
                    # a spec blob.
                    "task_id": req.task_id,
                    "thread_id": str(run_id),
                    "status": RunStatus.QUEUED.value,
                    "idempotency_key": req.idempotency_key,
                    "priority": req.priority,
                    "deadline": deadline,
                    "depth": req.depth,
                    # M5. The audit copy of the path the cycle check ran against. A
                    # root run's is its own actor's name when delegation is on and
                    # empty when it is off — which keeps T68's byte-identical claim
                    # about a checkout with the flag off literally true of this column
                    # as well as of the behaviour.
                    "agent_path": list(agent_path),
                }
            )
            if inserted is None:
                # Someone else won the key. Return theirs; ours never existed, and
                # because nothing else in this transaction has been written yet
                # there is nothing to undo.
                existing = await uow.runs.get_by_idempotency_key(
                    req.organization_id, req.idempotency_key
                )
                if existing is None:  # pragma: no cover - unique index guarantees one
                    raise SpecError("idempotency conflict with no visible run")
                spec_row = await uow.runs.get_spec(RunId(existing.id))
                return StartRunResult(
                    run_id=RunId(existing.id),
                    status=RunStatus(existing.status),
                    created=False,
                    spec_hash=spec_row.spec_hash if spec_row else spec_hash,
                )

            # M5. The spawn record, in the **same transaction as the run row**. It is
            # bookkeeping rather than the guard — `uq_run_idem` is the guard, because a
            # child's idempotency key *is* its delegation key — but it has to be in this
            # transaction anyway: a row claiming a key for a child that does not exist,
            # or a child with no row, would each be a state nothing can repair.
            #
            # Written before admission, so a child refused for budget still leaves a
            # record of having been asked for. `ON CONFLICT DO NOTHING` covers the
            # replay that lost the run insert above and never reaches here.
            if req.parent_run_id is not None:
                await uow.delegations.claim_key(
                    delegation_row_id(req.parent_run_id, req.idempotency_key),
                    organization_id=req.organization_id,
                    parent_run_id=req.parent_run_id,
                    root_run_id=root_run_id,
                    target_actor=req.actor_name,
                    node=req.delegation_node,
                    ordinal=req.delegation_ordinal,
                    idempotency_key=req.idempotency_key,
                    child_run_id=run_id,
                )

            # --- admission (§4) ---------------------------------------------------
            #
            # M5 turns the fourth level on. `root_run_id` is passed only when
            # delegation is enabled, so with the flag off `ensure_chain` builds the
            # same three-level chain M2 built and takes the same three locks — the
            # parameter M2 left for exactly this is the whole change.
            pool_id = await self._budget.ensure_chain(
                uow,
                req.organization_id,
                actor_id=resolved.actor_id,
                department=authority.department,
                root_run_id=root_run_id if self._root_pool_enabled else None,
            )

            # M5 §4, checks 7 and 8. Read here, one statement before `reserve_chain`
            # takes the chain lock, and *after* the two static checks above — §4 orders
            # them that way because these touch a table the reservation is about to
            # contend on and the static ones do not.
            #
            # Only for a child. A root run has spent nothing yet by definition, and
            # running the query anyway would put a `usage_ledger` scan on every
            # admission in the system to answer `0` (§9's non-regression concern).
            if req.parent_run_id is not None and req.parent_limits is not None:
                await self._check_subtree_ceilings(
                    uow,
                    root_run_id=root_run_id,
                    limits=req.parent_limits,
                    estimate_cents=compiled.ceilings.max_cost_cents,
                )

            verdict = await self._budget.admit(
                uow,
                leaf_pool_id=pool_id,
                ceiling_cents=compiled.ceilings.max_cost_cents,
                priority=req.admission_priority,
            )

            if kill.stopped or verdict.refused:
                reason = "KILL_SWITCH" if kill.stopped else (verdict.reason or "POOL_EXHAUSTED")
                detail = (
                    f"{kill.mode.value if kill.mode else 'stopped'} on {kill.scope}: {kill.reason}"
                    if kill.stopped
                    else verdict.detail
                )
                await self._refuse(
                    uow,
                    run_id=run_id,
                    request=req,
                    reason=reason,
                    detail=detail,
                    actor_id=resolved.actor_id,
                )
                return StartRunResult(
                    run_id=run_id,
                    status=RunStatus.LIMIT_REACHED,
                    created=True,
                    spec_hash=spec_hash,
                    refusal_reason=reason,
                )

            allocation_id = await self._budget.open_allocation(
                uow,
                leaf_pool_id=pool_id,
                run_id=run_id,
                ceiling_cents=compiled.ceilings.max_cost_cents,
                priority=req.admission_priority,
            )
            # Zero cents for read-only tools, but the reservation row is created
            # regardless so the reconcile path has no "free call" branch — and it now
            # holds against every level of the chain, not just the org pool.
            await self._budget.reserve(
                uow,
                pool_id=pool_id,
                run_id=run_id,
                amount_cents=estimate_tool_cents("run.admission"),
            )

            run_spec = RunSpec(
                run_id=run_id,
                organization_id=req.organization_id,
                root_run_id=root_run_id,
                parent_run_id=req.parent_run_id,
                session_id=req.session_id,
                task_id=req.task_id,
                correlation_id=req.correlation_id,
                thread_id=str(run_id),
                depth=req.depth,
                spec=compiled,
                spec_hash=spec_hash,
                input=req.input,
                budget_pool_id=str(pool_id),
                allocation_id=str(allocation_id),
                priority=req.admission_priority,
                durability=req.durability,
                # M5. Three values that make the run's own delegation answerable from
                # the spec alone: where it sits in the tree, what it may read, and what
                # it is allowed to spawn. All three are frozen for the reason authority
                # is frozen — a limit re-read from live config could be raised mid-run.
                memory_scopes=memory_scopes,
                agent_path=agent_path,
                delegation=limits if limits.enabled else None,
            )

            await uow.runs.insert_spec(run_id, run_spec.model_dump(mode="json"), spec_hash)

            await uow.outbox.enqueue(
                organization_id=req.organization_id,
                topic=TOPIC_RUN_QUEUED,
                payload={
                    "run_id": str(run_id),
                    "root_run_id": str(root_run_id),
                    "organization_id": str(req.organization_id),
                    "actor": req.actor_name,
                    "spec_hash": spec_hash,
                    "priority": req.priority,
                },
                dedupe_key=str(run_id),
                run_id=run_id,
                root_run_id=root_run_id,
            )

            await uow.audit.record(
                organization_id=req.organization_id,
                run_id=run_id,
                root_run_id=root_run_id,
                actor_id=resolved.actor_id,
                action="run.start",
                target=req.actor_name,
                outcome="admitted",
                detail={"spec_hash": spec_hash, "idempotency_key": req.idempotency_key},
            )

        return StartRunResult(
            run_id=run_id, status=RunStatus.QUEUED, created=True, spec_hash=spec_hash
        )

    # --- M5 §4: the checks that need the database -------------------------------------

    async def _check_fanout(
        self, uow: UnitOfWork, parent_run_id: RunId, limits: DelegationLimits
    ) -> None:
        """Checks 3 and 4, against live counts.

        Two counts rather than one because they bound different things and only
        together bound a fan-out: `max_children` stops one parent spawning twelve,
        `max_live_descendants` stops three children spawning three each. A
        configuration that sets only the first is bounded at `max_children ^ max_depth`,
        which at 3 and 2 is nine and at 5 and 3 is a hundred and twenty-five.

        **Under a lock on the parent's row**, taken first. An earlier version of this
        argued the race away — a parent is one run on one worker, so two simultaneous
        delegations would need two holders of one lease — and it was wrong in a way
        worth recording, because the reasoning sounded complete. The concurrency is not
        between workers, it is *inside the node*: a parent that fans out with
        `asyncio.gather` runs three admissions at once from one lease, all three read
        zero live children, and all three are admitted against a limit of two. T57
        caught it on the first run and it passed with the check deleted before that.
        """
        await uow.runs.lock_for_fanout(parent_run_id)
        children = await uow.runs.live_children(parent_run_id)
        if children >= limits.max_children:
            raise FanoutExceeded(
                f"run {parent_run_id} already has {children} live child(ren), at its "
                f"limit of {limits.max_children}"
            )
        descendants = await uow.runs.live_descendants(parent_run_id)
        if descendants >= limits.max_live_descendants:
            raise FanoutExceeded(
                f"run {parent_run_id} already has {descendants} live descendant(s), at "
                f"its limit of {limits.max_live_descendants}. Direct children are "
                f"within limits ({children}/{limits.max_children}); it is the subtree "
                "below them that is full"
            )

    async def _check_subtree_ceilings(
        self,
        uow: UnitOfWork,
        *,
        root_run_id: RunId,
        limits: DelegationLimits,
        estimate_cents: int,
    ) -> None:
        """Checks 7 and 8, against the tree's ledger.

        Both bounds are checked even when only one is configured, because `bounds_cost`
        and `bounds_calls` read a zero as *unbounded by this limit* — see
        `DelegationLimits`. T65 is the case where the call ceiling binds and the cost
        ceiling does not, which is a real shape: a tree of cheap classification calls
        exhausts wall clock and attention long before it exhausts money.

        The cost check adds the *child's own ceiling* to what the tree has spent, not
        an estimate of what the child will actually spend. That is the same
        conservatism `admit` uses at every other level and it is the right direction to
        be wrong in: refusing a child that would have fitted costs an escalation to the
        parent, and admitting one that does not costs the ceiling the level exists to
        enforce.
        """
        spent, calls = await self._budget.subtree_usage(uow, root_run_id)
        if limits.bounds_cost and spent + estimate_cents > limits.max_subtree_cost_cents:
            raise SubtreeBudgetExceeded(
                f"run tree {root_run_id} has spent {spent}c and this child could spend "
                f"{estimate_cents}c more, against a subtree ceiling of "
                f"{limits.max_subtree_cost_cents}c"
            )
        if limits.bounds_calls and calls >= limits.max_subtree_llm_calls:
            raise SubtreeCallsExceeded(
                f"run tree {root_run_id} has made {calls} model call(s), at its subtree "
                f"ceiling of {limits.max_subtree_llm_calls}"
            )

    async def _refuse(
        self,
        uow: UnitOfWork,
        *,
        run_id: RunId,
        request: StartRunRequest,
        reason: str,
        detail: str,
        actor_id: ActorId,
    ) -> None:
        """Mark an already-inserted run `LIMIT_REACHED`, and make noise about it.

        The run row is already written at this point — the insert is what won the
        idempotency key — so this is an UPDATE, not a cleanup. Keeping the row is the
        point: a refused run that left no trace would make "why did nothing happen on
        Monday" an unanswerable question, and a cron whose runs are being refused looks
        identical to a cron that is not firing.

        No reservation and no allocation are taken. A refused run holds nothing.
        """
        await uow.runs.refuse(run_id, f"{reason}: {detail}"[:500])
        await uow.outbox.enqueue(
            organization_id=request.organization_id,
            topic=TOPIC_RUN_REFUSED,
            payload={
                "run_id": str(run_id),
                "organization_id": str(request.organization_id),
                "actor": request.actor_name,
                "reason": reason,
                "detail": detail,
                "priority": request.admission_priority.value,
            },
            dedupe_key=f"refused:{run_id}",
            run_id=run_id,
            root_run_id=request.root_run_id or run_id,
        )
        await uow.audit.record(
            organization_id=request.organization_id,
            run_id=run_id,
            root_run_id=request.root_run_id or run_id,
            actor_id=actor_id,
            action="run.refused",
            target=request.actor_name,
            severity=AuditSeverity.HIGH,
            outcome=reason,
            detail={"detail": detail, "priority": request.admission_priority.value},
        )
        log.warning(
            "run.refused",
            run_id=str(run_id),
            actor=request.actor_name,
            reason=reason,
            detail=detail,
            priority=request.admission_priority.value,
        )

    async def get_run_spec(self, uow: UnitOfWork, run_id: RunId) -> RunSpec | None:
        """Load the frozen spec. The worker calls this and never reads live config."""
        row = await uow.runs.get_spec(run_id)
        if row is None:
            return None
        return RunSpec.model_validate(row.spec)

    async def close_allocation(self, run_id: RunId) -> None:
        """Release a run's advisory allocation. Called when it reaches a terminal state.

        Separate from `release_run_holds` — which returns *money* — because an
        allocation returns *intent*, and the two can legitimately be settled at
        different moments. Both must happen, and both are idempotent.
        """
        async with self._uow.transaction() as uow:
            await self._budget.close_allocation(uow, run_id)

    def _compile(
        self,
        raw_spec: dict[str, Any],
        actor_id: ActorId,
        version: int,
        authority: ResolvedAuthority | None = None,
    ) -> tuple[CompiledSpec, str]:
        spec = ActorSpec.model_validate(raw_spec)
        if spec.kind is ActorKind.HYBRID:
            # compile_actor_spec raises this too; naming it here keeps the reason
            # attached to the admission path, which is where an operator will look.
            raise NotImplementedError(
                "ActorKind.HYBRID is not implemented in M0 or M1; refusing to admit a run"
            )
        return compile_actor_spec(
            spec, actor_id=actor_id, actor_version=version, authority=authority
        )


def _limits_from(raw: dict[str, Any] | None) -> DelegationLimits:
    """The actor row's `delegation` JSONB as a value. M5.

    A malformed blob resolves to the refuse-everything default rather than raising.
    That is the one place in this module that swallows an error, and the reasoning is
    specific: this column is written by `apply`, which validated it, so a bad value here
    means somebody edited the row by hand — and the safe reading of a limit nobody can
    parse is that it permits nothing. Raising would take down every run by an actor
    whose delegation block was mistyped, including the runs that never delegate.
    """
    if not raw:
        return DelegationLimits()
    try:
        return DelegationLimits.model_validate(raw)
    except ValueError:
        log.warning("delegation.limits_unparseable", raw=str(raw)[:200])
        return DelegationLimits()


def spec_payload(spec: ActorSpec) -> str:
    """Canonical text for an ActorSpec, for storage and comparison."""
    return canonical_json(spec)
