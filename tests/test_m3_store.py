"""The store, the embedding gateway, the cache, and the Mem0 adapter's contract.

Four groups, and the fourth is the unusual one.

**The store port.** Search returns candidates ordered by similarity, scope-prefiltered,
and the prefilter is not the boundary.

**The embedding gateway.** Every M3 embedding is an external call, so it carries a
`work_class` and writes an audit row — I12 and §13 risk 4. The cache is measured on the
axis it was built for: repeated writes, not repeated queries.

**Reranking.** A pure function, tested as one, including the two decisions in it that
are not obvious: additive rather than multiplicative, and a deterministic tie-break.

**The Mem0 adapter, without Mem0 installed.** `mem0ai` is an optional extra and CI does
not have it, so what is tested is everything about the adapter that does not need the
library to be importable: that it refuses rather than silently falling back, that
telemetry is disabled before the import rather than after, and that the loop bridge
asserts fact 2 rather than assuming it. Those are the three things that would fail
*quietly* in production, which makes them exactly the ones worth holding without the
dependency present.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text

from runtime.domain.enums import MemoryScope, MemoryStatus, MemoryTrust, MemoryType, WorkClass
from runtime.domain.errors import MemoryStoreUnavailable
from runtime.domain.ids import MemoryId, OrganizationId
from runtime.domain.memory import (
    InjectionBudget,
    MemoryRecord,
    RerankWeights,
    RetrievedMemory,
    estimate_tokens,
    rerank,
)
from runtime.gateway.embeddings import (
    EmbeddingCache,
    EmbeddingGateway,
    HashingEmbedder,
    cosine,
)
from runtime.memory.store import _quote, capabilities, collection_name_for
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.settings import Settings
from tests.conftest_m3 import build_memory_harness, seed_org

pytestmark = pytest.mark.integration

ACTOR = uuid.UUID("00000000-0000-0000-0000-00000000ab01")


# --- the store ---------------------------------------------------------------------


async def test_search_orders_by_similarity_and_prefilters_by_scope(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    org = OrganizationId(uuid.uuid4())
    await seed_org(uow_factory, org)
    harness = await build_memory_harness(uow_factory, settings)
    await harness.write(
        org,
        scope=MemoryScope.PRIVATE_ACTOR,
        scope_id=ACTOR,
        facts=[
            ("Acme pricing", "Acme charges $49 per seat per month"),
            ("staffing", "the team hired two designers in March"),
        ],
    )
    other = uuid.uuid4()
    await harness.write(
        org,
        scope=MemoryScope.PRIVATE_ACTOR,
        scope_id=other,
        facts=[("Acme pricing", "Acme charges $49 per seat per month")],
    )

    hits = await harness.store.search(
        organization_id=org,
        query="Acme pricing per seat per month",
        scope_values=[f"private:{ACTOR}"],
        top_k=10,
    )
    assert len(hits) == 2, "the prefilter let another scope's rows through"
    assert "Acme" in hits[0].text
    assert hits[0].similarity >= hits[1].similarity


async def test_search_with_no_scopes_returns_nothing_rather_than_everything(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """The same ambiguity `ScopeFilter` refuses, at the other end of the port."""
    org = OrganizationId(uuid.uuid4())
    await seed_org(uow_factory, org)
    harness = await build_memory_harness(uow_factory, settings)
    await harness.write(
        org, scope=MemoryScope.PRIVATE_ACTOR, scope_id=ACTOR, facts=[("s", "a fact")]
    )
    assert (
        await harness.store.search(organization_id=org, query="a fact", scope_values=[], top_k=5)
        == []
    )


def test_a_collection_name_that_is_not_an_identifier_is_refused() -> None:
    """The one place in this codebase where a value is interpolated into SQL rather than
    bound — a table name cannot be a parameter. `collection_name_for` sanitises and
    `_quote` refuses anything it would not have produced, so the interpolation is over a
    value a regex has already closed."""
    for hostile in ['a"; DROP TABLE runs; --', "m_" + "x" * 60, "Mixed-Case", ""]:
        with pytest.raises(MemoryStoreUnavailable, match="table identifier"):
            _quote(hostile)
    assert _quote(collection_name_for("openai/text-embedding-3-large")).startswith('mem."m_')


async def test_capabilities_records_whether_pgvector_is_present(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """§3.3. An operator who expected pgvector and got the native store finds out from a
    table rather than from a latency graph six weeks later."""
    caps = await capabilities(uow_factory)
    assert set(caps) == {"pgvector", "pgvector_version"}
    assert isinstance(caps["pgvector"], bool)


async def test_a_noop_does_not_grow_the_collection(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    org = OrganizationId(uuid.uuid4())
    await seed_org(uow_factory, org)
    harness = await build_memory_harness(uow_factory, settings)
    fact = [("Acme pricing", "Acme charges $49")]
    await harness.write(org, scope=MemoryScope.PRIVATE_ACTOR, scope_id=ACTOR, facts=fact)
    await harness.write(org, scope=MemoryScope.PRIVATE_ACTOR, scope_id=ACTOR, facts=fact)

    async with uow_factory() as uow:
        rows = (
            await uow.session.execute(
                text(f'SELECT count(*) FROM mem."{harness.store.collection}"')
            )
        ).scalar_one()
    assert rows == 1


# --- the embedding gateway ---------------------------------------------------------


async def test_every_embedding_writes_an_audit_row_with_a_work_class(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """I12 and §13 risk 4. Left to itself Mem0 makes these calls with no work class, no
    budget line and no audit row — which is that risk made structurally true."""
    org = OrganizationId(uuid.uuid4())
    await seed_org(uow_factory, org)
    gateway = EmbeddingGateway(uow_factory, provider=HashingEmbedder(dim=64), settings=settings)
    result = await gateway.embed(org, ["hello world"], call_site="test.embed")
    assert len(result.vectors) == 1 and len(result.vectors[0]) == 64

    async with uow_factory() as uow:
        row = (
            await uow.session.execute(
                text(
                    "SELECT gateway, decision, detail FROM audit_logs "
                    "WHERE organization_id = :o AND gateway = 'embedding'"
                ),
                {"o": org},
            )
        ).one()
    assert row.decision == "allowed"
    assert row.detail["work_class"] == WorkClass.MEMORY.value
    assert row.detail["call_site"] == "test.embed"


async def test_a_fully_cached_batch_makes_no_call_and_charges_nothing(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """Charging for a call that did not happen would make the cache look like it saved
    nothing, which is how a cost lever gets removed for not working."""
    org = OrganizationId(uuid.uuid4())
    await seed_org(uow_factory, org)
    gateway = EmbeddingGateway(uow_factory, provider=HashingEmbedder(dim=64), settings=settings)

    await gateway.embed(org, ["repeatable text"], call_site="test.embed")
    second = await gateway.embed(org, ["repeatable text"], call_site="test.embed")

    assert second.cached == 1
    assert second.input_tokens == 0
    assert second.cost_cents == 0
    assert gateway.cache.hit_rate > 0

    async with uow_factory() as uow:
        rows = (
            await uow.session.execute(
                text(
                    "SELECT count(*) FROM audit_logs WHERE organization_id = :o "
                    "AND gateway = 'embedding'"
                ),
                {"o": org},
            )
        ).scalar_one()
    assert rows == 1, "a fully cached batch wrote a second audit row for a call it did not make"


def test_the_cache_is_keyed_on_the_embedding_version() -> None:
    """An embedder swap must not serve a stale vector of the wrong width out of a warm
    cache — which would turn §3.3's loud dimension error into a silent wrong answer."""
    cache = EmbeddingCache()
    cache.put("text", "v1", [1.0, 0.0])
    assert cache.get("text", "v1") == [1.0, 0.0]
    assert cache.get("text", "v2") is None


def test_the_cache_evicts_least_recently_used() -> None:
    cache = EmbeddingCache(max_entries=2)
    cache.put("a", "v", [1.0])
    cache.put("b", "v", [2.0])
    cache.get("a", "v")
    cache.put("c", "v", [3.0])
    assert cache.get("b", "v") is None
    assert cache.get("a", "v") == [1.0]


async def test_the_hashing_embedder_is_stable_across_calls_and_distinguishes_word_order(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """Two properties the retrieval tests rest on.

    *Stable*, because `blake2b` rather than `hash()` — which is salted per process, so a
    vector written by the worker would be unfindable by the API.

    *Order-sensitive*, because unigram-only feature hashing makes "the report rejected
    the draft" and "the draft rejected the report" identical, and a store that cannot
    tell those apart will confidently retrieve the wrong one.
    """
    embedder = HashingEmbedder(dim=256)
    first = (await embedder.embed(["the report rejected the draft"]))[0]
    again = (await embedder.embed(["the report rejected the draft"]))[0]
    reversed_ = (await embedder.embed(["the draft rejected the report"]))[0]

    assert first == again
    assert cosine(first, again) == pytest.approx(1.0)
    assert cosine(first, reversed_) < 0.95


# --- reranking ---------------------------------------------------------------------


def _candidate(similarity: float, *, importance: float, age: float, access: int) -> RetrievedMemory:
    return RetrievedMemory(
        memory_id=MemoryId(f"m-{similarity}-{importance}-{age}-{access}"),
        text="x",
        similarity=similarity,
        record=MemoryRecord(
            memory_id=MemoryId("m"),
            organization_id=uuid.uuid4(),
            scope=MemoryScope.PRIVATE_ACTOR,
            scope_id=ACTOR,
            memory_type=MemoryType.FACT,
            trust=MemoryTrust.TRUSTED,
            embedding_version="v",
            status=MemoryStatus.ACTIVE,
            importance=importance,
            access_count=access,
            age_days=age,
        ),
    )


def test_rerank_does_not_annihilate_a_never_read_memory() -> None:
    """The reason the score is additive despite §7's "recency x importance x
    similarity x access decay": a product makes any single zero term annihilate the
    row, and `access_count = 0` is the state every new memory starts in."""
    fresh = _candidate(0.9, importance=0.9, age=0.0, access=0)
    ranked = rerank([fresh], weights=RerankWeights(), top_k=5)
    assert ranked[0].score > 0


def test_rerank_prefers_recent_important_and_read_over_a_bare_similarity_match() -> None:
    stale = _candidate(0.80, importance=0.1, age=365.0, access=0)
    good = _candidate(0.75, importance=0.9, age=0.0, access=10)
    ranked = rerank([stale, good], weights=RerankWeights(), top_k=2)
    assert ranked[0].memory_id == good.memory_id


def test_rerank_ties_break_deterministically() -> None:
    """A retrieval whose output depends on the order the store happened to return rows
    is not comparable between the attempt that crashed and the attempt that resumes —
    and shadow-mode traces would be measuring the store's stability rather than ours."""
    a = _candidate(0.5, importance=0.5, age=1.0, access=1)
    b = _candidate(0.5, importance=0.5, age=1.0, access=1)
    object.__setattr__(a, "memory_id", MemoryId("aaa"))
    object.__setattr__(b, "memory_id", MemoryId("bbb"))
    assert [m.memory_id for m in rerank([b, a], top_k=2)] == ["aaa", "bbb"]
    assert [m.memory_id for m in rerank([a, b], top_k=2)] == ["aaa", "bbb"]


def test_rerank_assigns_ranks_and_truncates() -> None:
    candidates = [_candidate(0.9 - i / 10, importance=0.5, age=0.0, access=0) for i in range(5)]
    ranked = rerank(candidates, top_k=3)
    assert [m.rank for m in ranked] == [0, 1, 2]


def test_the_injection_budget_caps_both_count_and_tokens() -> None:
    budget = InjectionBudget(max_memories=2, max_tokens=10)
    assert budget.fits(0, 5, 0) is True
    assert budget.fits(6, 5, 1) is False, "the token cap did not bind"
    assert budget.fits(0, 1, 2) is False, "the count cap did not bind"


def test_token_estimation_matches_the_rest_of_the_codebase() -> None:
    """Four characters to a token, the same estimator `_estimate_cents` uses. Two
    estimators would make eval 8's comparison against the M2 baseline a measurement of
    the estimators."""
    assert estimate_tokens("a" * 400) == 100
    assert estimate_tokens("") == 1


# --- the Mem0 adapter, without Mem0 -------------------------------------------------


def test_the_mem0_adapter_refuses_rather_than_falling_back() -> None:
    """Falling back silently from `RUNTIME_MEMORY_STORE=mem0` to the native store would
    be the worst of both: an operator who asked for model-based fusion and pgvector
    would get neither, and nothing would say so."""
    from runtime.memory import mem0_store

    if mem0_store.mem0_available():
        pytest.skip("mem0ai is installed; the refusal path is unreachable here")
    with pytest.raises(MemoryStoreUnavailable, match="mem0ai is not installed"):
        mem0_store._register_providers()


def test_telemetry_is_disabled_before_the_import() -> None:
    """Fact 5. `mem0/memory/telemetry.py` reads `MEM0_TELEMETRY` at *import* time and,
    with it on, `AsyncMemory.__init__` builds a second vector store against a
    `mem0migrations` collection — an extra table in your database. Setting the variable
    after importing is too late."""
    from runtime.memory import mem0_store

    previous = os.environ.get("MEM0_TELEMETRY")
    try:
        os.environ.pop("MEM0_TELEMETRY", None)
        mem0_store._disable_telemetry()
        assert os.environ["MEM0_TELEMETRY"] == "False"
    finally:
        if previous is None:
            os.environ.pop("MEM0_TELEMETRY", None)
        else:
            os.environ["MEM0_TELEMETRY"] = previous


async def test_the_loop_bridge_refuses_to_run_on_the_loop_thread() -> None:
    """Fact 2 as an assertion rather than an assumption.

    `AsyncMemory` wraps every synchronous call in `asyncio.to_thread`, so the adapters
    run on a worker thread and `run_coroutine_threadsafe` cannot deadlock. If a future
    release calls the LLM directly from the loop, this raises with the fact number in
    the message instead of hanging.
    """
    import asyncio

    from runtime.memory import mem0_store

    mem0_store._LOOP = asyncio.get_running_loop()
    try:

        async def noop() -> int:
            return 1

        coro = noop()
        with pytest.raises(MemoryStoreUnavailable, match=r"fact 2|Fact 2|M3_LIBRARY_FACTS"):
            mem0_store._bridge(coro)
        coro.close()
    finally:
        mem0_store._LOOP = None


def test_the_gateway_adapters_refuse_without_a_bound_context() -> None:
    """An adapter with no gateway bound must raise rather than construct a client of its
    own — which is the failure the whole registration dance exists to prevent."""
    from runtime.memory.mem0_store import GatewayEmbedder, GatewayLLM

    with pytest.raises(MemoryStoreUnavailable, match="not bound"):
        GatewayLLM().generate_response([{"role": "user", "content": "hi"}])
    with pytest.raises(MemoryStoreUnavailable, match="not bound"):
        GatewayEmbedder().embed_batch(["hi"])
