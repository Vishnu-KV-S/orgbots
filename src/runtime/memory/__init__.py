"""Memory and context. M3.

One package, sitting between `gateway` and `graphs` in the layer stack — above the
gateway because every model call and every embedding it makes must go through one, and
below the graphs because a graph node is what asks it for context.

    graphs | handlers
    memory              ← here
    gateway
    org

The pieces, and which PR each is:

| module | PR | what it is |
|---|---|---|
| `store` | 27 | the `MemoryStore` port and `NativeMemoryStore` |
| `mem0_store` | 27 | the Mem0 adapter — production, optional dependency |
| `extraction` | 28 | run → candidate facts, one `work_class=MEMORY` call |
| `service` | 28 | the write path, `run_trust`, consolidation |
| `worker` | 28 | the outbox consumer that keeps all of it off the hot path |
| `planner` | 29 | retrieval, rerank, budget, `context_traces`, shadow mode |
| `grading` | 30 | the offline grading harness |
| `intentions` | 31 | scheduled intentions and procedure candidates |
| `promotion` | 33 | private → department → company, with review |
| `golden` | 34 | the golden set: loading it, and building it from real runs |
| `evals` | 34 | the nine evals |

**Nothing here changes a prompt unless `memory_injection_enabled` is on** — PR-35, off
by default in `Settings`, per-actor. That is §9's whole point about build order: PR-27
through PR-34 are safe to build during a measurement window because none of them touches
what a model sees, and this package is arranged so that the claim is checkable rather
than merely believed. `ContextPlanner.injects_for` is the single place the decision is
made.
"""

from __future__ import annotations

from runtime.gateway.embeddings import EmbeddingCache, EmbeddingGateway
from runtime.gateway.models import ModelGateway
from runtime.memory.evals import EvalBars, EvalSuite, render_all
from runtime.memory.grading import GradingBar, GradingHarness
from runtime.memory.intentions import IntentionService, ProcedureService
from runtime.memory.planner import ContextPlanner, plan_with
from runtime.memory.promotion import PromotionService
from runtime.memory.service import MemoryService
from runtime.memory.store import (
    ExtractedFact,
    MemoryStore,
    NativeMemoryStore,
    StoreEvent,
    capabilities,
)
from runtime.memory.worker import MemoryWorker
from runtime.observability.logging import get_logger
from runtime.org.department import MEMORY_PROFILE
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.settings import Settings, get_settings

log = get_logger("memory")


class MemorySubsystem:
    """Everything memory, constructed once per process, like `OrgServices`.

    Built together rather than piecemeal for the reason `org.services` gives — a node
    that reached for a planner at module scope would be a node no test could substitute
    — and for one more: the store, the planner and the service must share **one**
    embedding gateway, because they must share its cache and its `embedding_version`.
    Two gateways would mean two versions of "which collection are we in", and the way
    that failure presents is an empty retrieval, not an error.
    """

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        models: ModelGateway,
        *,
        settings: Settings | None = None,
        embeddings: EmbeddingGateway | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self.embeddings = embeddings or EmbeddingGateway(
            uow_factory, settings=self._settings, cache=EmbeddingCache()
        )
        self.store: MemoryStore = _build_store(uow_factory, self.embeddings, models, self._settings)
        self.service = MemoryService(uow_factory, self.store, models, profile=MEMORY_PROFILE)
        self.planner = ContextPlanner(uow_factory, self.store, settings=self._settings)
        self.promotions = PromotionService(uow_factory, self.store, models, profile=MEMORY_PROFILE)
        self.intentions = IntentionService(uow_factory)
        self.procedures = ProcedureService(uow_factory)
        self.grading = GradingHarness(uow_factory)
        self.evals = EvalSuite(uow_factory, self.store)
        self._uow = uow_factory
        self._ready = False

    async def setup(self) -> None:
        """Create the collection and log which backend and capabilities are in play.

        Logged once, loudly, because "we are on the native exact-cosine store because
        pgvector is absent" is the kind of fact that is obvious on the day it is set up
        and mysterious six weeks later when someone asks why search is O(n).
        """
        if self._ready:
            return
        caps = await capabilities(self._uow)
        collection = await self.store.setup()
        self._ready = True
        log.info(
            "memory.ready",
            backend=self.store.backend,
            collection=collection,
            embedding_version=self.embeddings.embedding_version,
            dim=self.embeddings.dim,
            pgvector=caps["pgvector"],
            injection=self._settings.memory_injection_enabled,
        )


def _build_store(
    uow_factory: UnitOfWorkFactory,
    embeddings: EmbeddingGateway,
    models: ModelGateway,
    settings: Settings,
) -> MemoryStore:
    """`native` unless asked otherwise. **Never falls back** — see `mem0_available`."""
    if settings.memory_store == "mem0":
        from runtime.memory.mem0_store import Mem0MemoryStore

        return Mem0MemoryStore(
            uow_factory=uow_factory,
            embeddings=embeddings,
            models=models,
            profile=MEMORY_PROFILE,
            connection_string=settings.sync_database_url.replace(
                "postgresql+psycopg", "postgresql"
            ),
        )
    return NativeMemoryStore(uow_factory=uow_factory, embeddings=embeddings)


__all__ = [
    "ContextPlanner",
    "EvalBars",
    "EvalSuite",
    "ExtractedFact",
    "GradingBar",
    "GradingHarness",
    "IntentionService",
    "MemoryService",
    "MemoryStore",
    "MemorySubsystem",
    "MemoryWorker",
    "NativeMemoryStore",
    "ProcedureService",
    "PromotionService",
    "StoreEvent",
    "plan_with",
    "render_all",
]
