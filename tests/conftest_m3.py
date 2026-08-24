"""Fixtures for the M3 memory tests.

`MemoryHarness` builds the subsystem against a real Postgres, the deterministic
`HashingEmbedder` and a scripted model, and hands back the pieces a test needs. It
exists for the same reason `conftest_m2.Harness` does: the write path has five
collaborators and a test that had to construct all five to assert one of them would be
a test nobody updates.

**Two things here are deliberately real rather than faked.**

*The database.* Every isolation property M3 gates on is a property of a SQL predicate —
`MemoryMetadataRepository.authorize` — and a fake would prove the fake works. This is
`tests/conftest.py`'s stated position and it applies with more force here, because
evals 4, 5 and 9 are zero-tolerance.

*The embedder.* `HashingEmbedder` is not a stub returning constants; it is a real
feature-hashing embedder, so a retrieval test measures retrieval rather than measuring a
dictionary lookup. What it is not is *good* — see its docstring — which is why the
quality evals carry their corpus provenance and the isolation tests do not care.

**The model is scripted**, because extraction quality is not what these tests are for.
`ScriptedProvider` returns whatever the test queued, which makes "these exact facts were
extracted" a precondition rather than a hope, and keeps the suite free of a network.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from runtime.domain.context import Lease, RunContext
from runtime.domain.enums import ActorKind, MemoryScope, WorkClass
from runtime.domain.ids import (
    ActorId,
    Fence,
    OrganizationId,
    RunId,
    new_run_id,
    new_worker_id,
)
from runtime.domain.specs import (
    Ceilings,
    CompiledSpec,
    ModelProfile,
    ModelProfiles,
    RunSpec,
)
from runtime.gateway.embeddings import EmbeddingGateway, HashingEmbedder
from runtime.gateway.models import ModelGateway, ModelRequest, ModelResponse
from runtime.memory.planner import ContextPlanner
from runtime.memory.promotion import PromotionService
from runtime.memory.service import MemoryService
from runtime.memory.store import ExtractedFact, NativeMemoryStore
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.settings import Settings

MEMORY_PROFILE = ModelProfile(provider="fake", model="echo-1", input_cents_per_mtok=0)


@dataclass
class ScriptedProvider:
    """A provider that returns queued responses in order, then repeats the last one.

    Repeating rather than raising at the end is deliberate: a test that queues three
    responses and triggers four calls is usually testing something else, and failing on
    the fourth would turn "the write path retried" into an unrelated red test.
    """

    name: str = "fake"
    responses: list[str] = field(default_factory=list)
    calls: list[ModelRequest] = field(default_factory=list)

    def queue(self, text: str) -> None:
        self.responses.append(text)

    async def complete(self, profile: ModelProfile, req: ModelRequest) -> ModelResponse:
        self.calls.append(req)
        text = (
            self.responses.pop(0)
            if self.responses
            else (self.responses[-1:] or ['{"facts": []}'])[0]
        )
        return ModelResponse(
            text=text,
            provider=profile.provider,
            model=profile.model,
            input_tokens=max(1, len(req.prompt) // 4),
            output_tokens=max(1, len(text) // 4),
            cost_cents=0,
        )


def facts_json(*facts: tuple[str, str]) -> str:
    """Build an extractor response for `(subject, statement)` pairs."""
    import json

    return json.dumps(
        {
            "facts": [
                {
                    "subject": subject,
                    "statement": statement,
                    "type": "fact",
                    "confidence": 0.8,
                    "importance": 0.7,
                    "entities": [],
                }
                for subject, statement in facts
            ]
        }
    )


@dataclass
class MemoryHarness:
    settings: Settings
    embeddings: EmbeddingGateway
    store: NativeMemoryStore
    service: MemoryService
    planner: ContextPlanner
    promotions: PromotionService
    provider: ScriptedProvider
    models: ModelGateway

    async def write(
        self,
        organization_id: OrganizationId,
        *,
        scope: MemoryScope,
        scope_id: uuid.UUID,
        facts: Sequence[tuple[str, str]],
        trust: Any = None,
        actor_id: uuid.UUID | None = None,
        run_id: RunId | None = None,
        importance: float = 0.7,
    ) -> list[str]:
        """Put facts in the store *and* the sidecar, bypassing extraction.

        A direct write, because most tests are about what happens to a memory once it
        exists and routing every one of them through a scripted extractor would make
        each test assert three things it does not care about. `test_m3_write.py` is the
        one that exercises the real path end to end.
        """
        from runtime.domain.enums import MemoryTrust
        from runtime.domain.memory import status_for

        level = trust or MemoryTrust.TRUSTED
        events = await self.store.write(
            organization_id=organization_id,
            scope_key=f"{scope.value}:{scope_id}",
            facts=[ExtractedFact(subject=s, statement=t, importance=importance) for s, t in facts],
        )
        async with _uow(self.store).transaction() as uow:
            for event in events:
                if event.event == "NOOP":
                    continue
                await uow.memories.insert(
                    memory_id=event.memory_id,
                    organization_id=organization_id,
                    scope=scope,
                    scope_id=scope_id,
                    memory_type=event.fact.memory_type,
                    trust=level,
                    status=status_for(level),
                    embedding_version=self.embeddings.embedding_version,
                    collection=self.store.collection,
                    source_run_id=run_id,
                    source_actor_id=actor_id,
                    importance=event.fact.importance,
                    supersedes=event.supersedes,
                )
                if event.supersedes is not None:
                    await uow.memories.supersede(old=event.supersedes, new=event.memory_id)
        return [e.memory_id for e in events if e.event != "NOOP"]


def _uow(store: NativeMemoryStore) -> UnitOfWorkFactory:
    return store.uow_factory


async def build_memory_harness(
    uow_factory: UnitOfWorkFactory,
    settings: Settings,
    *,
    injection: bool = False,
    actors: str = "",
    **overrides: Any,
) -> MemoryHarness:
    """Construct the subsystem with memory on and injection off — shadow mode.

    That default is the one M3 spends its first two weeks in (§5), so it is the one a
    test gets unless it says otherwise. `injection=True` is the PR-35 configuration and
    every test that passes it is, by construction, a test of the thing that changes what
    a model sees.
    """
    memory_settings = settings.model_copy(
        update={
            "memory_enabled": True,
            "memory_injection_enabled": injection,
            "memory_injection_actors": actors,
            "embedding_dim": 256,
            **overrides,
        }
    )
    embeddings = EmbeddingGateway(
        uow_factory, provider=HashingEmbedder(dim=256), settings=memory_settings
    )
    store = NativeMemoryStore(uow_factory=uow_factory, embeddings=embeddings)
    await store.setup()

    provider = ScriptedProvider()
    models = ModelGateway(uow_factory, providers={"fake": provider}, settings=memory_settings)
    return MemoryHarness(
        settings=memory_settings,
        embeddings=embeddings,
        store=store,
        service=MemoryService(uow_factory, store, models, profile=MEMORY_PROFILE),
        planner=ContextPlanner(uow_factory, store, settings=memory_settings),
        promotions=PromotionService(uow_factory, store, models, profile=MEMORY_PROFILE),
        provider=provider,
        models=models,
    )


def make_memory_ctx(
    organization_id: OrganizationId,
    *,
    actor_id: uuid.UUID | None = None,
    actor_name: str = "research",
    session_id: uuid.UUID | None = None,
    memory_scopes: tuple[MemoryScope, ...] | None = None,
    run_id: RunId | None = None,
) -> RunContext:
    """A `RunContext` with the fields retrieval actually reads.

    `actor_id` matters more here than anywhere else in the suite: it is both the
    private scope's id and the quarantine key, so two contexts sharing one by accident
    would make T49 and T50 pass for the wrong reason. Callers pass it explicitly.
    """
    rid = run_id or new_run_id()
    compiled = CompiledSpec(
        actor_id=ActorId(actor_id or uuid.uuid4()),
        actor_name=actor_name,
        actor_version=1,
        kind=ActorKind.LLM_AGENT,
        graph_ref="research@1",
        handler_ref=None,
        allowed_tools=frozenset(),
        ceilings=Ceilings(),
        model_profiles=ModelProfiles(
            profiles={WorkClass.WORK: ModelProfile(provider="fake", model="echo-1")}
        ),
        allowed_model_call_sites=None,
        authority=None,
    )
    spec = RunSpec(
        run_id=rid,
        organization_id=organization_id,
        root_run_id=rid,
        thread_id=str(rid),
        session_id=session_id,  # type: ignore[arg-type]
        spec=compiled,
        spec_hash="x" * 64,
        memory_scopes=memory_scopes,
    )
    return RunContext(
        run_id=rid,
        organization_id=organization_id,
        root_run_id=rid,
        actor_id=compiled.actor_id,
        actor_version=1,
        spec=spec,
        lease=Lease(
            run_id=rid,
            worker_id=new_worker_id(),
            fence=Fence(1),
            lease_until=dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5),
        ),
        worker_id=new_worker_id(),
        trace_id="0" * 32,
        session_id=session_id,  # type: ignore[arg-type]
    )


async def seed_org(uow_factory: UnitOfWorkFactory, organization_id: OrganizationId) -> None:
    """The one FK every memory table has. Nothing else."""
    from sqlalchemy import text

    async with uow_factory.transaction() as uow:
        await uow.session.execute(
            text(
                "INSERT INTO organizations (id, name) VALUES (:id, 'm3') "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"id": organization_id},
        )


async def seed_actor(
    uow_factory: UnitOfWorkFactory,
    organization_id: OrganizationId,
    actor_id: uuid.UUID,
    name: str,
) -> int:
    """An actor and one published version, because `runs.actor_version_id` is a FK.

    Returns the version id so a caller inserting a run has something valid to point at.
    Seeding the version as well as the actor is not incidental: a test that skipped it
    would fail on the foreign key rather than on the thing under test, which is how a
    helper starts collecting workarounds.
    """
    from sqlalchemy import text

    async with uow_factory.transaction() as uow:
        await uow.session.execute(
            text(
                "INSERT INTO actors (id, organization_id, name, kind) "
                "VALUES (:id, :org, :name, 'llm_agent') ON CONFLICT (id) DO NOTHING"
            ),
            {"id": actor_id, "org": organization_id, "name": name},
        )
        version_id = (
            await uow.session.execute(
                text(
                    """
                    INSERT INTO actor_versions (actor_id, version, spec, spec_hash)
                    VALUES (:actor, 1, CAST(:spec AS jsonb), 'm3-test')
                    ON CONFLICT ON CONSTRAINT uq_actor_version DO UPDATE SET spec_hash = 'm3-test'
                    RETURNING id
                    """
                ),
                {
                    "actor": actor_id,
                    "spec": f'{{"name": "{name}", "kind": "llm_agent", "graph_ref": "research@1"}}',
                },
            )
        ).scalar_one()
        await uow.session.execute(
            text("UPDATE actors SET active_version_id = :v WHERE id = :id"),
            {"v": version_id, "id": actor_id},
        )
    return int(version_id)
