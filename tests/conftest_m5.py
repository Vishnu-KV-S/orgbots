"""M5 fixtures — a delegating runtime with two workers.

**Two workers, and it has to be two.** A parent holds its lease and polls while its
child runs (`DelegationService._await_child`), so a single worker executing the parent
has no thread of control left to execute the child: the test would deadlock, and it
would deadlock for a reason that is a *correct* property of the design rather than a
bug. In production that is several worker processes; here it is two `Worker` objects
over one Redis stream with different consumer names, which is the same arrangement.

`Tree.run` is the crank: it starts the parent on worker A as a task and pumps worker B
until the parent finishes. Worker B never steals the parent, because `claimable` only
returns runs whose lease has lapsed and the parent is heartbeating throughout — which
is itself worth knowing, so `test_the_waiting_parent_is_not_stolen` asserts it rather
than leaving it as a property the fixture depends on silently.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text

import runtime.graphs.delegator
import runtime.graphs.echo_agent
import runtime.handlers  # noqa: F401  registers hasher@1
from runtime.budget.service import department_scope_id as budget_department_scope_id
from runtime.domain.delegation import DelegationLimits
from runtime.domain.enums import ActorKind, ExhaustionPolicy, RunStatus, WorkClass
from runtime.domain.ids import OrganizationId, RunId
from runtime.domain.specs import (
    ActorSpec,
    Ceilings,
    ModelProfile,
    ModelProfiles,
    StartRunRequest,
)
from runtime.events.relay import OutboxRelay
from runtime.events.stream import RedisStreams
from runtime.graphs.checkpointer import checkpointer
from runtime.org.department import department_scope_id as memory_department_scope_id
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.runtime.bootstrap import Registrar
from runtime.runtime.run_service import RunService
from runtime.settings import Settings
from runtime.worker.worker import Worker

DEPARTMENT = "marketing"

PARENT = "delegating-head"
CHILD = "researcher"
SECOND_CHILD = "writer"
GRANDCHILD = "assistant"

PARENT_LIMITS = DelegationLimits(
    enabled=True,
    max_depth=2,
    max_children=2,
    max_live_descendants=3,
    max_subtree_cost_cents=0,
    max_subtree_llm_calls=0,
    exhaustion_policy=ExhaustionPolicy.DRAIN,
)
"""Deliberately not round numbers taken from the settings defaults. Every fan-out test
below is written against *these*, so a limit that stops being enforced fails a test
rather than coincidentally matching a default that also stopped being enforced."""


def _delegator(name: str) -> ActorSpec:
    return ActorSpec(
        name=name,
        kind=ActorKind.LLM_AGENT,
        graph_ref="delegator@1",
        allowed_tools=frozenset(),
        ceilings=Ceilings(max_llm_calls=0, max_tool_calls=0, max_cost_cents=200),
        model_profiles=ModelProfiles(),
    )


def _leaf(name: str, *, tools: frozenset[str] = frozenset()) -> ActorSpec:
    return ActorSpec(
        name=name,
        kind=ActorKind.LLM_AGENT,
        graph_ref="echo_agent@1",
        allowed_tools=tools,
        ceilings=Ceilings(max_llm_calls=2, max_tool_calls=4, max_cost_cents=100),
        model_profiles=ModelProfiles(
            profiles={
                WorkClass.GENERATION: ModelProfile(provider="fake", model="echo-1"),
            }
        ),
    )


@dataclass
class Tree:
    """Everything a delegation test needs, wired and running."""

    settings: Settings
    uow: UnitOfWorkFactory
    streams: RedisStreams
    service: RunService
    relay: OutboxRelay
    parent_worker: Worker
    child_worker: Worker
    organization_id: OrganizationId

    async def start(self, actor: str, key: str, payload: dict[str, Any] | None = None) -> RunId:
        result = await self.service.start_run(
            StartRunRequest(
                organization_id=self.organization_id,
                actor_name=actor,
                input=payload or {},
                idempotency_key=key,
            )
        )
        return result.run_id

    async def run(self, run_id: RunId, *, wait_s: float = 30.0) -> RunStatus:
        """Execute `run_id` on the parent worker while the child worker drains.

        Returns the run's final status. Raises `TimeoutError` rather than hanging: a
        delegation bug's most likely presentation is a parent that waits forever, and a
        test that hangs teaches nobody anything.
        """
        task = asyncio.create_task(self.parent_worker.run_one(run_id))
        deadline = asyncio.get_running_loop().time() + wait_s
        try:
            while not task.done():
                await self.relay.drain()
                await self.child_worker.drain_database(limit=8)
                if asyncio.get_running_loop().time() > deadline:
                    task.cancel()
                    raise TimeoutError(f"run {run_id} did not finish within {wait_s}s")
                await asyncio.sleep(0.05)
            await task
        finally:
            await self.relay.drain()
        return await self.status(run_id)

    async def pump_children(self, turns: int = 6) -> None:
        """Drive the child worker alone. For tests that spawn without a live parent."""
        for _ in range(turns):
            await self.relay.drain()
            await self.child_worker.drain_database(limit=8)
            await asyncio.sleep(0.02)

    # --- inspection ------------------------------------------------------------------

    async def status(self, run_id: RunId) -> RunStatus:
        async with self.uow() as uow:
            row = await uow.runs.get(run_id)
        assert row is not None, f"no run {run_id}"
        return RunStatus(row.status)

    async def children_of(self, run_id: RunId) -> list[Any]:
        async with self.uow() as uow:
            return await uow.delegations.for_parent(run_id)

    async def child_runs(self, run_id: RunId) -> list[dict[str, Any]]:
        async with self.uow() as uow:
            rows = (
                await uow.session.execute(
                    text(
                        "SELECT id, status, depth, agent_path, status_reason FROM runs "
                        "WHERE parent_run_id = :p ORDER BY created_at"
                    ),
                    {"p": run_id},
                )
            ).all()
        return [
            {
                "id": RunId(r.id),
                "status": r.status,
                "depth": r.depth,
                "agent_path": list(r.agent_path),
                "reason": r.status_reason,
            }
            for r in rows
        ]

    async def output_of(self, run_id: RunId) -> dict[str, Any]:
        async with self.uow() as uow:
            events = await uow.outbox.events_for_run(run_id)
        for event in reversed(events):
            if event["topic"] in ("run.succeeded", "run.failed"):
                payload = event["payload"].get("output")
                return payload if isinstance(payload, dict) else {}
        return {}

    async def topics_for(self, run_id: RunId) -> list[str]:
        async with self.uow() as uow:
            events = await uow.outbox.events_for_run(run_id)
        return [e["topic"] for e in events]

    async def set_limits(self, actor: str, limits: DelegationLimits | None) -> None:
        async with self.uow.transaction() as uow:
            await uow.actors.set_delegation(
                self.organization_id,
                actor,
                limits.model_dump(mode="json") if limits else None,
            )

    async def spend(self, root_run_id: RunId, cents: int, *, calls: int = 1) -> None:
        """Charge a run tree without running anything.

        Checks 7 and 8 read `usage_ledger`, and making a fake model actually cost money
        would mean the ceiling tests depended on a price table. Writing the ledger row
        directly is the same thing the gateway does and is the only way to state "this
        tree has spent 400c" as a precondition rather than as an outcome.
        """
        async with self.uow.transaction() as uow:
            for _ in range(max(calls, 1)):
                await uow.budget.record_usage(
                    organization_id=self.organization_id,
                    run_id=root_run_id,
                    root_run_id=root_run_id,
                    pool_id=None,
                    reservation_id=None,
                    kind="model",
                    work_class=WorkClass.WORK.value,
                    call_site="test",
                    provider="fake",
                    model="echo-1",
                    input_tokens=0,
                    output_tokens=0,
                    cost_cents=cents // max(calls, 1),
                )


def delegating_settings(base: Settings, **overrides: Any) -> Settings:
    """`base` with delegation on. Everything else is left exactly as it was, so a
    delegation test and an M4 test differ by the flags under examination and nothing
    else."""
    return Settings(
        **{
            **base.model_dump(),
            "delegation_enabled": True,
            "delegation_child_poll_seconds": 0.05,
            "delegation_child_timeout_seconds": 20.0,
            **overrides,
        }
    )


async def build_tree(settings: Settings, organization_id: OrganizationId) -> AsyncIterator[Tree]:
    uow = UnitOfWorkFactory(settings)
    registrar = Registrar(uow)
    await registrar.ensure_organization(organization_id, "acme")

    async with uow.transaction() as bootstrap:
        # The department row has to exist before any actor is placed in it: the trigger
        # from migration 034 resolves `actors.department` to `department_id` and leaves
        # it NULL when there is no row, which is silent and wrong.
        await bootstrap.departments.ensure(
            budget_department_scope_id(organization_id, DEPARTMENT),
            organization_id,
            DEPARTMENT,
            memory_scope_id=memory_department_scope_id(organization_id, DEPARTMENT),
        )

    for spec in (
        _delegator(PARENT),
        _leaf(CHILD),
        _leaf(SECOND_CHILD),
        _leaf(GRANDCHILD),
    ):
        await registrar.publish_actor(organization_id, spec)

    async with uow.transaction() as setup:
        for name in (PARENT, CHILD, SECOND_CHILD, GRANDCHILD):
            await setup.authority.set_actor_role(
                organization_id, name, role_name=None, department=DEPARTMENT
            )
        await setup.actors.set_delegation(
            organization_id, PARENT, PARENT_LIMITS.model_dump(mode="json")
        )

    streams = RedisStreams(settings)
    await streams.client.flushall()

    async with checkpointer(settings) as saver:
        parent_worker = Worker(uow, streams, settings=settings, checkpointer=saver)
        child_worker = Worker(uow, streams, settings=settings, checkpointer=saver)
        await parent_worker.setup()
        await child_worker.setup()
        yield Tree(
            settings=settings,
            uow=uow,
            streams=streams,
            service=RunService(uow, settings=settings),
            relay=OutboxRelay(uow, streams, settings),
            parent_worker=parent_worker,
            child_worker=child_worker,
            organization_id=organization_id,
        )
    await streams.close()


def child_request(
    actor: str,
    *,
    facts: tuple[str, ...] = (),
    message: str = "done",
    **extra: Any,
) -> dict[str, Any]:
    """One entry for `delegator@1`'s `delegate_to` list."""
    return {
        "actor": actor,
        "task": {"message": message},
        "facts": list(facts),
        **extra,
    }


def payload(*children: dict[str, Any]) -> dict[str, Any]:
    return {"delegate_to": list(children)}


def a_uuid() -> uuid.UUID:
    return uuid.uuid4()


def now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)
