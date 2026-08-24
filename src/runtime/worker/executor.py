"""Executing one run.

The worker's job for a single run: claim it, load its frozen spec, build the
context and gateways, dispatch to a graph or a handler, and record a terminal
status guarded by the fence.

Three details are load-bearing.

*The spec comes from `run_specs`, never from live config.* An actor republished
mid-flight must not change what an in-flight run may do.

*The heartbeat is a separate task.* A cooperative heartbeat would be starved
during exactly the long tool call where it is needed.

*The terminal write is fence-guarded.* A worker that lost the run cannot mark it
SUCCESS, so a zombie cannot bury the fact that someone else is still working on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from runtime.artifacts.store import ArtifactStore
from runtime.domain.context import Lease, RunContext
from runtime.domain.delegation import ChildContext, DelegationOutcome, DelegationRequest
from runtime.domain.enums import ActorKind, RunStatus
from runtime.domain.errors import DelegationDisabled, StaleFence
from runtime.domain.ids import RunId, WorkerId
from runtime.domain.memory import PlannedContext
from runtime.domain.specs import RunSpec
from runtime.events.topics import TOPIC_RUN_FAILED, TOPIC_RUN_SUCCEEDED
from runtime.gateway.models import ModelGateway
from runtime.gateway.tools import ToolGateway
from runtime.graphs.registry import GRAPH_KEY, get_graph
from runtime.handlers.registry import get_handler
from runtime.memory.planner import ContextPlanner
from runtime.observability.logging import bound_ids, get_logger
from runtime.observability.tracing import current_trace_id, span
from runtime.org.services import OrgServices, build_org_services
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.runtime.delegation import PARENT_TERMINAL, DelegationService
from runtime.settings import Settings
from runtime.worker.lease import Heartbeater, LeaseManager

log = get_logger("worker.executor")


@dataclass
class NodeContext:
    """What a graph node or handler is handed through the LangGraph config.

    Passing the gateways here rather than importing them keeps `runtime.graphs`
    free of the imports the `gateway-only` contract forbids, and makes a graph
    testable against a fake gateway with no patching.

    M1 adds `org` and `artifacts` by the same route and for the same reason. A node
    that reached for a `TaskService` at module scope would be a node no test could
    substitute, and the whole M1 test suite is about substituting exactly these.
    """

    ctx: RunContext
    gateway: ToolGateway
    models: ModelGateway
    org: OrgServices
    artifacts: ArtifactStore

    delegation: DelegationService | None = None
    """M5, and unlike `memory` above, `None` here is **not** a normal value to build a
    graph against — it is the value a unit test gets when it did not wire one.

    The difference is that memory degrades and delegation does not. A node that asks
    for memory when there is none gets an empty block and behaves identically, which is
    the property M3's shadow mode rests on. A node that asks to delegate when there is
    no service cannot quietly do the work itself: that would make the flag change
    *behaviour* rather than *capability*, and M5a's exit claim is precisely that it does
    not. So `delegate()` raises `DelegationDisabled` here, exactly as it does when the
    setting is off, and the two states are indistinguishable to a caller — which is the
    correct symmetry, because both mean "this runtime does not delegate"."""

    memory: ContextPlanner | None = None
    """M3, and **`None` is a normal value here, not a degraded one.**

    A node asks the planner for context and gets an empty `PlannedContext` back when
    memory is off, when the subsystem is not wired, or when retrieval failed — three
    states that a node must not distinguish, because a node that behaved differently
    when memory was unavailable would make M2's and M3's runs incomparable. So the
    idiom at every call site is the same one line whether or not this is set, and the
    shadow-mode guarantee holds trivially when it is `None`.
    """

    async def recall(
        self, node: str, query: str, *, call_site: str | None = None
    ) -> PlannedContext:
        """The one idiom every node uses to ask for memory. **Never raises, never blocks
        on failure, and returns an empty plan when memory is off.**

        Defined here rather than left to each node because there are four graphs and a
        handler, and "remember to check for None, remember to catch, remember that
        shadow mode returns an empty block" is three things to get subtly different in
        at least one of them — which is the same argument `call_structured` makes for
        model calls.

        The returned `PlannedContext.block` is the empty string in shadow mode, so
        `assemble(memory=plan.block)` is correct in both modes and a node needs no
        branch. That is what makes `test_m3_shadow.py`'s byte-identical assertion a
        property of the system rather than of one carefully written node.
        """
        if self.memory is None:
            return PlannedContext(query_text=query, shadow_mode=True)
        return await self.memory.plan(self.ctx, node=node, query=query, call_site=call_site)

    async def delegate(
        self,
        target_actor: str,
        context: ChildContext,
        *,
        node: str = "",
        ordinal: int | None = None,
    ) -> DelegationOutcome:
        """The one idiom every node uses to spawn child work. M5 §4.

        Defined here for the same reason `recall` is: there are four graphs, and
        "remember to check for None, remember the ordinal comes from the node scope,
        remember the checkpoint namespace or a loop will alias" is three things to get
        subtly wrong in at least one of them. The ordinal in particular — a node that
        passed a literal `0` inside a loop would have every iteration derive the same
        idempotency key and every iteration after the first would silently receive the
        first child's result, which is the aliasing bug `NodeScope` was built to
        prevent for model calls and which delegation reintroduces at a higher price.

        So the default ordinal comes from the live scope, and the namespace comes from
        `ctx.scope` rather than from an argument.
        """
        if self.delegation is None:
            raise DelegationDisabled(
                "this runtime has no delegation service; delegation is off "
                "(RUNTIME_DELEGATION_ENABLED) or the worker was built without one"
            )
        scope = self.ctx.scope
        return await self.delegation.delegate(
            self.ctx,
            DelegationRequest(
                target_actor=target_actor,
                context=context,
                node=node or scope.node,
                checkpoint_ns=scope.checkpoint_ns,
                ordinal=scope.next_ordinal() if ordinal is None else ordinal,
            ),
        )


@dataclass(frozen=True, slots=True)
class RunOutcome:
    run_id: RunId
    status: RunStatus
    output: dict[str, Any]
    reason: str | None = None


class RunExecutor:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        *,
        settings: Settings,
        lease_manager: LeaseManager,
        tools: ToolGateway,
        models: ModelGateway,
        artifacts: ArtifactStore,
        checkpointer: Any = None,
        org: OrgServices | None = None,
        memory: ContextPlanner | None = None,
        delegation: DelegationService | None = None,
    ) -> None:
        self._uow = uow_factory
        # M5. `None` unless the worker was built with one, which happens only when
        # `RUNTIME_DELEGATION_ENABLED` is set. Not defaulted into existence, for the
        # reason the planner is not: constructing one here would give every executor in
        # every unit test a live `RunService` capable of creating runs.
        self._delegation = delegation
        self._settings = settings
        self._leases = lease_manager
        self._tools = tools
        self._models = models
        self._artifacts = artifacts
        self._checkpointer = checkpointer
        self._org = org or build_org_services(uow_factory, artifacts)
        # Not defaulted into existence. `build_org_services` constructs its services
        # unconditionally because they are pure database and cost nothing; a planner
        # constructs a store, which creates a table and may need an embedder, so it is
        # built once in `worker.main` and passed down — never here, where an executor
        # in a unit test would silently acquire one.
        self._memory = memory

    def attach_memory(self, planner: ContextPlanner) -> None:
        """See `Worker.attach_memory`. Set once, at startup, never mid-run.

        There is no guard against calling it twice, because the only caller is process
        startup and a guard here would be a check on a path that a test would have to
        work around. What matters is that it is never called *during* a run: a run that
        gained memory halfway through would produce a prompt nobody could reconstruct
        from its spec.
        """
        self._memory = planner

    @property
    def tools(self) -> ToolGateway:
        return self._tools

    @property
    def models(self) -> ModelGateway:
        return self._models

    @property
    def org(self) -> OrgServices:
        return self._org

    async def execute(self, lease: Lease, worker_id: WorkerId) -> RunOutcome:
        run_spec = await self._load_spec(lease.run_id)
        ctx = RunContext(
            run_id=run_spec.run_id,
            organization_id=run_spec.organization_id,
            root_run_id=run_spec.root_run_id,
            actor_id=run_spec.spec.actor_id,
            actor_version=run_spec.spec.actor_version,
            spec=run_spec,
            lease=lease,
            worker_id=worker_id,
            trace_id=current_trace_id(),
            session_id=run_spec.session_id,
        )

        with bound_ids(**ctx.log_fields()), span("run.execute", run_id=str(ctx.run_id)):
            async with Heartbeater(self._leases, lease, self._settings.heartbeat_seconds) as beat:
                try:
                    output = await self._dispatch(ctx)
                except StaleFence as exc:
                    # Someone else owns the run. Say nothing about its status: it is
                    # no longer ours to describe.
                    log.warning("run.fence_lost", error=str(exc))
                    return RunOutcome(ctx.run_id, RunStatus.RUNNING, {}, "fence advanced")
                except Exception as exc:
                    log.exception("run.failed")
                    await self._finish(
                        ctx, beat.lease, RunStatus.FAILED, f"{type(exc).__name__}: {exc}"
                    )
                    return RunOutcome(
                        ctx.run_id, RunStatus.FAILED, {}, f"{type(exc).__name__}: {exc}"
                    )

                await self._finish(ctx, beat.lease, RunStatus.SUCCESS, None, output)
                return RunOutcome(ctx.run_id, RunStatus.SUCCESS, output)

    async def _dispatch(self, ctx: RunContext) -> dict[str, Any]:
        node_ctx = NodeContext(
            ctx=ctx,
            gateway=self._tools,
            models=self._models,
            org=self._org,
            artifacts=self._artifacts,
            memory=self._memory,
            delegation=self._delegation,
        )
        if ctx.spec.spec.kind is ActorKind.DETERMINISTIC_WORKER:
            handler = get_handler(ctx.spec.spec.entrypoint)
            with ctx.node("handler"):
                return await handler(node_ctx, ctx.spec.input)

        graph = get_graph(ctx.spec.spec.entrypoint)().compile(checkpointer=self._checkpointer)
        result = await graph.ainvoke(
            {"input": ctx.spec.input},
            config={
                "configurable": {
                    "thread_id": ctx.spec.thread_id,
                    GRAPH_KEY: node_ctx,
                }
            },
            durability=ctx.spec.durability,
        )
        output: dict[str, Any] = result.get("output", {})
        return output

    async def _load_spec(self, run_id: RunId) -> RunSpec:
        async with self._uow() as uow:
            row = await uow.runs.get_spec(run_id)
        if row is None:
            raise StaleFence(run_id, -1, -1)
        return RunSpec.model_validate(row.spec)

    async def _finish(
        self,
        ctx: RunContext,
        lease: Lease,
        status: RunStatus,
        reason: str | None,
        output: dict[str, Any] | None = None,
    ) -> None:
        """Terminal transition plus its announcement, in one transaction.

        Fence-guarded: if the run moved on, nothing is written and no event is
        published, because a zombie announcing SUCCESS is worse than a run that
        looks stuck.
        """
        won = False
        async with self._uow.transaction() as uow:
            won = await uow.runs.finish(ctx.run_id, lease.worker_id, lease.fence, status, reason)
            if not won:
                # M5 widened what this means. It was "your fence moved"; it is now also
                # "somebody already ended this run", which is what a descendant sees
                # when its parent's cascade got there first. Both answers are the same
                # instruction — write nothing, announce nothing — so the branch does not
                # need to tell them apart, but a reader of this log line does.
                log.warning("run.finish_rejected", status=status.value)
                return
            # A terminal run holds nothing. The admission reservation has no
            # per-call reconcile point, so this is where it settles.
            await uow.budget.release_run_holds(ctx.run_id)
            # M2: and it declares nothing. The allocation is *intent*, separate from
            # the reservation's *money*, and both have to be given back — a run that
            # released its holds but kept its allocation would leave the soft ceiling
            # ratcheting until admission refused runs against a pool with headroom
            # to spare.
            await uow.budget.close_allocation(ctx.run_id)
            topic = TOPIC_RUN_SUCCEEDED if status is RunStatus.SUCCESS else TOPIC_RUN_FAILED
            await uow.outbox.enqueue(
                organization_id=ctx.organization_id,
                topic=topic,
                payload={
                    "run_id": str(ctx.run_id),
                    "status": status.value,
                    "reason": reason,
                    "output": output or {},
                },
                dedupe_key=f"{ctx.run_id}:{status.value}",
                run_id=ctx.run_id,
                root_run_id=ctx.root_run_id,
            )
            await uow.audit.record(
                organization_id=ctx.organization_id,
                run_id=ctx.run_id,
                root_run_id=ctx.root_run_id,
                actor_id=ctx.actor_id,
                fence=int(lease.fence),
                action="run.finish",
                outcome=status.value,
                detail={"reason": reason},
                trace_id=ctx.trace_id,
            )
            # M5, edge case 29, and it is **inside the terminal transaction** on
            # purpose. A child attaches its own result to its delegation row whether or
            # not anybody is waiting, because a child cannot know at spawn time that
            # its parent will die — and a mechanism that only ran when a parent was
            # listening would lose exactly the results nobody was listening for. When
            # the parent is already terminal this marks the row late and emits an
            # event, and resumes nobody. T59.
            if self._delegation is not None and ctx.spec.parent_run_id is not None:
                await self._delegation.attach_child_result(
                    uow,
                    child_run_id=ctx.run_id,
                    parent_run_id=ctx.spec.parent_run_id,
                    status=status,
                    output=output or {},
                )

        # M5, edge case 28, and it is **outside** that transaction — deliberately, and
        # for the opposite reason. The cascade cancels other people's runs, which means
        # other pool chains and other locks; folding it into the terminal transaction
        # would make a run's own ending block on locks its descendants hold, and a
        # descendant mid-reservation would be enough to fail the parent's finish. The
        # cost of the split is a window in which the parent is terminal and a child is
        # not, which is edge case 29 — already handled above, from the child's side.
        if won and self._delegation is not None and status.is_terminal:
            await self._delegation.cancel_subtree(ctx.run_id, PARENT_TERMINAL)
