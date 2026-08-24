"""The memory store: one port, two adapters.

**Why a port at all**, when §3.1 says *"test theirs before building yours"*: because
pgvector is not installed on a stock Postgres and is absent from this repository's dev
stack (`docs/M3_LIBRARY_FACTS.md` fact 3). M3's three zero-tolerance gates — evals 4, 5
and 9 — are the whole reason the milestone ships in this order, and making them
conditional on an optional system package would have made them decorative. So there is a
default adapter that needs no extension, and the Mem0 adapter is the production path.

**What is not behind the port.** Scope, trust, provenance, promotion state and the
authoritative retrieval filter are all in `memory_metadata` and its repository, for both
adapters. §13 risk 3 is that *"scope filtering is a security boundary implemented by a
third-party library"* — it is not, here, and T48/T49 pass against either adapter because
they are testing the same SQL either way.

**The division of labour.** Extraction is ours: it is a model call, and a model call
that does not go through `ModelGateway` has no `work_class`, no budget line and no audit
row (I3, I4, I12). Fusion — deciding whether a new fact ADDs, UPDATEs, DELETEs or is a
NOOP against what is already there — is the store's, which is the part §3.1 says to try
before writing. `Mem0MemoryStore` hands the facts to `AsyncMemory.add` and lets it
decide; `NativeMemoryStore` fuses on a subject key, which is cruder and deterministic.

`NativeMemoryStore`'s crudeness is worth stating plainly rather than discovering: it
supersedes when two facts share a *subject*, not when they contradict. "Competitor X
charges $10" replacing "Competitor X charges $20" is caught; "the launch slipped" against
"the launch is on track" is caught only if the extractor gave them the same subject. That
is a real limitation and it is why eval 6 is a measurement rather than an assumption.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from sqlalchemy import text

from runtime.domain.enums import MemoryType
from runtime.domain.errors import EmbeddingDimensionMismatch, MemoryStoreUnavailable
from runtime.domain.ids import MemoryId, OrganizationId, new_memory_id
from runtime.gateway.embeddings import EmbeddingGateway
from runtime.observability.logging import get_logger
from runtime.persistence.uow import UnitOfWorkFactory

log = get_logger("memory.store")

StoreEventKind = Literal["ADD", "UPDATE", "DELETE", "NOOP"]


@dataclass(frozen=True, slots=True)
class ExtractedFact:
    """One thing a run turned out to know. The unit extraction emits and fusion consumes.

    **`subject` is the fusion key and it is why extraction returns structure rather than
    sentences.** A model asked for "facts" returns prose, and prose can only be fused by
    another model call. A model asked for `(subject, statement)` returns something a
    deterministic fuser can group on, which is what lets `NativeMemoryStore` supersede
    without a second model call — and what lets a test assert supersession happened
    without asserting anything about a model's judgement.
    """

    subject: str
    statement: str
    memory_type: MemoryType = MemoryType.FACT
    confidence: float = 0.5
    importance: float = 0.5
    entities: tuple[str, ...] = ()

    @property
    def subject_key(self) -> str:
        """Normalised for grouping: lowercase, collapsed whitespace, no punctuation.

        Deliberately aggressive. "Competitor X's pricing" and "competitor x pricing"
        are the same subject and a fuser that treats them as two writes both and
        supersedes neither, which looks exactly like memory working and is memory
        accumulating."""
        return re.sub(r"[^a-z0-9 ]+", "", self.subject.lower()).strip()

    @property
    def text(self) -> str:
        """What is embedded and what is injected.

        Subject and statement together, because the statement alone often loses its
        referent — "raised prices 20% in June" retrieves against a query about pricing
        and then tells the reader nothing about whose prices."""
        return f"{self.subject}: {self.statement}"


@dataclass(frozen=True, slots=True)
class StoreEvent:
    """§3.1's four outcomes, in one shape both adapters report."""

    event: StoreEventKind
    memory_id: MemoryId
    text: str
    subject_key: str
    fact: ExtractedFact
    supersedes: MemoryId | None = None


@dataclass(frozen=True, slots=True)
class Candidate:
    """A similarity hit, before anything has checked whether it may be read."""

    memory_id: MemoryId
    text: str
    similarity: float


class MemoryStore(Protocol):
    backend: str

    @property
    def collection(self) -> str:
        """The collection this store is writing to, named for the embedding version.

        Part of the port rather than an adapter detail, because `memory_metadata`
        records it on every row: "which collection is this memory's vector in" is a
        question the sidecar has to be able to answer after an embedder swap, and it
        cannot ask an adapter it does not know the type of.
        """

    async def setup(self) -> str:
        """Ensure the collection exists and return its name. Idempotent."""

    async def write(
        self,
        *,
        organization_id: OrganizationId,
        scope_key: str,
        facts: Sequence[ExtractedFact],
    ) -> list[StoreEvent]: ...

    async def search(
        self,
        *,
        organization_id: OrganizationId,
        query: str,
        scope_values: list[str],
        top_k: int,
    ) -> list[Candidate]: ...

    async def fetch(self, memory_ids: Sequence[MemoryId]) -> dict[MemoryId, str]: ...

    async def delete(self, memory_id: MemoryId) -> None: ...


# --- native ------------------------------------------------------------------------

_COLLECTION_NAME = re.compile(r"\A[a-z0-9_]{1,48}\Z")


def collection_name_for(embedding_version: str) -> str:
    """§3.3: *"name the collection with the version"*.

    Sanitised to `[a-z0-9_]` because it becomes a table name, and a table name is the
    one place in this codebase where a value is interpolated into SQL rather than
    bound. `_quote` below refuses anything this function would not have produced, so
    the interpolation is over a value the regex has already closed.
    """
    slug = re.sub(r"[^a-z0-9]+", "_", embedding_version.lower()).strip("_")
    return f"m_{slug}"[:48]


def _quote(collection: str) -> str:
    if not _COLLECTION_NAME.match(collection):
        raise MemoryStoreUnavailable(
            f"refusing collection name {collection!r}: it becomes a table identifier and "
            "must match [a-z0-9_]{1,48}"
        )
    return f'mem."{collection}"'


@dataclass
class NativeMemoryStore:
    """Exact cosine over `real[]`, in Postgres, with no extension.

    **The scaling limit, stated up front.** Every search is a sequential scan of the
    rows in the queried scopes, with a dot product per row. At M3 volumes — one
    department, weeks of runs, thousands of memories — that is a few milliseconds and
    the exactness is worth more than the speed: an ANN index has a recall parameter, and
    eval 1 measuring the index's recall alongside the store's would make a bad number
    ambiguous. It stops being the right trade somewhere in the hundred-thousands, which
    is where `memory_store=mem0` and pgvector are.

    **The dimension is enforced from `mem.collections`**, even though `real[]` would
    accept anything. pgvector fixes the width in the column type (§3.3); an adapter that
    quietly accepted a re-dimensioned vector would pass T45 while the production adapter
    failed it, which is worse than not having the test.
    """

    uow_factory: UnitOfWorkFactory
    embeddings: EmbeddingGateway
    backend: str = "native"
    _collection: str | None = field(default=None, repr=False)

    async def setup(self) -> str:
        version = self.embeddings.embedding_version
        collection = collection_name_for(version)
        dim = self.embeddings.dim
        table = _quote(collection)

        async with self.uow_factory.transaction() as uow:
            registered = (
                await uow.session.execute(
                    text("SELECT dim, backend FROM mem.collections WHERE name = :n"),
                    {"n": collection},
                )
            ).one_or_none()
            if registered is not None and int(registered.dim) != dim:
                raise EmbeddingDimensionMismatch(
                    f"collection {collection!r} was created at dim {registered.dim} and the "
                    f"configured embedder produces {dim}. §3.3: an embedder change is a new "
                    f"collection plus a backfill, never an in-place edit — set "
                    f"RUNTIME_EMBEDDING_VERSION to a new value."
                )

            await uow.session.execute(
                text(
                    f"""
                    CREATE TABLE IF NOT EXISTS {table} (
                        memory_id       text PRIMARY KEY,
                        organization_id uuid NOT NULL,
                        scope_key       text NOT NULL,
                        subject_key     text NOT NULL,
                        body            text NOT NULL,
                        embedding       real[] NOT NULL,
                        created_at      timestamptz NOT NULL DEFAULT now()
                    )
                    """
                )
            )
            await uow.session.execute(
                text(
                    f'CREATE INDEX IF NOT EXISTS "ix_{collection}_scope" '
                    f"ON {table} (organization_id, scope_key)"
                )
            )
            await uow.session.execute(
                text(
                    f'CREATE INDEX IF NOT EXISTS "ix_{collection}_subject" '
                    f"ON {table} (organization_id, scope_key, subject_key)"
                )
            )
            await uow.session.execute(
                text(
                    """
                    INSERT INTO mem.collections (name, embedding_version, dim, backend)
                    VALUES (:n, :v, :d, 'native')
                    ON CONFLICT (name) DO NOTHING
                    """
                ),
                {"n": collection, "v": version, "d": dim},
            )
        self._collection = collection
        return collection

    @property
    def collection(self) -> str:
        if self._collection is None:
            raise MemoryStoreUnavailable("NativeMemoryStore.setup() has not been called")
        return self._collection

    async def write(
        self,
        *,
        organization_id: OrganizationId,
        scope_key: str,
        facts: Sequence[ExtractedFact],
    ) -> list[StoreEvent]:
        """Fuse and insert. One transaction for the whole batch.

        The fusion rule, in full: for each fact, look for an existing memory in the same
        `(organization, scope, subject_key)`. Identical statement → `NOOP`, nothing
        written. Different statement → `UPDATE`: the new row is inserted and the event
        names the old one, which `MemoryService` then marks superseded in the sidecar.
        No existing subject → `ADD`.

        `DELETE` is never emitted here. A deterministic fuser has no way to know that a
        fact has ceased to be true as opposed to having been replaced, and guessing
        would silently destroy the pair eval 6 measures. Mem0's model-based fusion does
        emit it, and the sidecar handles it the same way either adapter reports it.
        """
        if not facts:
            return []
        collection = self.collection
        table = _quote(collection)
        result = await self.embeddings.embed(
            organization_id,
            [f.text for f in facts],
            call_site="memory.store.write",
            actor_name=None,
        )
        if result.vectors and len(result.vectors[0]) != self.embeddings.dim:
            raise EmbeddingDimensionMismatch(
                f"embedder returned dim {len(result.vectors[0])} for collection "
                f"{collection!r} at dim {self.embeddings.dim}"
            )

        events: list[StoreEvent] = []
        async with self.uow_factory.transaction() as uow:
            for fact, vector in zip(facts, result.vectors, strict=True):
                existing = (
                    await uow.session.execute(
                        text(
                            f"""
                            SELECT m.memory_id, m.body
                              FROM {table} m
                              JOIN memory_metadata md ON md.memory_id = m.memory_id
                             WHERE m.organization_id = :org AND m.scope_key = :sk
                               AND m.subject_key = :subj AND md.status = 'active'
                             ORDER BY m.created_at DESC LIMIT 1
                            """
                        ),
                        {"org": organization_id, "sk": scope_key, "subj": fact.subject_key},
                    )
                ).one_or_none()

                if existing is not None and existing.body == fact.text:
                    events.append(
                        StoreEvent(
                            "NOOP", MemoryId(existing.memory_id), fact.text, fact.subject_key, fact
                        )
                    )
                    continue

                memory_id = new_memory_id()
                await uow.session.execute(
                    text(
                        f"""
                        INSERT INTO {table} (memory_id, organization_id, scope_key,
                                             subject_key, body, embedding)
                        VALUES (:mid, :org, :sk, :subj, :body, CAST(:emb AS real[]))
                        """
                    ),
                    {
                        "mid": memory_id,
                        "org": organization_id,
                        "sk": scope_key,
                        "subj": fact.subject_key,
                        "body": fact.text,
                        "emb": vector,
                    },
                )
                events.append(
                    StoreEvent(
                        "UPDATE" if existing is not None else "ADD",
                        memory_id,
                        fact.text,
                        fact.subject_key,
                        fact,
                        supersedes=MemoryId(existing.memory_id) if existing is not None else None,
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
        """Exact cosine, scope-prefiltered, ordered, in one statement.

        `scope_values` is typed `list[str]` and bound as an array, which is the
        mechanical form of §3.4's lesson: the library bug that started this was a
        filter builder iterating a *string* into a one-element-per-character array, and
        a bound `ANY(:scopes)` has no way to do that.

        This filter is a prefilter, not the boundary. `MemoryMetadataRepository.authorize`
        is the boundary and it runs over whatever this returns.
        """
        if not scope_values or top_k <= 0:
            return []
        if not isinstance(scope_values, list):  # pragma: no cover - typed, but §3.4
            raise MemoryStoreUnavailable(
                f"scope_values must be a list, got {type(scope_values).__name__}"
            )
        table = _quote(self.collection)
        result = await self.embeddings.embed(
            organization_id, [query], call_site="memory.store.search", actor_name=None
        )
        vector = result.vectors[0]

        async with self.uow_factory() as uow:
            rows = (
                await uow.session.execute(
                    text(
                        f"""
                        SELECT m.memory_id, m.body, s.sim
                          FROM {table} m
                          CROSS JOIN LATERAL (
                              SELECT coalesce(sum(a::float8 * b::float8), 0.0) AS sim
                                FROM unnest(m.embedding, CAST(:q AS real[])) AS t(a, b)
                          ) s
                         WHERE m.organization_id = :org
                           AND m.scope_key = ANY(:scopes)
                         ORDER BY s.sim DESC
                         LIMIT :k
                        """
                    ),
                    {
                        "q": vector,
                        "org": organization_id,
                        "scopes": scope_values,
                        "k": top_k,
                    },
                )
            ).all()
        return [Candidate(MemoryId(r.memory_id), r.body, float(r.sim)) for r in rows]

    async def fetch(self, memory_ids: Sequence[MemoryId]) -> dict[MemoryId, str]:
        if not memory_ids:
            return {}
        table = _quote(self.collection)
        async with self.uow_factory() as uow:
            rows = (
                await uow.session.execute(
                    text(f"SELECT memory_id, body FROM {table} WHERE memory_id = ANY(:ids)"),
                    {"ids": list(memory_ids)},
                )
            ).all()
        return {MemoryId(r.memory_id): r.body for r in rows}

    async def delete(self, memory_id: MemoryId) -> None:
        table = _quote(self.collection)
        async with self.uow_factory.transaction() as uow:
            await uow.session.execute(
                text(f"DELETE FROM {table} WHERE memory_id = :mid"), {"mid": memory_id}
            )

    async def backfill_from(
        self, source: NativeMemoryStore, *, organization_id: OrganizationId, batch: int = 200
    ) -> int:
        """Re-embed another collection's rows into this one. T45's second half.

        §3.3: *"treat an embedder change as a new collection plus backfill, never an
        in-place edit."* This is the backfill, and the property T45 checks is what it
        does **not** touch: the source collection is read-only here, so a backfill that
        fails halfway leaves the old collection serving every retrieval exactly as it
        did before.

        Memory ids are preserved. The sidecar is keyed on them and the whole provenance
        chain — promotions, entity links, audit rows — hangs off that key, so a backfill
        that minted new ids would be a migration of the store and an amnesia of the
        policy.
        """
        source_table = _quote(source.collection)
        target_table = _quote(self.collection)
        moved = 0
        while True:
            async with self.uow_factory() as uow:
                rows = (
                    await uow.session.execute(
                        text(
                            f"""
                            SELECT s.memory_id, s.scope_key, s.subject_key, s.body
                              FROM {source_table} s
                             WHERE s.organization_id = :org
                               AND NOT EXISTS (
                                   SELECT 1 FROM {target_table} t WHERE t.memory_id = s.memory_id
                               )
                             ORDER BY s.created_at LIMIT :lim
                            """
                        ),
                        {"org": organization_id, "lim": batch},
                    )
                ).all()
            if not rows:
                return moved

            result = await self.embeddings.embed(
                organization_id, [r.body for r in rows], call_site="memory.store.backfill"
            )
            async with self.uow_factory.transaction() as uow:
                for row, vector in zip(rows, result.vectors, strict=True):
                    await uow.session.execute(
                        text(
                            f"""
                            INSERT INTO {target_table} (memory_id, organization_id, scope_key,
                                                        subject_key, body, embedding)
                            VALUES (:mid, :org, :sk, :subj, :body, CAST(:emb AS real[]))
                            ON CONFLICT (memory_id) DO NOTHING
                            """
                        ),
                        {
                            "mid": row.memory_id,
                            "org": organization_id,
                            "sk": row.scope_key,
                            "subj": row.subject_key,
                            "body": row.body,
                            "emb": vector,
                        },
                    )
                await uow.session.execute(
                    text(
                        "UPDATE memory_metadata SET embedding_version = :v, collection = :c "
                        "WHERE memory_id = ANY(:ids)"
                    ),
                    {
                        "v": self.embeddings.embedding_version,
                        "c": self.collection,
                        "ids": [r.memory_id for r in rows],
                    },
                )
            moved += len(rows)


async def capabilities(uow_factory: UnitOfWorkFactory) -> dict[str, Any]:
    """What migration 023 found out about this database. Read at startup, logged once."""
    async with uow_factory() as uow:
        row = (
            await uow.session.execute(
                text("SELECT pgvector, pgvector_version FROM mem.capabilities LIMIT 1")
            )
        ).one_or_none()
    if row is None:
        return {"pgvector": False, "pgvector_version": None}
    return {"pgvector": bool(row.pgvector), "pgvector_version": row.pgvector_version}
