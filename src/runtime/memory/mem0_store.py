"""The Mem0 adapter. The production path, and the one §3.1 says to test first.

Verified against **mem0ai 2.0.18 on 2026-08-23** — `docs/M3_LIBRARY_FACTS.md` is the
record, and it should be re-read before any upgrade. Optional dependency:
`pip install 'agent-org-runtime[memory]'`, plus pgvector in the database.

What this adapter is for is Mem0's **fusion**: the model-based ADD / UPDATE / DELETE /
NOOP decision at write time (fact 1). §3.1: *"This overlaps the supersession design in
v3 §16 — test theirs before building yours."* `NativeMemoryStore`'s subject-key fusion
is deterministic and crude; this is the one that can notice a fact has stopped being
true rather than merely been rewritten.

---

**The part that took the most care: Mem0 owns its own LLM, embedder and connection
pool** (fact 7). Constructed naively, an M3 built on Mem0 would make the highest-volume
model calls in the system the only ones with no `work_class`, no budget reservation, no
kill switch, no rate limit and no audit row — I12 violated, and §13 risk 4's *"tag every
extraction call `work_class=MEMORY`"* made impossible.

The injection points are plain class attributes:

    LlmFactory.provider_to_class["gateway"]      = ("...mem0_store.GatewayLLM", BaseLlmConfig)
    EmbedderFactory.provider_to_class["gateway"] = ("...mem0_store.GatewayEmbedder", ...)

so Mem0 keeps the extraction and fusion, and the runtime keeps the accounting.

**The loop bridge is safe because of fact 2.** `AsyncMemory` wraps every synchronous
call in `asyncio.to_thread`, so `GatewayLLM.generate_response` runs on a worker thread
and never on the event loop thread. `asyncio.run_coroutine_threadsafe(...).result()`
from there cannot deadlock. If a future release calls the LLM directly from the loop,
that assumption breaks — loudly, as a deadlock, not silently — and
`test_m3_store.py::test_the_loop_bridge_refuses_to_run_on_the_loop_thread` is what
catches it before a release does.

**Telemetry is disabled before the import, not after.** `mem0/memory/telemetry.py` reads
`MEM0_TELEMETRY` at module import time, and with it on, `AsyncMemory.__init__` builds a
second vector store against a `mem0migrations` collection — an extra table in your
database (fact 5).
"""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text

from runtime.domain.errors import MemoryStoreUnavailable
from runtime.domain.ids import MemoryId, OrganizationId
from runtime.gateway.embeddings import EmbeddingGateway
from runtime.memory.store import Candidate, ExtractedFact, StoreEvent, collection_name_for
from runtime.observability.logging import get_logger
from runtime.persistence.uow import UnitOfWorkFactory

log = get_logger("memory.mem0")

_LOOP: asyncio.AbstractEventLoop | None = None
_GATEWAYS: dict[str, Any] = {}
_REGISTERED = False
_LOCK = threading.Lock()


def _disable_telemetry() -> None:
    """Set `MEM0_TELEMETRY=False` before `mem0` is imported anywhere. Fact 5.

    Idempotent and unconditional. An operator who *wants* Mem0 telemetry can set the
    variable themselves after this module loads; what is not acceptable is it being on
    because nobody chose, which is the library's default.
    """
    os.environ.setdefault("MEM0_TELEMETRY", "False")
    os.environ["MEM0_TELEMETRY"] = os.environ.get("MEM0_TELEMETRY_OVERRIDE", "False")


def _register_providers() -> None:
    """Point Mem0's factories at our gateway-backed adapters. Fact 7.

    Registered once per process, under a lock, because the factories are module-level
    dicts shared by every `AsyncMemory` in the process and a second registration racing
    the first would leave one of them holding a half-built class path.
    """
    global _REGISTERED
    with _LOCK:
        if _REGISTERED:
            return
        _disable_telemetry()
        try:
            # `BaseLlmConfig` is imported because `LlmFactory`'s registry values carry a
            # config class; `EmbedderFactory`'s do not, which is fact 7's asymmetry and
            # is why there is no matching embedder-config import here.
            from mem0.configs.llms.base import BaseLlmConfig
            from mem0.utils.factory import EmbedderFactory, LlmFactory
        except ImportError as exc:  # pragma: no cover - exercised only without the extra
            raise MemoryStoreUnavailable(
                "mem0ai is not installed. Either `pip install 'agent-org-runtime[memory]'` "
                "or set RUNTIME_MEMORY_STORE=native (the default)."
            ) from exc

        # **The two registries do not have the same value shape**, and this is exactly
        # the kind of asymmetry an upgrade changes silently. In 2.0.18:
        #
        #   LlmFactory.provider_to_class[name]      = (class_path, ConfigClass)
        #   EmbedderFactory.provider_to_class[name] = class_path
        #
        # Getting it wrong does not fail at registration — it fails later, inside
        # `EmbedderFactory.create`, as `AttributeError: 'tuple' object has no attribute
        # 'rsplit'`, which names neither this module nor the mistake. The assertions
        # below turn that into a failure at startup with a sentence attached.
        LlmFactory.provider_to_class["gateway"] = (
            "runtime.memory.mem0_store.GatewayLLM",
            BaseLlmConfig,
        )
        EmbedderFactory.provider_to_class["gateway"] = "runtime.memory.mem0_store.GatewayEmbedder"
        _assert_registry_shapes(LlmFactory, EmbedderFactory)
        _REGISTERED = True


def _assert_registry_shapes(llm_factory: Any, embedder_factory: Any) -> None:
    """Check our two entries look like the library's own, at startup.

    Compared against a provider Mem0 ships rather than against a hardcoded expectation,
    so an upgrade that changes the convention is caught by the *convention* changing
    rather than by our guess about it going stale. `openai` is used because it is the
    one provider both registries have had for every release of this library.
    """
    reference_llm = llm_factory.provider_to_class.get("openai")
    reference_embedder = embedder_factory.provider_to_class.get("openai")
    ours_llm = llm_factory.provider_to_class["gateway"]
    ours_embedder = embedder_factory.provider_to_class["gateway"]

    if reference_llm is not None and type(ours_llm) is not type(reference_llm):
        raise MemoryStoreUnavailable(
            f"mem0's LlmFactory registry now holds {type(reference_llm).__name__} values, "
            f"and our entry is a {type(ours_llm).__name__}. Re-read "
            "docs/M3_LIBRARY_FACTS.md fact 7 before upgrading."
        )
    if reference_embedder is not None and type(ours_embedder) is not type(reference_embedder):
        raise MemoryStoreUnavailable(
            f"mem0's EmbedderFactory registry now holds {type(reference_embedder).__name__} "
            f"values, and our entry is a {type(ours_embedder).__name__}. Re-read "
            "docs/M3_LIBRARY_FACTS.md fact 7 before upgrading."
        )


def _bridge(coro: Any) -> Any:
    """Await a coroutine on the runtime's loop from a Mem0 worker thread.

    See the module docstring for why this is safe. The `RuntimeError` is not defensive
    padding: it is the assertion that fact 2 still holds, and the message names the fact
    so that whoever hits it after an upgrade knows which paragraph to re-verify.
    """
    if _LOOP is None:
        raise MemoryStoreUnavailable("Mem0MemoryStore.setup() has not bound an event loop")
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        running = None
    if running is _LOOP:
        raise MemoryStoreUnavailable(
            "mem0 called a gateway adapter from the event loop thread. Fact 2 in "
            "docs/M3_LIBRARY_FACTS.md — that AsyncMemory wraps sync calls in "
            "asyncio.to_thread — no longer holds for this release. Re-verify before "
            "upgrading; do not remove this check."
        )
    return asyncio.run_coroutine_threadsafe(coro, _LOOP).result(timeout=120)


class GatewayLLM:
    """Mem0's LLM interface, answered by `ModelGateway.complete_detached`.

    Subclassing `LLMBase` is deliberately avoided: it validates a config shape that has
    changed twice in this library's recent history, and all Mem0 requires of an LLM is
    `generate_response`. Duck typing here is the smaller coupling.
    """

    def __init__(self, config: Any = None) -> None:
        self.config = config

    def generate_response(
        self,
        messages: list[dict[str, str]],
        response_format: Any = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
        **kwargs: Any,
    ) -> str:
        from runtime.domain.enums import WorkClass
        from runtime.gateway.models import ModelRequest

        gateway = _GATEWAYS.get("models")
        profile = _GATEWAYS.get("profile")
        organization_id = _GATEWAYS.get("organization_id")
        if gateway is None or profile is None or organization_id is None:
            raise MemoryStoreUnavailable("Mem0 gateway adapters are not bound to a run context")

        system = "\n\n".join(m["content"] for m in messages if m.get("role") == "system")
        prompt = "\n\n".join(m["content"] for m in messages if m.get("role") != "system")
        response = _bridge(
            gateway.complete_detached(
                organization_id,
                ModelRequest(prompt=prompt, system=system or None),
                profile=profile,
                work_class=WorkClass.MEMORY,
                call_site="memory.mem0.fusion",
                pool_id=_GATEWAYS.get("pool_id"),
            )
        )
        return str(response.text)


class GatewayEmbedder:
    """Mem0's embedder interface, answered by `EmbeddingGateway`.

    `embed` and `embed_batch` both exist because `AsyncMemory` calls each in different
    places (fact 2's call sites), and a missing `embed_batch` degrades silently into
    per-item calls — which would make the embedding cache's hit rate look worse and the
    embedding bill look larger, with nothing pointing at the cause.
    """

    def __init__(self, config: Any = None) -> None:
        self.config = config

    def embed(self, text_value: str, memory_action: str | None = None) -> list[float]:
        return self.embed_batch([text_value], memory_action)[0]

    def embed_batch(
        self, texts: Sequence[str], memory_action: str | None = None
    ) -> list[list[float]]:
        gateway = _GATEWAYS.get("embeddings")
        organization_id = _GATEWAYS.get("organization_id")
        if gateway is None or organization_id is None:
            raise MemoryStoreUnavailable("Mem0 gateway adapters are not bound to a run context")
        result = _bridge(
            gateway.embed(
                organization_id,
                list(texts),
                call_site=f"memory.mem0.embed.{memory_action or 'unknown'}",
                pool_id=_GATEWAYS.get("pool_id"),
            )
        )
        return list(result.vectors)


@dataclass
class Mem0MemoryStore:
    """`AsyncMemory` over pgvector, with our accounting and our isolation boundary.

    The scope filter passed down to Mem0 is defence in depth only — fact 4 and §13
    risk 3. `MemoryMetadataRepository.authorize` is the boundary for this adapter
    exactly as it is for the native one, which is what lets T48 and T49 be one test
    parameterised over both rather than two tests that could drift.
    """

    uow_factory: UnitOfWorkFactory
    embeddings: EmbeddingGateway
    models: Any
    profile: Any
    connection_string: str
    backend: str = "mem0"
    _memory: Any = field(default=None, repr=False)
    _collection: str | None = field(default=None, repr=False)

    async def setup(self) -> str:
        global _LOOP
        _register_providers()
        _LOOP = asyncio.get_running_loop()
        _GATEWAYS.update(
            {"models": self.models, "embeddings": self.embeddings, "profile": self.profile}
        )

        await self._require_pgvector()
        collection = collection_name_for(self.embeddings.embedding_version)

        from mem0 import AsyncMemory
        from mem0.configs.base import MemoryConfig

        config = MemoryConfig(
            **{
                "vector_store": {
                    "provider": "pgvector",
                    "config": {
                        "connection_string": self.connection_string,
                        "collection_name": collection,
                        "embedding_model_dims": self.embeddings.dim,
                    },
                },
                "llm": {"provider": "gateway", "config": {"model": "gateway"}},
                "embedder": {"provider": "gateway", "config": {"model": "gateway"}},
            }
        )
        self._memory = await asyncio.to_thread(AsyncMemory, config)
        self._collection = collection

        async with self.uow_factory.transaction() as uow:
            await uow.session.execute(
                text(
                    """
                    INSERT INTO mem.collections (name, embedding_version, dim, backend)
                    VALUES (:n, :v, :d, 'mem0') ON CONFLICT (name) DO NOTHING
                    """
                ),
                {
                    "n": collection,
                    "v": self.embeddings.embedding_version,
                    "d": self.embeddings.dim,
                },
            )
        log.info("memory.mem0_ready", collection=collection, dim=self.embeddings.dim)
        return collection

    async def _require_pgvector(self) -> None:
        """Refuse to start without the extension, rather than failing at first write.

        §3.3's dimension error arrives *days later, in a worker*; a missing extension
        arrives at the first insert in exactly the same way. Checking at setup turns
        both into a startup failure naming the package to install.
        """
        async with self.uow_factory() as uow:
            ok = (
                await uow.session.execute(text("SELECT pgvector FROM mem.capabilities LIMIT 1"))
            ).scalar_one_or_none()
        if not ok:
            raise MemoryStoreUnavailable(
                "RUNTIME_MEMORY_STORE=mem0 needs the pgvector extension, which this "
                "database does not have (see mem.capabilities and docs/M3_LIBRARY_FACTS.md "
                "fact 3). Install postgresql-NN-pgvector, or use the native store."
            )

    @property
    def collection(self) -> str:
        if self._collection is None:
            raise MemoryStoreUnavailable("Mem0MemoryStore.setup() has not been called")
        return self._collection

    async def write(
        self,
        *,
        organization_id: OrganizationId,
        scope_key: str,
        facts: Sequence[ExtractedFact],
    ) -> list[StoreEvent]:
        """Hand the facts to Mem0 and translate its events into ours.

        The facts go in as `user` messages rather than as pre-formed memories, which is
        what routes them through the deduplication and fusion path — the part of the
        library this adapter exists to use. Passing them as `infer=False` memories would
        get the pgvector store and none of the reason for the dependency.
        """
        if not facts:
            return []
        _GATEWAYS["organization_id"] = organization_id
        payload = [{"role": "user", "content": f.text} for f in facts]
        by_subject = {f.text: f for f in facts}

        result = await self._memory.add(
            messages=payload,
            agent_id=scope_key,
            metadata={"scope_key": scope_key, "organization_id": str(organization_id)},
        )
        events: list[StoreEvent] = []
        for item in (result or {}).get("results", []):
            kind = str(item.get("event", "ADD")).upper()
            if kind not in ("ADD", "UPDATE", "DELETE", "NOOP"):
                kind = "NOOP"
            body = str(item.get("memory", ""))
            fact = by_subject.get(body) or facts[0]
            events.append(
                StoreEvent(
                    event=kind,  # type: ignore[arg-type]
                    memory_id=MemoryId(str(item.get("id", ""))),
                    text=body,
                    subject_key=fact.subject_key,
                    fact=fact,
                    supersedes=(
                        MemoryId(str(item["previous_id"])) if item.get("previous_id") else None
                    ),
                )
            )
        return events

    async def search(
        self,
        *,
        organization_id: OrganizationId,
        query: str,
        scope_values: list[str],
        top_k: int,
    ) -> list[Candidate]:
        if not scope_values or top_k <= 0:
            return []
        if not isinstance(scope_values, list):  # fact 4: never a bare string
            raise MemoryStoreUnavailable(
                f"scope_values must be a list, got {type(scope_values).__name__}"
            )
        _GATEWAYS["organization_id"] = organization_id
        result = await self._memory.search(
            query=query,
            agent_id=scope_values[0] if len(scope_values) == 1 else None,
            filters={"scope_key": {"in": scope_values}},
            limit=top_k,
        )
        return [
            Candidate(
                memory_id=MemoryId(str(item["id"])),
                text=str(item.get("memory", "")),
                similarity=float(item.get("score", 0.0)),
            )
            for item in (result or {}).get("results", [])
        ]

    async def fetch(self, memory_ids: Sequence[MemoryId]) -> dict[MemoryId, str]:
        out: dict[MemoryId, str] = {}
        for memory_id in memory_ids:
            item = await self._memory.get(memory_id)
            if item:
                out[memory_id] = str(item.get("memory", ""))
        return out

    async def delete(self, memory_id: MemoryId) -> None:
        await self._memory.delete(memory_id)


def mem0_available() -> bool:
    """Is the optional dependency importable? Used to skip its tests, never to fall back.

    Falling back silently from `RUNTIME_MEMORY_STORE=mem0` to the native store would be
    the worst of both: an operator who asked for model-based fusion and pgvector would
    get neither, and nothing would say so.
    """
    try:
        import mem0  # noqa: F401
    except ImportError:
        return False
    return True
