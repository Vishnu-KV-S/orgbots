"""The memory sidecar, the trace log, and the four small tables around them.

Six repositories, all bound to one session by `UnitOfWork` like every other.

**`MemoryMetadataRepository.authorize` is the isolation boundary**, and it is the
reason this module exists at all rather than the store owning its own metadata. §13
risk 3: *"Scope filtering is a security boundary implemented by a third-party
library."* It is not, here. The store's filter is a hint; this is the answer. A
regression in the library costs recall, not isolation, and T48/T49 hold against both
adapters because they are testing this SQL either way.

Everything here is parameterised. There is no string interpolation of a scope value
anywhere in this file, which is the mechanical form of §3.4's lesson: the two library
bugs were both a filter that matched something other than what was asked for, and both
would have been impossible against a bound parameter.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import CursorResult, text
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.domain.enums import (
    MemoryScope,
    MemoryStatus,
    MemoryTrust,
    MemoryType,
    PromotionStatus,
    RetrievalGrade,
)
from runtime.domain.ids import (
    ContextTraceId,
    EntityId,
    IntentionId,
    MemoryId,
    OrganizationId,
    ProcedureCandidateId,
    PromotionId,
    RunId,
)
from runtime.domain.memory import MemoryRecord, ScopeFilter

_RECORD_COLUMNS = """
    memory_id, organization_id, scope, scope_id, memory_type, trust,
    embedding_version, collection, status, source_run_id, source_actor_id,
    source_actor_version, source_artifact_id,
    coalesce(confidence, 0.5) AS confidence,
    coalesce(importance, 0.5) AS importance,
    access_count, supersedes, superseded_by,
    EXTRACT(EPOCH FROM (now() - created_at)) / 86400.0 AS age_days
"""


def rowcount(result: Any) -> int:
    """`rowcount` off a DML statement, typed.

    `AsyncSession.execute` is annotated as returning `Result`, which has no
    `rowcount`; what it actually returns for an UPDATE is a `CursorResult`, which does.
    One cast here rather than six at the call sites, and rather than six
    `# type: ignore` comments — which would also silence the next genuine mistake.
    """
    return int(cast("CursorResult[Any]", result).rowcount)


def _record(row: Any) -> MemoryRecord:
    return MemoryRecord(
        memory_id=MemoryId(row.memory_id),
        organization_id=row.organization_id,
        scope=MemoryScope(row.scope),
        scope_id=row.scope_id,
        memory_type=MemoryType(row.memory_type),
        trust=MemoryTrust(row.trust),
        embedding_version=row.embedding_version,
        status=MemoryStatus(row.status),
        source_run_id=row.source_run_id,
        source_actor_version=row.source_actor_version,
        source_artifact_id=row.source_artifact_id,
        confidence=float(row.confidence),
        importance=float(row.importance),
        access_count=int(row.access_count),
        age_days=float(row.age_days),
        supersedes=MemoryId(row.supersedes) if row.supersedes else None,
        superseded_by=MemoryId(row.superseded_by) if row.superseded_by else None,
    )


class MemoryMetadataRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def insert(
        self,
        *,
        memory_id: MemoryId,
        organization_id: OrganizationId,
        scope: MemoryScope,
        scope_id: uuid.UUID,
        memory_type: MemoryType,
        trust: MemoryTrust,
        status: MemoryStatus,
        embedding_version: str,
        collection: str,
        source_run_id: uuid.UUID | None = None,
        source_actor_id: uuid.UUID | None = None,
        source_actor_version: int | None = None,
        source_artifact_id: uuid.UUID | None = None,
        confidence: float = 0.5,
        importance: float = 0.5,
        supersedes: MemoryId | None = None,
    ) -> None:
        """Write the sidecar row for a memory the store has just created.

        `ON CONFLICT DO NOTHING` on the memory id, because the write path runs in a
        worker that can be redelivered: an outbox event processed twice must produce
        one sidecar row and not an integrity error that looks like a bug.
        """
        await self._s.execute(
            text(
                """
                INSERT INTO memory_metadata (
                    memory_id, organization_id, scope, scope_id, scope_key, memory_type,
                    trust, status, embedding_version, collection, source_run_id,
                    source_actor_id, source_actor_version, source_artifact_id,
                    confidence, importance, supersedes)
                VALUES (:mid, :org, :scope, :scope_id, :scope_key, :mtype, :trust, :status,
                        :ev, :coll, :run, :actor, :aver, :artifact, :conf, :imp, :sup)
                ON CONFLICT (memory_id) DO NOTHING
                """
            ),
            {
                "mid": memory_id,
                "org": organization_id,
                "scope": scope.value,
                "scope_id": scope_id,
                "scope_key": f"{scope.value}:{scope_id}",
                "mtype": memory_type.value,
                "trust": trust.value,
                "status": status.value,
                "ev": embedding_version,
                "coll": collection,
                "run": source_run_id,
                "actor": source_actor_id,
                "aver": source_actor_version,
                "artifact": source_artifact_id,
                "conf": confidence,
                "imp": importance,
                "sup": supersedes,
            },
        )

    async def authorize(
        self, scope_filter: ScopeFilter, memory_ids: Sequence[MemoryId]
    ) -> dict[MemoryId, MemoryRecord]:
        """**The isolation boundary.** Which of these candidates may this filter read?

        Called with whatever the vector store returned and returns a subset — never a
        superset, never a reordering, never a repair. A candidate the store surfaced
        that this query does not return is dropped silently, which is the correct
        handling: the store answered a question about similarity, not a question about
        permission.

        Four predicates, all in one statement (§7: *"in the query, not after"*):

        1. the organization, always;
        2. `scope_key` in the run's own scope list, bound as an array — never
           interpolated, and never a bare string (§3.4);
        3. `status` in the requested set, which is `{active}` for every ordinary read;
        4. quarantine, which is *excluded* unless `include_quarantined_for_actor`
           names the actor that produced it (§6). That parameter is set from the run
           context and never from anything a query supplies, which is what makes T50
           a property rather than a convention.

        An empty `memory_ids` short-circuits without a query. Not an optimisation: an
        `IN ()` against an empty array is a valid query that matches nothing, but an
        empty *scope* list is not — and `ScopeFilter` has already refused that case at
        construction, so the two empties cannot be confused here.
        """
        if not memory_ids:
            return {}
        scope_filter.validate()

        rows = (
            await self._s.execute(
                text(
                    f"""
                    SELECT {_RECORD_COLUMNS}
                      FROM memory_metadata
                     WHERE organization_id = :org
                       AND memory_id = ANY(:ids)
                       AND scope_key = ANY(:scopes)
                       AND status = ANY(:statuses)
                       AND (
                             trust <> 'UNTRUSTED_QUARANTINE'
                             -- Cast, because the usual case binds NULL here and
                             -- Postgres cannot infer a type for a bare NULL parameter
                             -- used only in `IS NOT NULL`. Without it the query is an
                             -- AmbiguousParameter error rather than a filter — which
                             -- fails closed, but fails the whole retrieval rather than
                             -- excluding the quarantined rows.
                             OR (CAST(:quarantine_actor AS uuid) IS NOT NULL
                                 AND source_actor_id = CAST(:quarantine_actor AS uuid))
                           )
                    """
                ),
                {
                    "org": scope_filter.organization_id,
                    "ids": list(memory_ids),
                    "scopes": scope_filter.scope_values(),
                    "statuses": [s.value for s in scope_filter.statuses],
                    "quarantine_actor": scope_filter.include_quarantined_for_actor,
                },
            )
        ).all()
        records = {MemoryId(r.memory_id): _record(r) for r in rows}
        if scope_filter.memory_types is not None:
            wanted = scope_filter.memory_types
            records = {k: v for k, v in records.items() if v.memory_type in wanted}
        return records

    async def get(self, memory_id: MemoryId) -> MemoryRecord | None:
        row = (
            await self._s.execute(
                text(f"SELECT {_RECORD_COLUMNS} FROM memory_metadata WHERE memory_id = :mid"),
                {"mid": memory_id},
            )
        ).one_or_none()
        return _record(row) if row is not None else None

    async def in_scope(
        self,
        organization_id: OrganizationId,
        *,
        scope: MemoryScope,
        scope_id: uuid.UUID,
        statuses: Sequence[MemoryStatus] = (MemoryStatus.ACTIVE,),
        limit: int = 500,
    ) -> list[MemoryRecord]:
        rows = (
            await self._s.execute(
                text(
                    f"""
                    SELECT {_RECORD_COLUMNS}
                      FROM memory_metadata
                     WHERE organization_id = :org AND scope = :scope AND scope_id = :sid
                       AND status = ANY(:statuses)
                     ORDER BY created_at DESC LIMIT :lim
                    """
                ),
                {
                    "org": organization_id,
                    "scope": scope.value,
                    "sid": scope_id,
                    "statuses": [s.value for s in statuses],
                    "lim": limit,
                },
            )
        ).all()
        return [_record(r) for r in rows]

    async def touch_access(self, memory_ids: Sequence[MemoryId]) -> None:
        """Record that these memories were injected. One statement for the batch.

        Only the *injected* ones, never the retrieved-and-dropped ones: `access_count`
        feeds the rerank's access term, and counting a candidate that lost would make
        the rerank reward memories for being nearly chosen, which compounds into
        exactly the "store develops a favourite" failure `RerankWeights` warns about.

        Not written in shadow mode at all — see `ContextPlanner`. Shadow mode must not
        perturb the store it is measuring.
        """
        if not memory_ids:
            return
        await self._s.execute(
            text(
                """
                UPDATE memory_metadata
                   SET access_count = access_count + 1, last_accessed = now()
                 WHERE memory_id = ANY(:ids)
                """
            ),
            {"ids": list(memory_ids)},
        )

    async def supersede(self, *, old: MemoryId, new: MemoryId) -> bool:
        """Mark `old` superseded by `new`. Idempotent; `False` if it was not active.

        Both directions are written — `superseded_by` on the old row, `supersedes` on
        the new — because the two questions ("what replaced this" and "what did this
        replace") are asked by different readers and a single-direction link makes one
        of them a scan.
        """
        result = await self._s.execute(
            text(
                """
                UPDATE memory_metadata
                   SET status = 'superseded', superseded_by = :new
                 WHERE memory_id = :old AND status = 'active'
                """
            ),
            {"old": old, "new": new},
        )
        if rowcount(result) == 0:
            return False
        await self._s.execute(
            text("UPDATE memory_metadata SET supersedes = :old WHERE memory_id = :new"),
            {"old": old, "new": new},
        )
        return True

    async def set_status(self, memory_id: MemoryId, status: MemoryStatus) -> None:
        await self._s.execute(
            text("UPDATE memory_metadata SET status = :st WHERE memory_id = :mid"),
            {"mid": memory_id, "st": status.value},
        )

    async def release_quarantine(
        self, memory_id: MemoryId, *, trust: MemoryTrust = MemoryTrust.DERIVED
    ) -> None:
        """A human reviewed a quarantined memory and let it out.

        `DERIVED` rather than `TRUSTED` by default, and that is a considered default:
        a fact that came out of a run with untrusted material in its context has been
        *cleared*, not *vouched for*, and the two should not be indistinguishable in
        the table afterwards. A reviewer who genuinely wants TRUSTED passes it.
        """
        await self._s.execute(
            text(
                """
                UPDATE memory_metadata
                   SET trust = :trust, status = 'active'
                 WHERE memory_id = :mid AND status = 'quarantined'
                """
            ),
            {"mid": memory_id, "trust": trust.value},
        )

    async def move_scope(
        self, memory_id: MemoryId, *, scope: MemoryScope, scope_id: uuid.UUID
    ) -> None:
        """Widen a memory's scope. **Called only by `PromotionService.apply`.**

        There is deliberately no guard in here refusing an un-reviewed call, because a
        guard in the repository would be a second place the rule lives and the two
        would drift. The rule lives in the database — `ck_promotion_decided_has_reviewer`
        — and in the fact that this method has exactly one caller, which T53 asserts by
        reading the source.
        """
        await self._s.execute(
            text(
                """
                UPDATE memory_metadata
                   SET scope = :scope, scope_id = :sid, scope_key = :skey
                 WHERE memory_id = :mid
                """
            ),
            {
                "mid": memory_id,
                "scope": scope.value,
                "sid": scope_id,
                "skey": f"{scope.value}:{scope_id}",
            },
        )

    async def decay_and_prune(
        self,
        organization_id: OrganizationId,
        *,
        older_than_days: float,
        max_access_count: int = 0,
        limit: int = 500,
    ) -> int:
        """§13 risk 2's response: retire what is old and has never been read.

        Retired, not deleted. A retired memory is out of the hot index and out of every
        retrieval, and it is still there when someone asks why precision moved in
        week nine. Deleting would make the pruning itself unmeasurable, which is the
        mistake this whole milestone is arranged around not making.
        """
        result = await self._s.execute(
            text(
                """
                UPDATE memory_metadata
                   SET status = 'retired'
                 WHERE memory_id IN (
                     SELECT memory_id FROM memory_metadata
                      WHERE organization_id = :org
                        AND status = 'active'
                        AND access_count <= :max_access
                        AND created_at < now() - make_interval(days => :days)
                      ORDER BY created_at LIMIT :lim
                 )
                """
            ),
            {
                "org": organization_id,
                "max_access": max_access_count,
                "days": int(older_than_days),
                "lim": limit,
            },
        )
        return rowcount(result)

    async def audit(
        self,
        *,
        memory_id: MemoryId,
        organization_id: OrganizationId,
        event: str,
        actor_name: str | None = None,
        run_id: RunId | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        await self._s.execute(
            text(
                """
                INSERT INTO memory_audit (memory_id, organization_id, event, actor_name,
                                          run_id, detail)
                VALUES (:mid, :org, :event, :actor, :run, CAST(:detail AS jsonb))
                """
            ),
            {
                "mid": memory_id,
                "org": organization_id,
                "event": event,
                "actor": actor_name,
                "run": run_id,
                "detail": _json(detail or {}),
            },
        )

    async def audit_trail(self, memory_id: MemoryId, limit: int = 50) -> list[dict[str, Any]]:
        rows = (
            await self._s.execute(
                text(
                    """
                    SELECT event, actor_name, run_id, detail, occurred_at
                      FROM memory_audit WHERE memory_id = :mid
                     ORDER BY occurred_at DESC, id DESC LIMIT :lim
                    """
                ),
                {"mid": memory_id, "lim": limit},
            )
        ).all()
        return [
            {
                "event": r.event,
                "actor_name": r.actor_name,
                "run_id": str(r.run_id) if r.run_id else None,
                "detail": r.detail,
                "occurred_at": r.occurred_at.isoformat(),
            }
            for r in rows
        ]


# --- context traces ----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TraceRow:
    id: ContextTraceId
    run_id: RunId
    actor_name: str
    node: str
    query_text: str | None
    retrieved: list[dict[str, Any]]
    injected_count: int
    injected_tokens: int
    would_have_injected_tokens: int
    shadow_mode: bool
    grade: RetrievalGrade
    created_at: dt.datetime


class ContextTraceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def write(
        self,
        *,
        trace_id: ContextTraceId,
        organization_id: OrganizationId,
        run_id: RunId,
        actor_name: str,
        node: str,
        task_id: uuid.UUID | None = None,
        call_site: str | None = None,
        query_text: str | None = None,
        retrieved: list[dict[str, Any]] | None = None,
        injected_count: int = 0,
        injected_tokens: int = 0,
        would_have_injected_tokens: int = 0,
        total_tokens: int = 0,
        scopes: Sequence[str] = (),
        embedding_version: str | None = None,
        latency_ms: float | None = None,
        shadow_mode: bool = True,
    ) -> None:
        """One row per retrieval, shadow or live (T54).

        `query_text` is stored in full. It is the actor's own words about what it was
        looking for, and the grading harness cannot answer *"would this memory have
        helped in this specific call"* without it. It is not untrusted input — it is
        assembled by `ContextPlanner` from the task and the node, both of which are
        ours — so it is not fenced, and the harness reads it directly.
        """
        entries = retrieved or []
        await self._s.execute(
            text(
                """
                INSERT INTO context_traces (
                    id, organization_id, run_id, task_id, actor_name, node, call_site,
                    query_text, retrieved, retrieved_count, injected_count,
                    injected_tokens, would_have_injected_tokens, total_tokens, scopes,
                    embedding_version, latency_ms, shadow_mode)
                VALUES (:id, :org, :run, :task, :actor, :node, :site, :q,
                        CAST(:retrieved AS jsonb), :rc, :ic, :it, :wt, :tt, :scopes,
                        :ev, :lat, :shadow)
                """
            ),
            {
                "id": trace_id,
                "org": organization_id,
                "run": run_id,
                "task": task_id,
                "actor": actor_name,
                "node": node,
                "site": call_site,
                "q": query_text,
                "retrieved": _json(entries),
                "rc": len(entries),
                "ic": injected_count,
                "it": injected_tokens,
                "wt": would_have_injected_tokens,
                "tt": total_tokens,
                "scopes": list(scopes),
                "ev": embedding_version,
                "lat": latency_ms,
                "shadow": shadow_mode,
            },
        )

    async def ungraded(
        self, organization_id: OrganizationId, *, limit: int = 25, offset: int = 0
    ) -> list[TraceRow]:
        """The grading queue: traces that retrieved something and nobody has judged.

        Ordered oldest-first, deliberately. §5 wants a sample of *real* retrievals over
        a shadow-mode window; grading newest-first would sample the end of the window
        repeatedly and never reach the beginning, which is how a "sample of 100" turns
        out to be a sample of last Tuesday.
        """
        rows = (
            await self._s.execute(
                text(
                    """
                    SELECT id, run_id, actor_name, node, query_text, retrieved,
                           injected_count, injected_tokens, would_have_injected_tokens,
                           shadow_mode, grade, created_at
                      FROM context_traces
                     WHERE organization_id = :org AND grade = 'ungraded'
                       AND retrieved_count > 0
                     ORDER BY created_at, id LIMIT :lim OFFSET :off
                    """
                ),
                {"org": organization_id, "lim": limit, "off": offset},
            )
        ).all()
        return [_trace(r) for r in rows]

    async def get(self, trace_id: ContextTraceId) -> TraceRow | None:
        row = (
            await self._s.execute(
                text(
                    """
                    SELECT id, run_id, actor_name, node, query_text, retrieved,
                           injected_count, injected_tokens, would_have_injected_tokens,
                           shadow_mode, grade, created_at
                      FROM context_traces WHERE id = :id
                    """
                ),
                {"id": trace_id},
            )
        ).one_or_none()
        return _trace(row) if row is not None else None

    async def grade(
        self,
        trace_id: ContextTraceId,
        *,
        grade: RetrievalGrade,
        graded_by: str,
        note: str | None = None,
    ) -> bool:
        result = await self._s.execute(
            text(
                """
                UPDATE context_traces
                   SET grade = :grade, graded_by = :by, graded_at = now(), grade_note = :note
                 WHERE id = :id
                """
            ),
            {"id": trace_id, "grade": grade.value, "by": graded_by, "note": note},
        )
        return rowcount(result) == 1

    async def grade_summary(self, organization_id: OrganizationId) -> dict[str, Any]:
        """§5's bar, computed. Read straight off `v_memory_grades` so the CLI, the eval
        harness and anyone with a psql prompt are all looking at the same arithmetic."""
        row = (
            await self._s.execute(
                text(
                    """
                    SELECT coalesce(sum(graded), 0)  AS graded,
                           coalesce(sum(helpful), 0) AS helpful,
                           coalesce(sum(neutral), 0) AS neutral,
                           coalesce(sum(harmful), 0) AS harmful
                      FROM v_memory_grades WHERE organization_id = :org
                    """
                ),
                {"org": organization_id},
            )
        ).one()
        graded = int(row.graded)
        helpful, neutral, harmful = int(row.helpful), int(row.neutral), int(row.harmful)
        return {
            "graded": graded,
            "helpful": helpful,
            "neutral": neutral,
            "harmful": harmful,
            "helpful_or_neutral_pct": (helpful + neutral) / graded * 100 if graded else None,
            "harmful_pct": harmful / graded * 100 if graded else None,
        }

    async def token_effect(self, organization_id: OrganizationId) -> list[dict[str, Any]]:
        rows = (
            await self._s.execute(
                text(
                    """
                    SELECT week, shadow_mode, retrievals, avg_injected_tokens,
                           avg_would_have_tokens, max_injected_tokens, avg_retrieved,
                           avg_injected, avg_latency_ms
                      FROM v_memory_token_effect WHERE organization_id = :org
                     ORDER BY week DESC, shadow_mode
                    """
                ),
                {"org": organization_id},
            )
        ).all()
        return [dict(r._mapping) for r in rows]

    async def count_for_run(self, run_id: RunId) -> int:
        return int(
            (
                await self._s.execute(
                    text("SELECT count(*) FROM context_traces WHERE run_id = :r"),
                    {"r": run_id},
                )
            ).scalar_one()
        )


def _trace(r: Any) -> TraceRow:
    return TraceRow(
        id=ContextTraceId(r.id),
        run_id=RunId(r.run_id),
        actor_name=r.actor_name,
        node=r.node,
        query_text=r.query_text,
        retrieved=list(r.retrieved or []),
        injected_count=int(r.injected_count),
        injected_tokens=int(r.injected_tokens),
        would_have_injected_tokens=int(r.would_have_injected_tokens),
        shadow_mode=bool(r.shadow_mode),
        grade=RetrievalGrade(r.grade),
        created_at=r.created_at,
    )


# --- promotion ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PromotionRow:
    id: PromotionId
    memory_id: MemoryId
    from_scope: MemoryScope
    to_scope: MemoryScope
    to_scope_id: uuid.UUID
    proposed_by: str
    status: PromotionStatus
    reviewer: str | None
    rationale: str | None


class PromotionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def propose(
        self,
        *,
        promotion_id: PromotionId,
        organization_id: OrganizationId,
        memory_id: MemoryId,
        from_scope: MemoryScope,
        to_scope: MemoryScope,
        to_scope_id: uuid.UUID,
        proposed_by: str,
        proposed_run_id: RunId | None = None,
    ) -> PromotionId | None:
        """Enqueue a proposal. `None` when one is already open for this rung.

        `ON CONFLICT DO NOTHING` against `uq_promotion_open` rather than a
        select-then-insert: an actor whose retrieval keeps surfacing the same private
        fact proposes it on every run, and two of those runs land in the same second
        often enough to matter.
        """
        got = (
            await self._s.execute(
                text(
                    """
                    INSERT INTO memory_promotions (id, organization_id, memory_id, from_scope,
                                                   to_scope, to_scope_id, proposed_by,
                                                   proposed_run_id)
                    VALUES (:id, :org, :mid, :from, :to, :tosid, :by, :run)
                    -- The index form, not `ON CONSTRAINT`: `uq_promotion_open` is a
                    -- *partial* unique index and Postgres does not expose those as
                    -- named constraints. The predicate has to be repeated here and it
                    -- has to match 028's exactly, or the inference finds no index and
                    -- the statement fails at run time rather than at review.
                    ON CONFLICT (organization_id, memory_id, to_scope)
                        WHERE status = 'PROPOSED'
                    DO NOTHING
                    RETURNING id
                    """
                ),
                {
                    "id": promotion_id,
                    "org": organization_id,
                    "mid": memory_id,
                    "from": from_scope.value,
                    "to": to_scope.value,
                    "tosid": to_scope_id,
                    "by": proposed_by,
                    "run": proposed_run_id,
                },
            )
        ).scalar_one_or_none()
        return PromotionId(got) if got is not None else None

    async def pending(
        self, organization_id: OrganizationId, *, limit: int = 50
    ) -> list[PromotionRow]:
        rows = (
            await self._s.execute(
                text(
                    """
                    SELECT id, memory_id, from_scope, to_scope, to_scope_id, proposed_by,
                           status, reviewer, rationale
                      FROM memory_promotions
                     WHERE organization_id = :org AND status = 'PROPOSED'
                     ORDER BY proposed_at LIMIT :lim
                    """
                ),
                {"org": organization_id, "lim": limit},
            )
        ).all()
        return [
            PromotionRow(
                id=PromotionId(r.id),
                memory_id=MemoryId(r.memory_id),
                from_scope=MemoryScope(r.from_scope),
                to_scope=MemoryScope(r.to_scope),
                to_scope_id=r.to_scope_id,
                proposed_by=r.proposed_by,
                status=PromotionStatus(r.status),
                reviewer=r.reviewer,
                rationale=r.rationale,
            )
            for r in rows
        ]

    async def decide(
        self,
        promotion_id: PromotionId,
        *,
        status: PromotionStatus,
        reviewer: str,
        reviewer_kind: str,
        rationale: str,
        criteria: dict[str, Any] | None = None,
        promoted_memory_id: MemoryId | None = None,
        review_cost_cents: int = 0,
    ) -> bool:
        """Record a decision. Only a PROPOSED row can be decided.

        The `status = 'PROPOSED'` predicate makes a double review a no-op rather than
        an overwrite: two reviewers reaching the queue at once produce one decision and
        one loser that learns it lost, which is better than a silent last-write-wins on
        a row whose whole purpose is accountability.
        """
        result = await self._s.execute(
            text(
                """
                UPDATE memory_promotions
                   SET status = :status, reviewer = :reviewer, reviewer_kind = :kind,
                       rationale = :rationale, criteria = CAST(:criteria AS jsonb),
                       decided_at = now(), promoted_memory_id = :promoted,
                       review_cost_cents = :cost
                 WHERE id = :id AND status = 'PROPOSED'
                """
            ),
            {
                "id": promotion_id,
                "status": status.value,
                "reviewer": reviewer,
                "kind": reviewer_kind,
                "rationale": rationale,
                "criteria": _json(criteria or {}),
                "promoted": promoted_memory_id,
                "cost": review_cost_cents,
            },
        )
        return rowcount(result) == 1

    async def withdraw_for_memory(self, memory_id: MemoryId) -> int:
        """The source memory was superseded or retired before review. Not a judgement.

        Counted separately from a rejection because a proposal nobody got to is a
        statement about the *queue*, and a proposal somebody refused is a statement
        about the *memory*. Collapsing them would make the review backlog invisible
        inside a rejection rate.
        """
        result = await self._s.execute(
            text(
                """
                UPDATE memory_promotions SET status = 'WITHDRAWN', decided_at = now()
                 WHERE memory_id = :mid AND status = 'PROPOSED'
                """
            ),
            {"mid": memory_id},
        )
        return rowcount(result)

    async def approved_for(self, memory_id: MemoryId, to_scope: MemoryScope) -> bool:
        """Is there a decided approval for this exact widening? T53's query."""
        return bool(
            (
                await self._s.execute(
                    text(
                        """
                        SELECT 1 FROM memory_promotions
                         WHERE memory_id = :mid AND to_scope = :to AND status = 'APPROVED'
                         LIMIT 1
                        """
                    ),
                    {"mid": memory_id, "to": to_scope.value},
                )
            ).scalar_one_or_none()
        )


# --- entities ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EntityRow:
    id: EntityId
    kind: str
    canonical_name: str
    aliases: list[str]
    attributes: dict[str, Any]
    mention_count: int


class EntityRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def upsert(
        self,
        *,
        entity_id: EntityId,
        organization_id: OrganizationId,
        kind: str,
        canonical_name: str,
        scope: MemoryScope,
        scope_id: uuid.UUID,
        aliases: Sequence[str] = (),
        attributes: dict[str, Any] | None = None,
        first_seen_run_id: RunId | None = None,
    ) -> EntityId:
        """Get-or-create on `(org, kind, scope_id, lower(name))`, merging aliases.

        The alias merge is `array(SELECT DISTINCT unnest(...))` rather than `||`
        because the same alias arrives on most mentions and an append-only array would
        grow without bound on a hot entity — which is a slow leak into a GIN index, and
        the sort of thing that is invisible until the index is the size of the table.
        """
        await self._s.execute(
            text(
                """
                INSERT INTO entities (id, organization_id, kind, canonical_name, aliases,
                                      attributes, scope, scope_id, first_seen_run_id,
                                      mention_count)
                VALUES (:id, :org, :kind, :name, :aliases, CAST(:attrs AS jsonb), :scope,
                        :sid, :run, 1)
                ON CONFLICT (organization_id, kind, scope_id, lower(canonical_name))
                    WHERE merged_into IS NULL
                DO UPDATE
                   SET mention_count = entities.mention_count + 1,
                       updated_at    = now(),
                       aliases       = ARRAY(
                           SELECT DISTINCT unnest(entities.aliases || EXCLUDED.aliases)
                       ),
                       attributes    = entities.attributes || EXCLUDED.attributes
                """
            ),
            {
                "id": entity_id,
                "org": organization_id,
                "kind": kind,
                "name": canonical_name,
                "aliases": list(aliases),
                "attrs": _json(attributes or {}),
                "scope": scope.value,
                "sid": scope_id,
                "run": first_seen_run_id,
            },
        )
        found = (
            await self._s.execute(
                text(
                    """
                    SELECT id FROM entities
                     WHERE organization_id = :org AND kind = :kind AND scope_id = :sid
                       AND lower(canonical_name) = lower(:name) AND merged_into IS NULL
                    """
                ),
                {"org": organization_id, "kind": kind, "sid": scope_id, "name": canonical_name},
            )
        ).scalar_one()
        return EntityId(found)

    async def find(
        self, organization_id: OrganizationId, *, name: str, kind: str | None = None
    ) -> EntityRow | None:
        """Canonical name first, then aliases. One query, ordered.

        The order matters: a string that is one entity's canonical name and another's
        alias belongs to the first. `ORDER BY` on the match kind rather than two
        queries, so the tie-break is in the database and not in whichever caller
        remembered to check both.
        """
        row = (
            await self._s.execute(
                text(
                    """
                    SELECT id, kind, canonical_name, aliases, attributes, mention_count,
                           (lower(canonical_name) = lower(:name)) AS exact
                      FROM entities
                     WHERE organization_id = :org
                       AND (:kind IS NULL OR kind = :kind)
                       AND merged_into IS NULL
                       AND (lower(canonical_name) = lower(:name) OR :name = ANY(aliases))
                     ORDER BY exact DESC, mention_count DESC LIMIT 1
                    """
                ),
                {"org": organization_id, "name": name, "kind": kind},
            )
        ).one_or_none()
        if row is None:
            return None
        return EntityRow(
            id=EntityId(row.id),
            kind=row.kind,
            canonical_name=row.canonical_name,
            aliases=list(row.aliases or []),
            attributes=dict(row.attributes or {}),
            mention_count=int(row.mention_count),
        )

    async def link(
        self,
        entity_id: EntityId,
        *,
        memory_id: MemoryId | None = None,
        run_id: RunId | None = None,
        artifact_id: uuid.UUID | None = None,
        relation: str = "mentions",
        confidence: float | None = None,
    ) -> None:
        await self._s.execute(
            text(
                """
                INSERT INTO entity_links (entity_id, memory_id, run_id, artifact_id,
                                          relation, confidence)
                VALUES (:eid, :mid, :run, :art, :rel, :conf)
                ON CONFLICT (entity_id, memory_id, relation)
                    WHERE memory_id IS NOT NULL
                DO NOTHING
                """
            ),
            {
                "eid": entity_id,
                "mid": memory_id,
                "run": run_id,
                "art": artifact_id,
                "rel": relation,
                "conf": confidence,
            },
        )

    async def memories_for(self, entity_id: EntityId, *, limit: int = 50) -> list[MemoryId]:
        """The point of the whole table: "everything we know about X", exactly.

        Joined against `memory_metadata` so a link to a superseded memory does not come
        back — the link is kept for audit, but a retired fact is not an answer.
        """
        rows = (
            await self._s.execute(
                text(
                    """
                    SELECT l.memory_id
                      FROM entity_links l
                      JOIN memory_metadata m ON m.memory_id = l.memory_id
                     WHERE l.entity_id = :eid AND m.status = 'active'
                     ORDER BY l.created_at DESC LIMIT :lim
                    """
                ),
                {"eid": entity_id, "lim": limit},
            )
        ).scalars()
        return [MemoryId(r) for r in rows]


# --- intentions and procedures -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class IntentionRow:
    id: IntentionId
    organization_id: OrganizationId
    actor_name: str
    intent: str
    rationale: str | None
    due_at: dt.datetime
    dedupe_key: str
    source_run_id: RunId | None


class IntentionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def schedule(
        self,
        *,
        intention_id: IntentionId,
        organization_id: OrganizationId,
        actor_name: str,
        scope: MemoryScope,
        scope_id: uuid.UUID,
        intent: str,
        due_at: dt.datetime,
        dedupe_key: str,
        rationale: str | None = None,
        source_run_id: RunId | None = None,
        source_memory_id: MemoryId | None = None,
    ) -> IntentionId | None:
        """`None` when an identical intention is already pending. See 026's docstring."""
        got = (
            await self._s.execute(
                text(
                    """
                    INSERT INTO scheduled_intentions (id, organization_id, actor_name, scope,
                        scope_id, intent, rationale, due_at, dedupe_key, source_run_id,
                        source_memory_id)
                    VALUES (:id, :org, :actor, :scope, :sid, :intent, :why, :due, :dk, :run, :mid)
                    ON CONFLICT (organization_id, actor_name, dedupe_key)
                        WHERE status = 'PENDING'
                    DO NOTHING
                    RETURNING id
                    """
                ),
                {
                    "id": intention_id,
                    "org": organization_id,
                    "actor": actor_name,
                    "scope": scope.value,
                    "sid": scope_id,
                    "intent": intent,
                    "why": rationale,
                    "due": due_at,
                    "dk": dedupe_key,
                    "run": source_run_id,
                    "mid": source_memory_id,
                },
            )
        ).scalar_one_or_none()
        return IntentionId(got) if got is not None else None

    async def claim_due(self, *, now: dt.datetime, limit: int = 20) -> list[IntentionRow]:
        """Take the due intentions, atomically, one worker at a time.

        `FOR UPDATE SKIP LOCKED` inside a CTE, the same pattern the dispatcher uses.
        Two workers draining this queue must not both fire the same intention: an
        intention usually becomes a task, and two tasks is two runs and two bills for
        one decision.
        """
        rows = (
            await self._s.execute(
                text(
                    """
                    WITH due AS (
                        SELECT id FROM scheduled_intentions
                         WHERE status = 'PENDING' AND due_at <= :now
                         ORDER BY due_at
                         FOR UPDATE SKIP LOCKED
                         LIMIT :lim
                    )
                    UPDATE scheduled_intentions s
                       SET attempts = s.attempts + 1
                      FROM due
                     WHERE s.id = due.id
                    RETURNING s.id, s.organization_id, s.actor_name, s.intent, s.rationale,
                              s.due_at, s.dedupe_key, s.source_run_id
                    """
                ),
                {"now": now, "lim": limit},
            )
        ).all()
        return [
            IntentionRow(
                id=IntentionId(r.id),
                organization_id=OrganizationId(r.organization_id),
                actor_name=r.actor_name,
                intent=r.intent,
                rationale=r.rationale,
                due_at=r.due_at,
                dedupe_key=r.dedupe_key,
                source_run_id=RunId(r.source_run_id) if r.source_run_id else None,
            )
            for r in rows
        ]

    async def mark_fired(
        self, intention_id: IntentionId, *, run_id: RunId | None, task_id: uuid.UUID | None
    ) -> None:
        await self._s.execute(
            text(
                """
                UPDATE scheduled_intentions
                   SET status = 'FIRED', fired_run_id = :run, task_id = :task, fired_at = now()
                 WHERE id = :id
                """
            ),
            {"id": intention_id, "run": run_id, "task": task_id},
        )

    async def cancel(self, intention_id: IntentionId) -> None:
        await self._s.execute(
            text(
                "UPDATE scheduled_intentions SET status = 'CANCELLED' "
                "WHERE id = :id AND status = 'PENDING'"
            ),
            {"id": intention_id},
        )


class ProcedureRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def observe(
        self,
        *,
        candidate_id: ProcedureCandidateId,
        organization_id: OrganizationId,
        actor_name: str,
        scope: MemoryScope,
        scope_id: uuid.UUID,
        title: str,
        steps: dict[str, Any],
        steps_hash: str,
        run_id: RunId | None,
        succeeded: bool,
    ) -> None:
        """Record one more sighting of a shape. Upsert on `(org, actor, steps_hash)`.

        The success and failure counters move here rather than in a second call,
        because a shape observed and a shape's outcome are the same event seen once —
        splitting them would let a crash between the two produce a candidate with an
        observation count that no longer matches its outcomes, which is the state
        `ck_procedure_counts` refuses.
        """
        await self._s.execute(
            text(
                """
                INSERT INTO procedure_candidates (id, organization_id, actor_name, scope,
                    scope_id, title, steps, steps_hash, observed_count, success_count,
                    failure_count, first_run_id, last_run_id, last_failure_run_id)
                VALUES (:id, :org, :actor, :scope, :sid, :title, CAST(:steps AS jsonb), :hash,
                        1, :succ, :fail, :run, :run, :failrun)
                ON CONFLICT ON CONSTRAINT uq_procedure_shape DO UPDATE
                   SET observed_count = procedure_candidates.observed_count + 1,
                       success_count  = procedure_candidates.success_count + EXCLUDED.success_count,
                       failure_count  = procedure_candidates.failure_count + EXCLUDED.failure_count,
                       last_run_id    = EXCLUDED.last_run_id,
                       last_failure_run_id = coalesce(EXCLUDED.last_failure_run_id,
                                                      procedure_candidates.last_failure_run_id),
                       updated_at     = now()
                """
            ),
            {
                "id": candidate_id,
                "org": organization_id,
                "actor": actor_name,
                "scope": scope.value,
                "sid": scope_id,
                "title": title,
                "steps": _json(steps),
                "hash": steps_hash,
                "succ": 1 if succeeded else 0,
                "fail": 0 if succeeded else 1,
                "run": run_id,
                "failrun": None if succeeded else run_id,
            },
        )

    async def ready(
        self, organization_id: OrganizationId, *, min_observations: int, min_successes: int
    ) -> list[dict[str, Any]]:
        """Candidates that have earned a review. Not adopted — reviewed."""
        rows = (
            await self._s.execute(
                text(
                    """
                    SELECT id, actor_name, title, steps, observed_count, success_count,
                           failure_count, scope, scope_id
                      FROM procedure_candidates
                     WHERE organization_id = :org AND status = 'OBSERVING'
                       AND observed_count >= :minobs AND success_count >= :minsucc
                     ORDER BY success_count DESC, observed_count DESC
                    """
                ),
                {"org": organization_id, "minobs": min_observations, "minsucc": min_successes},
            )
        ).all()
        return [dict(r._mapping) for r in rows]

    async def set_status(
        self,
        candidate_id: ProcedureCandidateId,
        *,
        status: str,
        promoted_memory_id: MemoryId | None = None,
    ) -> None:
        await self._s.execute(
            text(
                """
                UPDATE procedure_candidates
                   SET status = :st, promoted_memory_id = :mid, updated_at = now()
                 WHERE id = :id
                """
            ),
            {"id": candidate_id, "st": status, "mid": promoted_memory_id},
        )


def _json(value: Any) -> str:
    """Canonical JSON for a jsonb bind parameter.

    `canonical_json` rather than `json.dumps` so a trace written twice — a redelivered
    outbox event, a replayed node — is byte-identical, which is what makes
    "did this retrieval change" a string comparison rather than a semantic diff.
    """
    from runtime.domain.hashing import canonical_json

    return canonical_json(value)
