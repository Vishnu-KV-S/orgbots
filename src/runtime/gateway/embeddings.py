"""Embedding gateway.

An embedding call is an external call that costs money and talks to a provider with a
quota, which is the same pair of facts that made M2 govern `ModelGateway` — see that
module's header. So it gets the same pipeline: kill switch, rate limit, budget
reservation, usage ledger, one audit decision row. It is not a `ToolGateway` call
because it mutates nothing and needs no effect journal.

**Why this is a gateway at all, rather than something the memory store does for
itself.** Mem0 constructs its own embedder and calls it directly
(`docs/M3_LIBRARY_FACTS.md` fact 7). Left that way, the highest-frequency model call in
M3 would be the one call in the system with no `work_class`, no budget line and no
audit row — which is I12 violated, and §13 risk 4's *"consolidation cost is invisible
until you look"* made structurally true. `Mem0MemoryStore` registers an adapter that
routes back through here, so there is one accounting path and not two.

**Everything here is `work_class=MEMORY`.** The parameter is still required and still
passed, because a call that hardcoded its own work class would be the one call site
that could not be re-tagged when the taxonomy moves.

`HashingEmbedder` is to this gateway what `EchoProvider` is to `ModelGateway`: the
deterministic default, and the reason the whole test suite runs without a network or a
key. It is a real embedder in the sense that matters here — similar text gets similar
vectors — and it is not a good one. That distinction is why eval 1 and eval 2 have
bars: they are measuring the embedder as much as the retrieval, and swapping in a real
one should move them.
"""

from __future__ import annotations

import hashlib
import itertools
import math
import re
import time
from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from runtime.budget.service import BudgetService
from runtime.domain.enums import AuditSeverity, GatewayDecision, WorkClass
from runtime.domain.errors import ProviderUnavailable, SpecError
from runtime.domain.ids import BudgetPoolId, OrganizationId, RunId
from runtime.gateway.governance import AuditBuffer
from runtime.gateway.ratelimit import RateLimiter
from runtime.observability.logging import get_logger
from runtime.org.killswitch import KillSwitchService
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.settings import Settings, get_settings

log = get_logger("gateway.embeddings")


@dataclass(frozen=True, slots=True)
class EmbeddingResult:
    vectors: list[list[float]]
    model: str
    embedding_version: str
    dim: int
    input_tokens: int
    cost_cents: int
    cached: int = 0
    duration_ms: float = 0.0


class EmbeddingProvider(Protocol):
    name: str
    model: str
    dim: int

    async def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


# --- the deterministic default -----------------------------------------------------

_TOKEN = re.compile(r"[a-z0-9]+")


@dataclass
class HashingEmbedder:
    """Feature hashing over word unigrams and bigrams, L2-normalised.

    Deterministic, offline, free, and stable across processes and Python versions —
    `hashlib.blake2b` rather than `hash()`, which is salted per process and would make
    a vector written by the worker unfindable by the API.

    **What it is good for.** Lexical overlap. Two sentences sharing "competitor
    pricing" land near each other; T48's cross-scope probes and eval 1's recall over
    facts that literally recurred are both lexical questions, so it measures them
    honestly. `docs/M3_SHADOW.md` records which eval numbers were produced under it.

    **What it is not good for.** Paraphrase. "What do they charge" and "pricing" share
    no tokens and land orthogonally. A real embedder is a `RUNTIME_EMBEDDING_*` setting
    away, and eval 2 moving when you turn one on is the expected result rather than a
    surprise.

    Bigrams as well as unigrams because unigram-only feature hashing makes "the report
    rejected the draft" and "the draft rejected the report" identical, and a memory
    store that cannot tell those apart will confidently retrieve the wrong one.
    """

    name: str = "hashing"
    model: str = "hashing-v1"
    dim: int = 256

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._one(t) for t in texts]

    def _one(self, text: str) -> list[float]:
        tokens = _TOKEN.findall(text.lower())
        features = tokens + [f"{a}_{b}" for a, b in itertools.pairwise(tokens)]
        vec = [0.0] * self.dim
        for feature in features:
            digest = hashlib.blake2b(feature.encode(), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "big") % self.dim
            # The sign bit comes from a different part of the digest than the bucket,
            # so collisions cancel as often as they compound. Signed hashing is the
            # standard trick and it is what keeps an unlucky collision from reading as
            # a strong match.
            sign = 1.0 if digest[4] & 1 else -1.0
            vec[index] += sign
        norm = math.sqrt(sum(v * v for v in vec))
        return [v / norm for v in vec] if norm else vec


@dataclass
class HttpEmbedder:
    """A JSON embeddings endpoint: `POST {"input": [...], "model": ...}` →
    `{"data": [{"embedding": [...]}, ...]}`.

    Shaped like `web_search`'s tool in M1 and for the same stated reason: *"defaults to
    unset, and refuses rather than degrades when unset."* An embedder that quietly
    returned zero vectors would produce a memory store where every query matches
    everything equally, which passes every structural test in this milestone and makes
    every number meaningless.
    """

    endpoint_url: str
    api_key: str | None = None
    name: str = "http"
    model: str = "unknown"
    dim: int = 1536
    timeout_s: float = 30.0

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        async with httpx.AsyncClient(timeout=self.timeout_s) as client:
            try:
                response = await client.post(
                    self.endpoint_url,
                    json={"input": list(texts), "model": self.model},
                    headers=headers,
                )
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise ProviderUnavailable(f"embedding endpoint failed: {exc}") from exc
        payload = response.json()
        vectors = [list(map(float, item["embedding"])) for item in payload.get("data", [])]
        if len(vectors) != len(texts):
            raise ProviderUnavailable(
                f"embedding endpoint returned {len(vectors)} vectors for {len(texts)} inputs"
            )
        for vector in vectors:
            if len(vector) != self.dim:
                # Caught here rather than at insert, because at insert it is a pgvector
                # error naming a column, days later, in a worker (§3.3).
                raise ProviderUnavailable(
                    f"embedding endpoint returned dim {len(vector)}, configured for {self.dim}; "
                    "set RUNTIME_EMBEDDING_DIM and use a new RUNTIME_EMBEDDING_VERSION"
                )
        return vectors


# --- cache -------------------------------------------------------------------------


@dataclass
class EmbeddingCache:
    """In-process LRU keyed by `(embedding_version, sha256(text))`. PR-32.

    **In-process, and deliberately not a table.** The honest accounting: retrieval
    queries are nearly all unique, so the read path gets almost nothing from any cache.
    What repeats is the *write* path — consolidation re-embedding the same fact, a
    redelivered outbox event, a backfill after an embedder swap — and those repeat
    inside one worker's lifetime, which an in-process cache covers. A Postgres-backed
    cache would add a round trip to every embed to save a round trip on the few that
    repeat, and the round trip it saves is to a service that is usually faster than the
    database.

    Keyed on `embedding_version` as well as the text, so an embedder swap cannot serve
    a stale vector of the wrong width out of a warm cache — which would turn §3.3's
    loud dimension error into a silent wrong answer.
    """

    max_entries: int = 4096
    _entries: OrderedDict[str, list[float]] = field(default_factory=OrderedDict)
    hits: int = 0
    misses: int = 0

    @staticmethod
    def key(text: str, version: str) -> str:
        return f"{version}:{hashlib.sha256(text.encode()).hexdigest()}"

    def get(self, text: str, version: str) -> list[float] | None:
        k = self.key(text, version)
        vector = self._entries.get(k)
        if vector is None:
            self.misses += 1
            return None
        self._entries.move_to_end(k)
        self.hits += 1
        return vector

    def put(self, text: str, version: str, vector: list[float]) -> None:
        k = self.key(text, version)
        self._entries[k] = vector
        self._entries.move_to_end(k)
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0


# --- the gateway -------------------------------------------------------------------


class EmbeddingGateway:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        *,
        provider: EmbeddingProvider | None = None,
        settings: Settings | None = None,
        budget: BudgetService | None = None,
        kill_switches: KillSwitchService | None = None,
        rate_limiter: RateLimiter | None = None,
        cache: EmbeddingCache | None = None,
    ) -> None:
        self._uow = uow_factory
        self._settings = settings or get_settings()
        self._provider = provider or _provider_from(self._settings)
        self._budget = budget or BudgetService(
            reservation_ttl_seconds=self._settings.reservation_ttl_seconds
        )
        self._kill_switches = kill_switches or KillSwitchService(uow_factory)
        self._rate_limiter = rate_limiter
        self._cache = cache if cache is not None else EmbeddingCache()

    @property
    def dim(self) -> int:
        return self._provider.dim

    @property
    def embedding_version(self) -> str:
        """The string that names the collection (§3.3).

        Provider, model and dimension together, because all three change the vectors
        and any one of them changing alone still means the old collection cannot be
        searched with the new embedder. Overridable, for the case where a provider
        silently reissues a model under the same name — which is the failure this
        string exists to survive.
        """
        return self._settings.embedding_version or (
            f"{self._provider.name}-{self._provider.model}-{self._provider.dim}"
        )

    @property
    def cache(self) -> EmbeddingCache:
        return self._cache

    async def embed(
        self,
        organization_id: OrganizationId,
        texts: Sequence[str],
        *,
        work_class: WorkClass = WorkClass.MEMORY,
        call_site: str,
        run_id: RunId | None = None,
        pool_id: BudgetPoolId | None = None,
        actor_name: str | None = None,
    ) -> EmbeddingResult:
        """Embed a batch. Kill switch, rate limit, budget, ledger, one audit row.

        **No `RunContext`**, unlike `ModelGateway.complete`. The busiest caller is the
        memory worker, which is not executing a run and has no lease, no fence and no
        frozen spec. Requiring a context here would have meant either inventing a fake
        one in the worker — a spec that lies about what is executing — or letting the
        worker call the provider directly, which is the ungoverned path this module
        exists to close. So the pieces that are genuinely needed are named parameters
        and the ones that do not apply are absent.
        """
        audit = AuditBuffer()
        try:
            return await self._embed(
                organization_id,
                texts,
                work_class=work_class,
                call_site=call_site,
                run_id=run_id,
                pool_id=pool_id,
                actor_name=actor_name,
                audit=audit,
            )
        finally:
            await audit.flush(self._uow)

    async def _embed(
        self,
        organization_id: OrganizationId,
        texts: Sequence[str],
        *,
        work_class: WorkClass,
        call_site: str,
        run_id: RunId | None,
        pool_id: BudgetPoolId | None,
        actor_name: str | None,
        audit: AuditBuffer,
    ) -> EmbeddingResult:
        version = self.embedding_version
        if not texts:
            return EmbeddingResult([], self._provider.model, version, self._provider.dim, 0, 0)

        # Cache first, and the ordering matters: a fully-cached batch makes no external
        # call, so it takes no rate-limit token and reserves no budget. Charging for a
        # call that did not happen would make the embedding cache look like it saved
        # nothing, which is how a cost lever gets removed for not working.
        cached: dict[int, list[float]] = {}
        pending: list[tuple[int, str]] = []
        for i, t in enumerate(texts):
            hit = self._cache.get(t, version)
            if hit is None:
                pending.append((i, t))
            else:
                cached[i] = hit

        if not pending:
            return EmbeddingResult(
                vectors=[cached[i] for i in range(len(texts))],
                model=self._provider.model,
                embedding_version=version,
                dim=self._provider.dim,
                input_tokens=0,
                cost_cents=0,
                cached=len(cached),
            )

        stop = await self._kill_switches.check(organization_id, actor=actor_name)
        if stop.stopped:
            self._decide(
                audit,
                organization_id,
                GatewayDecision.DENIED,
                "kill_switch",
                f"{stop.mode.value if stop.mode else '?'} on {stop.scope}: {stop.reason}",
                call_site,
                work_class,
                run_id,
                actor_name,
                severity=AuditSeverity.HIGH,
            )
            stop.raise_if_stopped(f"embedding call from {call_site}")

        if self._rate_limiter is not None:
            rate = await self._rate_limiter.check(
                organization_id, actor=actor_name, provider=self._provider.name
            )
            if not rate.allowed:
                self._decide(
                    audit,
                    organization_id,
                    GatewayDecision.DENIED,
                    "rate_limit",
                    f"{rate.scope}: {rate.reason}",
                    call_site,
                    work_class,
                    run_id,
                    actor_name,
                )
                rate.raise_if_limited(f"embedding call from {call_site}")

        input_tokens = sum(max(1, len(t) // 4) for _, t in pending)
        estimate = (input_tokens * self._settings.embedding_cents_per_mtok) // 1_000_000

        reservation_id = None
        if pool_id is not None and run_id is not None:
            async with self._uow.transaction() as uow:
                reservation_id = await self._budget.reserve(
                    uow, pool_id=pool_id, run_id=run_id, amount_cents=estimate
                )

        started = time.perf_counter()
        try:
            fresh = await self._provider.embed([t for _, t in pending])
        except Exception:
            if reservation_id is not None:
                async with self._uow.transaction() as uow:
                    await self._budget.release(uow, reservation_id)
            raise
        elapsed = (time.perf_counter() - started) * 1000

        for (i, t), vector in zip(pending, fresh, strict=True):
            self._cache.put(t, version, vector)
            cached[i] = vector

        cost = estimate
        async with self._uow.transaction() as uow:
            if reservation_id is not None:
                await self._budget.reconcile(uow, reservation_id, cost)
            # See `ModelGateway._complete_detached`: `usage_ledger.run_id` is NOT NULL,
            # so an embedding with no run to attribute to is carried by its audit row
            # instead of by a fabricated ledger entry.
            if run_id is not None:
                await uow.budget.record_usage(
                    organization_id=organization_id,
                    run_id=run_id,
                    root_run_id=run_id,
                    pool_id=pool_id,
                    reservation_id=reservation_id,
                    kind="embedding",
                    work_class=work_class.value,
                    call_site=call_site,
                    provider=self._provider.name,
                    model=self._provider.model,
                    input_tokens=input_tokens,
                    output_tokens=0,
                    cost_cents=cost,
                )

        self._decide(
            audit,
            organization_id,
            GatewayDecision.ALLOWED,
            "completed",
            None,
            call_site,
            work_class,
            run_id,
            actor_name,
            cost_cents=cost,
        )
        return EmbeddingResult(
            vectors=[cached[i] for i in range(len(texts))],
            model=self._provider.model,
            embedding_version=version,
            dim=self._provider.dim,
            input_tokens=input_tokens,
            cost_cents=cost,
            cached=len(texts) - len(pending),
            duration_ms=elapsed,
        )

    def _decide(
        self,
        audit: AuditBuffer,
        organization_id: OrganizationId,
        decision: GatewayDecision,
        check_name: str,
        reason: str | None,
        call_site: str,
        work_class: WorkClass,
        run_id: RunId | None,
        actor_name: str | None,
        *,
        severity: AuditSeverity = AuditSeverity.LOW,
        cost_cents: int | None = None,
    ) -> None:
        audit.add(
            organization_id=organization_id,
            gateway="embedding",
            subject=f"{self._provider.name}/{self._provider.model}",
            decision=decision,
            check_name=check_name,
            reason=reason,
            severity=severity,
            run_id=run_id,
            root_run_id=run_id,
            actor_name=actor_name,
            cost_cents=cost_cents,
            detail={"work_class": work_class.value, "call_site": call_site},
        )


def _provider_from(settings: Settings) -> EmbeddingProvider:
    """`hashing` unless an endpoint is configured, and it says which in the log.

    Silent fallback is the failure mode being avoided: an operator who configured a
    real embedder, typo'd the setting, and got the hashing one would see a store that
    works and recall numbers that never improve, with nothing anywhere connecting the
    two.
    """
    if settings.embedding_endpoint_url:
        provider: EmbeddingProvider = HttpEmbedder(
            endpoint_url=settings.embedding_endpoint_url,
            api_key=settings.embedding_api_key,
            model=settings.embedding_model,
            dim=settings.embedding_dim,
        )
        log.info("embeddings.provider", provider="http", model=settings.embedding_model)
        return provider
    log.info(
        "embeddings.provider",
        provider="hashing",
        note="no RUNTIME_EMBEDDING_ENDPOINT_URL; retrieval quality is lexical only",
    )
    return HashingEmbedder(dim=settings.embedding_dim if settings.embedding_dim <= 1024 else 256)


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity, defined here so the native store and the tests agree.

    Both embedders return normalised vectors, so this is a dot product in practice.
    It divides anyway: a provider that stops normalising should degrade the ranking
    rather than return similarities above 1, which would silently break every
    `[CHOSEN]` threshold expressed as a fraction.
    """
    if len(a) != len(b):
        raise SpecError(f"cosine over vectors of different width: {len(a)} vs {len(b)}")
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


__all__ = [
    "EmbeddingCache",
    "EmbeddingGateway",
    "EmbeddingProvider",
    "EmbeddingResult",
    "HashingEmbedder",
    "HttpEmbedder",
    "cosine",
]
