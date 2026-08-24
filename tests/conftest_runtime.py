"""Shared fixtures for tests that need a whole runtime.

Kept out of `conftest.py` so the pure-domain tests do not pay to import LangGraph
and the Redis client.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import runtime.graphs.echo_agent
import runtime.handlers  # noqa: F401  registers hasher@1
from runtime.artifacts.store import ArtifactStore
from runtime.domain.enums import ActorKind, WorkClass
from runtime.domain.ids import OrganizationId
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
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.runtime.bootstrap import Registrar
from runtime.runtime.run_service import RunService
from runtime.settings import Settings
from runtime.worker.worker import Worker

ECHO_SPEC = ActorSpec(
    name="echo-agent",
    kind=ActorKind.LLM_AGENT,
    graph_ref="echo_agent@1",
    allowed_tools=frozenset({"web.fetch@1", "fixture.sideeffect@1"}),
    ceilings=Ceilings(max_llm_calls=4, max_tool_calls=8),
    model_profiles=ModelProfiles(
        profiles={
            WorkClass.GENERATION: ModelProfile(
                provider="fake",
                model="echo-1",
                input_cents_per_mtok=300,
                output_cents_per_mtok=1500,
            )
        }
    ),
)

HASHER_SPEC = ActorSpec(
    name="hasher",
    kind=ActorKind.DETERMINISTIC_WORKER,
    handler_ref="hasher@1",
    allowed_tools=frozenset(),
    ceilings=Ceilings(max_llm_calls=0, max_tool_calls=0),
)


@dataclass
class Runtime:
    """Everything wired together, for a test that wants the real thing."""

    settings: Settings
    uow: UnitOfWorkFactory
    streams: RedisStreams
    service: RunService
    relay: OutboxRelay
    worker: Worker
    organization_id: OrganizationId

    async def start(self, actor: str, key: str, payload: dict[str, Any] | None = None) -> Any:
        return await self.service.start_run(
            StartRunRequest(
                organization_id=self.organization_id,
                actor_name=actor,
                input=payload or {},
                idempotency_key=key,
            )
        )

    async def pump(self) -> None:
        """Relay then worker — one full turn of the crank."""
        await self.relay.drain()
        await self.worker.drain_stream(block_ms=50)


async def build_runtime(
    settings: Settings, organization_id: OrganizationId
) -> AsyncIterator[Runtime]:
    uow = UnitOfWorkFactory(settings)
    registrar = Registrar(uow)
    await registrar.ensure_organization(organization_id, "acme")
    await registrar.publish_actor(organization_id, ECHO_SPEC)
    await registrar.publish_actor(organization_id, HASHER_SPEC)

    streams = RedisStreams(settings)
    await streams.client.flushall()

    async with checkpointer(settings) as saver:
        worker = Worker(uow, streams, settings=settings, checkpointer=saver)
        await worker.setup()
        yield Runtime(
            settings=settings,
            uow=uow,
            streams=streams,
            service=RunService(uow, settings=settings),
            relay=OutboxRelay(uow, streams, settings),
            worker=worker,
            organization_id=organization_id,
        )
    await streams.close()


def artifact_store(settings: Settings, uow: UnitOfWorkFactory) -> ArtifactStore:
    return ArtifactStore(uow, settings=settings)
