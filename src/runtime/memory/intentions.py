"""Scheduled intentions and procedure candidates.

Two small services over migrations 026 and 027. They share a file because they share a
shape: both turn something an actor noticed into a durable row that the *existing*
machinery — M1's scheduler, §8's promotion queue — later acts on. Neither introduces a
second scheduler or a second promotion path, and that restraint is the whole design.

**Intentions.** *"Come back to this on Thursday"* survives only if a machine holds it.
An actor's alternatives are a sentence in a session summary, which the next
summarization compresses away, and a memory, which retrieval may or may not surface on
the right day. `SchedulerService.drain_intentions` is what turns a due row into a task,
and the loop closes: `scheduled_intentions.fired_run_id` points at the run it caused, so
*"did the thing we promised to revisit actually get revisited"* is a join. That question
is one of §1's three hypotheses, and a hypothesis you cannot query is a hope.

**Procedures.** A shape that has worked three times is worth someone's attention; it is
not yet a method. `ProcedureService.observe` counts, `ready` selects the ones that have
earned a review, and adoption goes through `memory_promotions` like every other
widening. There is no threshold at which something is adopted automatically — §8 again,
and `ck_procedure_adopted_has_memory` in the database.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from runtime.domain.enums import MemoryScope, MemoryStatus, MemoryTrust, MemoryType
from runtime.domain.hashing import canonical_json
from runtime.domain.ids import (
    IntentionId,
    MemoryId,
    OrganizationId,
    RunId,
    TaskId,
    new_intention_id,
    new_procedure_candidate_id,
    new_promotion_id,
)
from runtime.observability.logging import get_logger
from runtime.org.department import department_scope_id
from runtime.persistence.repositories.memory import IntentionRow
from runtime.persistence.uow import UnitOfWorkFactory

log = get_logger("memory.intentions")

MAX_HORIZON_DAYS = 90
"""`[CHOSEN]`. An intention further out than this is refused.

Not arbitrary in kind, even if the number is: an actor asked to remember something
"eventually" will pick a date, and a date six years out is a row that sits in the
pending index forever and is never wrong. A horizon makes the actor commit to a time at
which the intention becomes checkable."""


class IntentionService:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow = uow_factory

    async def schedule(
        self,
        organization_id: OrganizationId,
        *,
        actor_name: str,
        intent: str,
        due_at: dt.datetime,
        rationale: str | None = None,
        scope: MemoryScope = MemoryScope.PRIVATE_ACTOR,
        scope_id: uuid.UUID | None = None,
        source_run_id: RunId | None = None,
        source_memory_id: MemoryId | None = None,
        now: dt.datetime | None = None,
    ) -> IntentionId | None:
        """Write one intention. `None` if an identical one is already pending.

        The dedupe key is derived from the *intent text and the due date*, not supplied,
        because a caller that chooses its own key is a caller that will choose a
        different one next time the same condition fires — and the whole purpose of the
        key is that the same condition produces the same key. 026's docstring has the
        failure it prevents: forty identical reminders arriving on Thursday.
        """
        moment = now or dt.datetime.now(dt.UTC)
        if due_at <= moment:
            log.warning("intention.rejected", reason="due in the past", actor=actor_name)
            return None
        if due_at > moment + dt.timedelta(days=MAX_HORIZON_DAYS):
            log.warning(
                "intention.rejected",
                reason=f"beyond the {MAX_HORIZON_DAYS}-day horizon",
                actor=actor_name,
            )
            return None

        normalised = " ".join(intent.lower().split())
        dedupe = hashlib.sha256(
            f"{normalised}\x00{due_at.date().isoformat()}".encode()
        ).hexdigest()[:32]

        async with self._uow.transaction() as uow:
            return await uow.intentions.schedule(
                intention_id=new_intention_id(),
                organization_id=organization_id,
                actor_name=actor_name,
                scope=scope,
                scope_id=scope_id or department_scope_id(organization_id),
                intent=intent[:2_000],
                due_at=due_at,
                dedupe_key=dedupe,
                rationale=rationale,
                source_run_id=source_run_id,
                source_memory_id=source_memory_id,
            )

    async def claim_due(
        self, *, now: dt.datetime | None = None, limit: int = 20
    ) -> list[IntentionRow]:
        """Take what is due, atomically. See the repository for why SKIP LOCKED."""
        async with self._uow.transaction() as uow:
            return await uow.intentions.claim_due(now=now or dt.datetime.now(dt.UTC), limit=limit)

    async def mark_fired(
        self,
        intention_id: IntentionId,
        *,
        run_id: RunId | None = None,
        task_id: TaskId | None = None,
    ) -> None:
        async with self._uow.transaction() as uow:
            await uow.intentions.mark_fired(
                intention_id,
                run_id=run_id,
                task_id=uuid.UUID(str(task_id)) if task_id else None,
            )

    async def cancel(self, intention_id: IntentionId) -> None:
        async with self._uow.transaction() as uow:
            await uow.intentions.cancel(intention_id)


# --- procedures --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ProcedureStep:
    """One step of an observed shape. Node and tool, never arguments.

    027's docstring: arguments differ every time and the shape is what recurs. A hash
    that included them would mean nothing ever recurred and the table filled with
    singletons — a feature doing nothing, quietly.
    """

    node: str
    tool: str | None = None

    def key(self) -> str:
        return f"{self.node}:{self.tool or '-'}"


def steps_hash(steps: Sequence[ProcedureStep]) -> str:
    return hashlib.sha256("|".join(s.key() for s in steps).encode()).hexdigest()[:32]


class ProcedureService:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow = uow_factory

    async def observe(
        self,
        organization_id: OrganizationId,
        *,
        actor_name: str,
        title: str,
        steps: Sequence[ProcedureStep],
        run_id: RunId | None,
        succeeded: bool,
        scope: MemoryScope = MemoryScope.PRIVATE_ACTOR,
        scope_id: uuid.UUID | None = None,
    ) -> None:
        if not steps:
            return
        async with self._uow.transaction() as uow:
            await uow.procedures.observe(
                candidate_id=new_procedure_candidate_id(),
                organization_id=organization_id,
                actor_name=actor_name,
                scope=scope,
                scope_id=scope_id or department_scope_id(organization_id),
                title=title[:200],
                steps={"steps": [{"node": s.node, "tool": s.tool} for s in steps]},
                steps_hash=steps_hash(steps),
                run_id=run_id,
                succeeded=succeeded,
            )

    async def observe_run(
        self, organization_id: OrganizationId, run_id: RunId, *, succeeded: bool
    ) -> None:
        """Reconstruct a run's shape from its effect journal and record it.

        The journal, not a trace the graph writes: `effect_intents` already records
        every tool call with its node and ordinal, so the shape is derivable from data
        that exists for a different and stronger reason. A graph that had to report its
        own shape is a graph with a second thing to keep in sync, and it would be the
        half that silently stops being updated.
        """
        from sqlalchemy import text as _text

        async with self._uow() as uow:
            rows = (
                await uow.session.execute(
                    _text(
                        """
                        SELECT e.node, e.tool_name, a.name AS actor_name, t.title
                          FROM effect_intents e
                          JOIN runs r ON r.id = e.run_id
                          LEFT JOIN actors a ON a.id = r.actor_id
                          LEFT JOIN tasks t ON t.id = r.task_id
                         WHERE e.run_id = :run AND e.status = 'COMMITTED'
                         ORDER BY e.node, e.ordinal
                        """
                    ),
                    {"run": run_id},
                )
            ).all()
        if not rows:
            return
        steps = [ProcedureStep(node=r.node, tool=r.tool_name) for r in rows]
        await self.observe(
            organization_id,
            actor_name=rows[0].actor_name or "unknown",
            title=rows[0].title or f"{rows[0].actor_name or 'actor'} tool sequence",
            steps=steps,
            run_id=run_id,
            succeeded=succeeded,
        )

    async def ready_for_review(
        self,
        organization_id: OrganizationId,
        *,
        min_observations: int,
        min_successes: int,
    ) -> list[dict[str, Any]]:
        async with self._uow() as uow:
            return await uow.procedures.ready(
                organization_id,
                min_observations=min_observations,
                min_successes=min_successes,
            )

    async def propose(
        self,
        organization_id: OrganizationId,
        candidate: dict[str, Any],
        *,
        store_write: Any,
    ) -> MemoryId | None:
        """Turn a candidate into a `procedure` memory and a promotion proposal.

        **Both, in that order, and neither alone.** The memory is written at the
        candidate's own scope — private, where it is visible to the actor that found the
        shape and to nobody else — and the proposal is what asks for it to be widened.
        Writing the memory straight at department scope would be the automatic promotion
        §8 forbids, wearing a different hat.

        `store_write` is passed rather than held so this service does not need a store:
        `ProcedureService` is otherwise pure database, and a store dependency would mean
        the CLI's `procedures --list` could not run without an embedder configured.
        """
        steps = candidate.get("steps", {}).get("steps", [])
        if not steps:
            return None
        rendered = " → ".join(
            f"{s['node']}" + (f" ({s['tool']})" if s.get("tool") else "") for s in steps
        )
        from runtime.memory.store import ExtractedFact

        fact = ExtractedFact(
            subject=f"procedure: {candidate['title']}",
            statement=(
                f"{rendered}. Observed {candidate['observed_count']} times by "
                f"{candidate['actor_name']}, succeeded {candidate['success_count']}."
            ),
            memory_type=MemoryType.PROCEDURE,
            confidence=candidate["success_count"] / max(1, candidate["observed_count"]),
            importance=0.7,
        )
        scope = MemoryScope(candidate["scope"])
        scope_id = candidate["scope_id"]
        events = await store_write(
            organization_id=organization_id,
            scope_key=f"{scope.value}:{scope_id}",
            facts=[fact],
        )
        if not events or events[0].event == "NOOP":
            return None

        memory_id: MemoryId = events[0].memory_id
        async with self._uow.transaction() as uow:
            await uow.memories.insert(
                memory_id=memory_id,
                organization_id=organization_id,
                scope=scope,
                scope_id=scope_id,
                memory_type=MemoryType.PROCEDURE,
                trust=MemoryTrust.DERIVED,
                status=MemoryStatus.ACTIVE,
                embedding_version="unknown",
                collection="unknown",
                confidence=fact.confidence,
                importance=fact.importance,
            )
            await uow.promotions.propose(
                promotion_id=new_promotion_id(),
                organization_id=organization_id,
                memory_id=memory_id,
                from_scope=scope,
                to_scope=MemoryScope.DEPARTMENT,
                to_scope_id=department_scope_id(organization_id),
                proposed_by=candidate["actor_name"],
            )
            await uow.procedures.set_status(candidate["id"], status="PROPOSED")
            await uow.memories.audit(
                memory_id=memory_id,
                organization_id=organization_id,
                event="ADD",
                actor_name=candidate["actor_name"],
                detail={"kind": "procedure", "steps": canonical_json(steps)[:500]},
            )
        return memory_id
