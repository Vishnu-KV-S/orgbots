"""The write path. §6, in order, off the hot path.

    run completes → outbox event → memory worker (never the hot path)
       ├─ gather: final state, artifacts, tool observations, trust levels
       ├─ compute run_trust = min(trust of all context blocks)
       ├─ store.write(facts)
       ├─ write memory_metadata sidecar with trust = run_trust
       └─ if run_trust == UNTRUSTED → status='quarantined'

Two things in here are worth reading before the code.

---

**How `run_trust` is actually computed, and where the plan's rule needed a decision.**

§6 says *"if any untrusted content was in the run's context"*. Taken with M2's
`graphs.common.context`, which fences **every** inbox message unconditionally, that
sentence quarantines every run in the department — the four actors talk to each other on
every turn of the loop. A rule that always returns the same answer is not a rule.

So the trust question is asked about *provenance* rather than about *fencing*, and the
two signals below are what the schema can actually answer:

1. **Direct.** The run's own effect journal contains a call to a tool that returns
   content from outside the organization. `effect_intents` is the authority here rather
   than `audit_logs`, because an audit row says a call was *allowed* and a journal row
   says it *happened*.

2. **Inherited.** Another run in the same `correlation_id` chain was directly untrusted.
   Correlation is the only lineage this schema records — `inbox_messages` carries a
   causation id but not a source run — and it is the conservative choice: it
   over-quarantines within a week rather than under-quarantining across one.

**Both directions of error, stated.** It over-quarantines: `analytics`, which cannot
call a model at all, is quarantined in any week where `research` fetched a page. It
under-quarantines in exactly one case: content that entered through a run's *input*
rather than through a tool — a human or an API caller pasting a payload into
`POST /v1/runs`. That case is what T52's corpus covers through the fetch path and does
not cover through the input path, and it is written down in `docs/M3_SHADOW.md` as a
known gap rather than left to be discovered.

Quarantine is not a dead end. §6: *"retrievable only by the originating actor, never
cross-scope, until reviewed."* `research` keeps reading its own research. What it cannot
do is put it in front of `content` without somebody looking at it.

---

**Why a failed extraction does not fail anything.**

Everything here runs after the run returned SUCCESS and the human has the artifact. A
memory subsystem that could turn a delivered piece of work into a failure would be a
liability dressed as a feature, so every fallible step is caught, logged and counted,
and the outbox event is consumed either way. The consequence — a run whose facts were
silently not written — is visible in `v_memory_inventory` as a gap and in the worker's
`memory.write_failed` counter, which is where a systematic extractor problem shows up.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text

from runtime.domain.enums import (
    PROMOTION_PATH,
    MemoryScope,
    MemoryStatus,
    MemoryTrust,
    TaskOutcome,
)
from runtime.domain.ids import (
    BudgetPoolId,
    MemoryId,
    OrganizationId,
    RunId,
    new_entity_id,
    new_promotion_id,
)
from runtime.domain.memory import ScopeKey, status_for
from runtime.domain.specs import ModelProfile
from runtime.gateway.models import ModelGateway
from runtime.memory.extraction import ExtractionSource, extract_facts
from runtime.memory.store import MemoryStore, StoreEvent
from runtime.observability.logging import get_logger
from runtime.org.department import company_scope_id, department_scope_id
from runtime.persistence.repositories.memory import rowcount
from runtime.persistence.uow import UnitOfWorkFactory

log = get_logger("memory.service")

OUTSIDE_CONTENT_TOOLS = frozenset({"web.search@1", "web.fetch@1"})
"""Tools whose result is content from outside the organization.

A frozenset here rather than a property on the tool registry, because "returns outside
content" and "has a blast radius" are different questions and conflating them gets the
answer wrong in both directions: `web.fetch@1` is `READ` blast radius and is the main
taint source, while `publish.external@1` is `IRREVERSIBLE` and brings nothing back.

Adding a tool that fetches and forgetting to list it here is the way this defence
silently stops working, which is why
`test_m3_write.py::test_the_taint_tool_set_names_every_fetching_tool` asserts the set
rather than trusting it.
"""


@dataclass(frozen=True, slots=True)
class WriteOutcome:
    run_id: RunId
    trust: MemoryTrust
    facts: int
    written: list[StoreEvent] = field(default_factory=list)
    superseded: list[MemoryId] = field(default_factory=list)
    proposed: int = 0
    cost_cents: int = 0
    skipped_reason: str | None = None


class MemoryService:
    """Everything that writes to memory. One object, so there is one write path.

    `ContextPlanner` reads and never writes; this writes and never assembles a prompt.
    That split is not tidiness — it is what makes the shadow-mode guarantee checkable,
    because the object that could change a prompt is the one that has no reason to
    exist during phase one.
    """

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        store: MemoryStore,
        models: ModelGateway,
        *,
        profile: ModelProfile,
        propose_promotions: bool = True,
    ) -> None:
        self._uow = uow_factory
        self._store = store
        self._models = models
        self._profile = profile
        self._propose = propose_promotions

    # --- trust ---------------------------------------------------------------------

    async def run_trust(self, run_id: RunId) -> MemoryTrust:
        """Direct then inherited. One query; see the module docstring for the rules."""
        async with self._uow() as uow:
            row = (
                await uow.session.execute(
                    text(
                        """
                        WITH me AS (
                            SELECT rs.spec ->> 'correlation_id' AS correlation_id
                              FROM run_specs rs WHERE rs.run_id = :run
                        )
                        SELECT
                          EXISTS (
                            SELECT 1 FROM effect_intents e
                             WHERE e.run_id = :run AND e.tool_name = ANY(:tools)
                          ) AS direct,
                          EXISTS (
                            -- Correlation lives in the *frozen spec*, not on `runs` —
                            -- `RunSpec.correlation_id` is hashed into `spec_hash`, which
                            -- makes it the authoritative answer to "what chain was this
                            -- run part of" rather than a column somebody could update.
                            SELECT 1
                              FROM me
                              JOIN run_specs peer
                                ON peer.spec ->> 'correlation_id' = me.correlation_id
                              JOIN effect_intents e ON e.run_id = peer.run_id
                             WHERE me.correlation_id IS NOT NULL
                               AND e.tool_name = ANY(:tools)
                          ) AS inherited
                        """
                    ),
                    {"run": run_id, "tools": sorted(OUTSIDE_CONTENT_TOOLS)},
                )
            ).one()
        tainted = bool(row.direct) or bool(row.inherited)
        return MemoryTrust.UNTRUSTED_QUARANTINE if tainted else MemoryTrust.TRUSTED

    # --- write ---------------------------------------------------------------------

    async def write_from_run(
        self,
        *,
        organization_id: OrganizationId,
        run_id: RunId,
        pool_id: BudgetPoolId | None = None,
        scope: MemoryScope = MemoryScope.PRIVATE_ACTOR,
    ) -> WriteOutcome:
        """Gather, extract, fuse, record. The whole of §6 for one run."""
        source, actor_id, task_outcome = await self._gather(organization_id, run_id)
        if source is None:
            return WriteOutcome(run_id, MemoryTrust.TRUSTED, 0, skipped_reason="run not found")

        trust = await self.run_trust(run_id)
        scope_id = await self._scope_id(organization_id, scope, actor_id, run_id)
        scope_key = str(ScopeKey(scope, scope_id))

        try:
            facts, cost = await extract_facts(
                self._models,
                organization_id=organization_id,
                source=source,
                profile=self._profile,
                pool_id=pool_id,
            )
        except Exception as exc:
            # The run already succeeded. See the module docstring: a memory failure is
            # not permitted to retroactively spoil a delivered piece of work.
            log.warning(
                "memory.write_failed", run_id=str(run_id), error=f"{type(exc).__name__}: {exc}"
            )
            return WriteOutcome(run_id, trust, 0, skipped_reason=f"{type(exc).__name__}")

        if not facts:
            return WriteOutcome(run_id, trust, 0, cost_cents=cost, skipped_reason="no facts")

        events = await self._store.write(
            organization_id=organization_id, scope_key=scope_key, facts=facts
        )
        superseded = await self._record(
            organization_id=organization_id,
            run_id=run_id,
            actor_id=actor_id,
            actor_name=source.actor_name,
            scope=scope,
            scope_id=scope_id,
            events=events,
            trust=trust,
        )
        proposed = 0
        if self._propose and trust is not MemoryTrust.UNTRUSTED_QUARANTINE:
            proposed = await self._propose_promotions(
                organization_id=organization_id,
                run_id=run_id,
                actor_name=source.actor_name,
                events=events,
                scope=scope,
                task_outcome=task_outcome,
            )

        log.info(
            "memory.written",
            run_id=str(run_id),
            actor=source.actor_name,
            trust=trust.value,
            added=sum(1 for e in events if e.event == "ADD"),
            updated=sum(1 for e in events if e.event == "UPDATE"),
            noop=sum(1 for e in events if e.event == "NOOP"),
            proposed=proposed,
            cost_cents=cost,
        )
        return WriteOutcome(
            run_id=run_id,
            trust=trust,
            facts=len(facts),
            written=events,
            superseded=superseded,
            proposed=proposed,
            cost_cents=cost,
        )

    async def _record(
        self,
        *,
        organization_id: OrganizationId,
        run_id: RunId,
        actor_id: uuid.UUID | None,
        actor_name: str,
        scope: MemoryScope,
        scope_id: uuid.UUID,
        events: Sequence[StoreEvent],
        trust: MemoryTrust,
    ) -> list[MemoryId]:
        """Sidecar rows, supersessions, entity links and audit — one transaction.

        All of it together, because a sidecar row committed without its supersession
        would leave two active memories about the same subject, which is eval 6 failing
        for a reason that has nothing to do with the store.
        """
        status = status_for(trust)
        superseded: list[MemoryId] = []
        async with self._uow.transaction() as uow:
            for event in events:
                if event.event == "NOOP":
                    await uow.memories.audit(
                        memory_id=event.memory_id,
                        organization_id=organization_id,
                        event="NOOP",
                        actor_name=actor_name,
                        run_id=run_id,
                    )
                    continue
                if event.event == "DELETE":
                    await uow.memories.set_status(event.memory_id, MemoryStatus.RETIRED)
                    await uow.promotions.withdraw_for_memory(event.memory_id)
                    await uow.memories.audit(
                        memory_id=event.memory_id,
                        organization_id=organization_id,
                        event="DELETE",
                        actor_name=actor_name,
                        run_id=run_id,
                    )
                    continue

                await uow.memories.insert(
                    memory_id=event.memory_id,
                    organization_id=organization_id,
                    scope=scope,
                    scope_id=scope_id,
                    memory_type=event.fact.memory_type,
                    trust=trust,
                    status=status,
                    embedding_version=_version_of(self._store),
                    collection=_collection_of(self._store),
                    source_run_id=run_id,
                    source_actor_id=actor_id,
                    confidence=event.fact.confidence,
                    importance=event.fact.importance,
                    supersedes=event.supersedes,
                )
                await uow.memories.audit(
                    memory_id=event.memory_id,
                    organization_id=organization_id,
                    event=event.event,
                    actor_name=actor_name,
                    run_id=run_id,
                    detail={"subject": event.subject_key, "trust": trust.value},
                )
                if trust.is_quarantined:
                    await uow.memories.audit(
                        memory_id=event.memory_id,
                        organization_id=organization_id,
                        event="QUARANTINE",
                        actor_name=actor_name,
                        run_id=run_id,
                        detail={"reason": "run_trust", "rule": "§6 blunt"},
                    )

                if event.supersedes is not None:
                    did = await uow.memories.supersede(old=event.supersedes, new=event.memory_id)
                    if did:
                        superseded.append(event.supersedes)
                        # A proposal for a fact that has just been replaced is not a
                        # rejection, it is a proposal nobody got to. 028's WITHDRAWN.
                        await uow.promotions.withdraw_for_memory(event.supersedes)
                        await uow.memories.audit(
                            memory_id=event.supersedes,
                            organization_id=organization_id,
                            event="SUPERSEDE",
                            actor_name=actor_name,
                            run_id=run_id,
                            detail={"by": str(event.memory_id)},
                        )

                for name in event.fact.entities:
                    entity_id = await uow.entities.upsert(
                        entity_id=new_entity_id(),
                        organization_id=organization_id,
                        kind="mentioned",
                        canonical_name=name,
                        scope=scope,
                        scope_id=scope_id,
                        first_seen_run_id=run_id,
                    )
                    await uow.entities.link(
                        entity_id,
                        memory_id=event.memory_id,
                        run_id=run_id,
                        confidence=event.fact.confidence,
                    )
        return superseded

    async def _propose_promotions(
        self,
        *,
        organization_id: OrganizationId,
        run_id: RunId,
        actor_name: str,
        events: Sequence[StoreEvent],
        scope: MemoryScope,
        task_outcome: str | None,
    ) -> int:
        """Enqueue proposals. **Enqueue — nothing is promoted here** (§8, T53).

        The filter is deliberately cheap and deliberately harsh, because the expensive
        filter is the reviewer and the queue is what has to stay short enough to be
        read. Three conditions, and the third is the one that does the most work:

        - the memory is fresh (`ADD` or `UPDATE`, never `NOOP`);
        - importance is above the floor;
        - **the task it came out of was accepted.** A fact extracted from work that a
          manager rejected is a fact from work that was wrong. Proposing it would put
          the organization's mistakes on the fastest path to being everybody's.
        """
        target = PROMOTION_PATH.get(scope)
        if target is None:
            return 0
        if task_outcome is not None and task_outcome not in _ACCEPTED:
            return 0

        to_scope_id = (
            department_scope_id(organization_id)
            if target is MemoryScope.DEPARTMENT
            else company_scope_id(organization_id)
        )
        count = 0
        async with self._uow.transaction() as uow:
            for event in events:
                if event.event not in ("ADD", "UPDATE"):
                    continue
                if event.fact.importance < PROMOTION_IMPORTANCE_FLOOR:
                    continue
                got = await uow.promotions.propose(
                    promotion_id=new_promotion_id(),
                    organization_id=organization_id,
                    memory_id=event.memory_id,
                    from_scope=scope,
                    to_scope=target,
                    to_scope_id=to_scope_id,
                    proposed_by=actor_name,
                    proposed_run_id=run_id,
                )
                count += 1 if got is not None else 0
        return count

    # --- consolidation -------------------------------------------------------------

    async def consolidate(
        self,
        organization_id: OrganizationId,
        *,
        scope: MemoryScope,
        scope_id: uuid.UUID,
        prune_after_days: float,
    ) -> dict[str, int]:
        """The maintenance half of the write path. Off the hot path, always (§2).

        Two jobs, both cheap and neither model-backed. That is a deliberate limit on
        what consolidation is allowed to be: §13 risk 4 is that *"consolidation cost is
        invisible until you look"*, and a consolidator that re-reads and re-summarises
        the whole store on a timer is the shape that gets expensive without anyone
        deciding it should. Summarising clusters of memories into `DERIVED` facts is the
        obvious next step and it is deliberately not here — it needs eval 7 to have a
        number first, so that "did compression lose anything" is answerable before
        compression is running.
        """
        async with self._uow.transaction() as uow:
            retired = await uow.memories.decay_and_prune(
                organization_id, older_than_days=prune_after_days
            )
            orphaned = await uow.session.execute(
                text(
                    """
                    UPDATE memory_promotions SET status = 'WITHDRAWN', decided_at = now()
                     WHERE organization_id = :org AND status = 'PROPOSED'
                       AND memory_id IN (
                           SELECT memory_id FROM memory_metadata
                            WHERE status <> 'active'
                       )
                    """
                ),
                {"org": organization_id},
            )
        if retired:
            log.info(
                "memory.pruned", retired=retired, scope=scope.value, days=int(prune_after_days)
            )
        return {"retired": retired, "withdrawn": rowcount(orphaned)}

    # --- gather --------------------------------------------------------------------

    async def _gather(
        self, organization_id: OrganizationId, run_id: RunId
    ) -> tuple[ExtractionSource | None, uuid.UUID | None, str | None]:
        """§6's first line. One query for the run and its task, one for the artifacts.

        Artifact *summaries*, never bodies. The same discipline `graphs.common.state`
        enforces for checkpoints applies here for a different reason: an extractor
        handed eight fetched pages is an extractor being asked to re-do the research
        actor's job at the research actor's prices, and what comes back is a summary of
        the evidence rather than what the run concluded from it.
        """
        async with self._uow() as uow:
            row = (
                await uow.session.execute(
                    text(
                        """
                        SELECT r.id, r.status,
                               a.id AS actor_id, a.name AS actor_name,
                               t.title, t.objective, t.outcome, t.output AS task_output,
                               (SELECT ev.payload -> 'output'
                                  FROM events ev
                                 WHERE ev.run_id = r.id AND ev.topic = 'run.succeeded'
                                 ORDER BY ev.id DESC LIMIT 1) AS run_output
                          FROM runs r
                          LEFT JOIN actors a ON a.id = r.actor_id
                          LEFT JOIN tasks t ON t.id = r.task_id
                         WHERE r.id = :run AND r.organization_id = :org
                        """
                    ),
                    {"run": run_id, "org": organization_id},
                )
            ).one_or_none()
            if row is None:
                return None, None, None

            # **Descriptors, not bodies — and not summaries either.** `artifacts` has no
            # summary column, and that constraint turns out to point the right way. An
            # extractor handed eight fetched pages is being asked to re-do the research
            # actor's job at the research actor's prices, and what comes back is a
            # summary of the evidence rather than what the run concluded from it. What is
            # worth knowing is *that* the run produced a 96 KB `research_evidence`
            # artifact, which is context for the conclusion rather than a substitute.
            artifacts = (
                await uow.session.execute(
                    text(
                        """
                        SELECT art.kind, av.size_bytes, av.sha256
                          FROM artifact_links l
                          JOIN artifacts art ON art.id = l.artifact_id
                          JOIN artifact_versions av
                            ON av.artifact_id = art.id AND av.version = art.current_version
                         WHERE l.source_type = 'run' AND l.source_id = :run
                         ORDER BY av.id DESC LIMIT 8
                        """
                    ),
                    {"run": str(run_id)},
                )
            ).all()

            observations = (
                (
                    await uow.session.execute(
                        text(
                            """
                        SELECT DISTINCT tool_name FROM effect_intents
                         WHERE run_id = :run AND status = 'COMMITTED'
                        """
                        ),
                        {"run": run_id},
                    )
                )
                .scalars()
                .all()
            )

        source = ExtractionSource(
            run_id=run_id,
            actor_name=row.actor_name or "unknown",
            task_title=row.title,
            task_objective=row.objective,
            # `runs` has no output column: a run's result reaches the world through
            # the transactional outbox and its durable twin in `events`. The task's
            # output is preferred where there is one, because it is the *validated*
            # object — the thing that passed the pinned schema — and the event payload
            # is whatever the graph returned.
            output=_first_dict(row.task_output, row.run_output),
            artifact_summaries=[
                f"produced a `{r.kind}` artifact of {r.size_bytes} bytes (sha256 {r.sha256[:12]})"
                for r in artifacts
            ],
            tool_observations=[f"used tool {t}" for t in observations],
            outcome=row.outcome or row.status,
        )
        return source, row.actor_id, row.outcome

    async def _scope_id(
        self,
        organization_id: OrganizationId,
        scope: MemoryScope,
        actor_id: uuid.UUID | None,
        run_id: RunId,
    ) -> uuid.UUID:
        if scope is MemoryScope.PRIVATE_ACTOR:
            if actor_id is None:
                raise ValueError(f"run {run_id} has no actor; cannot write a private memory")
            return actor_id
        if scope is MemoryScope.DEPARTMENT:
            return department_scope_id(organization_id)
        if scope is MemoryScope.COMPANY:
            return company_scope_id(organization_id)
        async with self._uow() as uow:
            session_id = (
                await uow.session.execute(
                    text("SELECT session_id FROM runs WHERE id = :r"), {"r": run_id}
                )
            ).scalar_one_or_none()
        if session_id is None:
            raise ValueError(f"run {run_id} has no session; cannot write a session memory")
        return uuid.UUID(str(session_id))


PROMOTION_IMPORTANCE_FLOOR = 0.6
"""`[CHOSEN]`. Below this a fact is not proposed for promotion at all.

The queue is read by a person as well as by a classifier (§8), and a queue nobody
finishes reading is a queue that approves whatever is at the top. This is the dial that
keeps it finishable; raise it when the queue outgrows the reviewer."""

_ACCEPTED = frozenset({TaskOutcome.ACCEPTED.value, TaskOutcome.ACCEPTED_WITH_EDITS.value})
"""`AUTO_ACCEPTED` is deliberately absent, for the reason M1 excludes it from every
acceptance metric: *"an unexamined task is not evidence that the organization produced
acceptable work."* Nobody looked at it, so nothing extracted from it should be on a path
toward being told to everybody."""


def _first_dict(*candidates: Any) -> dict[str, Any] | None:
    """The first candidate that is actually an object. `None` if none of them is."""
    for candidate in candidates:
        if isinstance(candidate, dict) and candidate:
            return candidate
    return None


def _version_of(store: MemoryStore) -> str:
    embeddings = getattr(store, "embeddings", None)
    return str(getattr(embeddings, "embedding_version", "unknown"))


def _collection_of(store: MemoryStore) -> str:
    return str(getattr(store, "collection", "unknown"))


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


__all__ = ["OUTSIDE_CONTENT_TOOLS", "Any", "MemoryService", "WriteOutcome"]
