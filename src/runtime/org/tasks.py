"""The task lifecycle.

    DRAFT ─assign→ ASSIGNED ─claim→ IN_PROGRESS ─submit→ SUBMITTED ─evaluate→ CLOSED
                       ↑                                      │
                       └──────────── rework (max 2) ──────────┘

Three things happen here that are worth reading closely.

**Validation gates `SUBMITTED`.** `submit()` validates the result against the
task's pinned `output_schema_ref` *before* any state moves. A failure increments
`schema_failures`, returns the typed error list, and leaves the task IN_PROGRESS —
so an assignee that produces garbage does not get to mark its own work as done and
hand the manager something unevaluable. On the third failure the task closes as
REJECTED/SCHEMA_FAILURE and a human is told. T14, T15.

**Assignment is one transaction with its notification.** The task row moving to
ASSIGNED and the assignee's inbox message are the same write. Splitting them gives
you either a task nobody was told about or a message about a task that is not
assigned, and both fail silently — the loop just stops on a Wednesday.

**Nothing in this module calls a model.** Assignment, submission notification and
closure are rules. That is the §3 thesis made checkable: if the coordination ratio
still comes out high, the hierarchy is the problem, and the plumbing being provably
free is what licenses that conclusion.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

from runtime.artifacts.store import ArtifactStore
from runtime.domain.enums import AuditSeverity, RejectionReason, TaskOutcome, TaskStatus
from runtime.domain.errors import OutputSchemaViolation
from runtime.domain.hashing import canonical_json
from runtime.domain.ids import (
    ArtifactId,
    CorrelationId,
    GoalId,
    OrganizationId,
    ProjectId,
    RunId,
    TaskId,
    WorkerId,
)
from runtime.domain.schemas import SCHEMAS, SchemaError
from runtime.observability.logging import get_logger
from runtime.org.inbox import (
    KIND_TASK_ASSIGNED,
    KIND_TASK_CLOSED,
    KIND_TASK_REWORK,
    KIND_TASK_SUBMITTED,
    InboxService,
    dedupe_key,
)
from runtime.persistence.repositories.tasks import MAX_SCHEMA_FAILURES, TaskRow
from runtime.persistence.uow import UnitOfWorkFactory

log = get_logger("org.tasks")

TASK_LEASE_SECONDS = 300.0
"""How long an assignee owns a task. Longer than a run lease on purpose: a task is
owned across a whole submit cycle, a run only while a worker executes it."""

EVAL_DEADLINE_HOURS = 24.0
"""How long the manager has before `AUTO_ACCEPTED`. Short enough that a stalled
evaluator shows up within a day; long enough that a Thursday submission is not
auto-accepted because nobody ran the loop overnight. Tune it and §8.4's clock
resets — it is part of the frozen protocol."""

ESCALATION_RECIPIENT = "operator"
"""Where a capped task goes. The same human as the approval gate; M1 has one."""


def task_id_for(correlation_id: CorrelationId, title: str) -> TaskId:
    """Derive a task id from the plan that created it.

    The plan node runs inside a graph that can be replayed after a crash. A
    `uuid4()` here would write six more tasks on every replay; deriving the id
    makes the whole plan idempotent with `ON CONFLICT DO NOTHING` underneath it.
    """
    return TaskId(uuid.uuid5(uuid.NAMESPACE_URL, f"task:{correlation_id}:{title}"))


@dataclass(frozen=True, slots=True)
class SubmitResult:
    """What `submit()` tells the assignee.

    `ok` false with `errors` populated is the ordinary schema-failure path and the
    assignee is expected to fix and retry. `ok` false with `closed` true means the
    cap was reached and there is nothing left to retry — the difference matters,
    because an agent that keeps retrying a closed task is a loop.
    """

    ok: bool
    task_id: TaskId
    errors: list[dict[str, str]]
    schema_failures: int
    closed: bool = False
    artifact_id: ArtifactId | None = None


class TaskService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        *,
        inbox: InboxService | None = None,
        artifacts: ArtifactStore | None = None,
    ) -> None:
        self._uow = uow_factory
        self._inbox = inbox or InboxService(uow_factory)
        self._artifacts = artifacts

    # --- creation and assignment ---------------------------------------------------

    async def create_and_assign(
        self,
        *,
        organization_id: OrganizationId,
        correlation_id: CorrelationId,
        title: str,
        objective: str,
        acceptance_criteria: list[str],
        assignee_name: str,
        output_schema_ref: str,
        task_input: dict[str, Any],
        created_by_actor_id: uuid.UUID | None = None,
        goal_id: GoalId | None = None,
        project_id: ProjectId | None = None,
        due_at: dt.datetime | None = None,
        input_schema_ref: str | None = None,
        sender: str | None = None,
        now: dt.datetime | None = None,
    ) -> tuple[TaskId, bool]:
        """Write a task and tell its assignee, atomically. `(task_id, created)`.

        Refuses an unregistered `output_schema_ref` rather than storing it. A task
        pinned to a schema that does not exist is unevaluable, and it would not be
        discovered until the assignee tried to submit — days later, with the failure
        landing on the wrong actor.
        """
        if not SCHEMAS.has(output_schema_ref):
            raise OutputSchemaViolation(
                f"task {title!r} pins {output_schema_ref!r}, which is not registered; "
                f"known: {sorted(SCHEMAS.refs())}",
                schema_ref=output_schema_ref,
            )
        moment = now or dt.datetime.now(dt.UTC)
        task_id = task_id_for(correlation_id, title)

        async with self._uow.transaction() as uow:
            created = await uow.tasks.create(
                {
                    "id": task_id,
                    "organization_id": organization_id,
                    "project_id": project_id,
                    "goal_id": goal_id,
                    "correlation_id": correlation_id,
                    "parent_task_id": None,
                    "title": title,
                    "objective": objective,
                    "acceptance_criteria": acceptance_criteria,
                    "created_by_actor_id": created_by_actor_id,
                    "assignee_actor_id": None,
                    "assignee_name": assignee_name,
                    "input_schema_ref": input_schema_ref,
                    "output_schema_ref": output_schema_ref,
                    "input": task_input,
                    "status": TaskStatus.ASSIGNED.value,
                    "due_at": due_at,
                    "eval_deadline": None,
                }
            )
            if created is None:
                return task_id, False

            await self._inbox.send(
                organization_id=organization_id,
                kind=KIND_TASK_ASSIGNED,
                recipient=assignee_name,
                correlation_id=correlation_id,
                key=dedupe_key(KIND_TASK_ASSIGNED, task_id),
                subject=title,
                body={
                    "task_id": str(task_id),
                    "objective": objective,
                    "acceptance_criteria": acceptance_criteria,
                    "output_schema_ref": output_schema_ref,
                    "due_at": due_at.isoformat() if due_at else None,
                },
                sender=sender,
                task_id=task_id,
                hop_count=0,
                uow=uow,
            )
            await uow.audit.record(
                organization_id=organization_id,
                action="task.assigned",
                target=str(task_id),
                outcome=assignee_name,
                detail={"title": title, "output_schema_ref": output_schema_ref},
            )
        log.info(
            "task.assigned",
            task_id=str(task_id),
            assignee=assignee_name,
            schema=output_schema_ref,
        )
        _ = moment
        return task_id, True

    # --- working -------------------------------------------------------------------

    async def claim(self, task_id: TaskId, worker_id: WorkerId) -> TaskRow | None:
        """Take ownership. `None` when another worker already has it (T21)."""
        async with self._uow.transaction() as uow:
            version = await uow.tasks.claim(task_id, worker_id, TASK_LEASE_SECONDS)
            if version is None:
                return None
            return await uow.tasks.get(task_id)

    async def get(self, task_id: TaskId) -> TaskRow | None:
        async with self._uow() as uow:
            return await uow.tasks.get(task_id)

    async def submit(
        self,
        task_id: TaskId,
        *,
        worker_id: WorkerId,
        result: Any,
        organization_id: OrganizationId,
        run_id: RunId | None = None,
        manager_name: str,
        now: dt.datetime | None = None,
    ) -> SubmitResult:
        """Validate against the pinned schema, then submit. Validation gates the
        transition — nothing moves if the result does not conform."""
        moment = now or dt.datetime.now(dt.UTC)
        task = await self.get(task_id)
        if task is None:
            raise OutputSchemaViolation(f"task {task_id} does not exist")

        schema = SCHEMAS.get(task.output_schema_ref)
        errors: list[SchemaError] = schema.check(result)
        if errors:
            return await self._handle_schema_failure(
                task,
                [e.to_json() for e in errors],
                organization_id=organization_id,
                manager_name=manager_name,
            )

        payload = schema.validate(result).model_dump(mode="json")
        artifact_id = await self._store_output(
            organization_id=organization_id, run_id=run_id, task=task, payload=payload
        )

        deadline = moment + dt.timedelta(hours=EVAL_DEADLINE_HOURS)
        async with self._uow.transaction() as uow:
            won = await uow.tasks.submit(
                task_id,
                worker_id=worker_id,
                output=payload,
                artifact_id=artifact_id,
                eval_deadline=deadline,
            )
            if not won:
                # Lost the task lease, or it was never claimed. Say nothing about
                # the task's state: it is no longer ours to describe.
                log.warning("task.submit_rejected", task_id=str(task_id))
                return SubmitResult(
                    ok=False,
                    task_id=task_id,
                    errors=[],
                    schema_failures=task.schema_failures,
                )
            await self._inbox.send(
                organization_id=organization_id,
                kind=KIND_TASK_SUBMITTED,
                recipient=manager_name,
                correlation_id=CorrelationId(task.correlation_id),
                key=dedupe_key(KIND_TASK_SUBMITTED, task_id, task.rework_count),
                subject=f"submitted: {task.title}",
                body={
                    "task_id": str(task_id),
                    "attempt": task.rework_count,
                    "output_schema_ref": task.output_schema_ref,
                },
                sender=task.assignee_name,
                task_id=task_id,
                artifact_id=artifact_id,
                # One hop from the assignment that caused it. The chain
                # plan → assign → submit → evaluate is four, well inside the cap.
                hop_count=1,
                uow=uow,
            )
        log.info("task.submitted", task_id=str(task_id), attempt=task.rework_count)
        return SubmitResult(
            ok=True,
            task_id=task_id,
            errors=[],
            schema_failures=task.schema_failures,
            artifact_id=artifact_id,
        )

    async def record_schema_failure(
        self,
        task_id: TaskId,
        errors: list[dict[str, str]],
        *,
        organization_id: OrganizationId,
        manager_name: str,
    ) -> SubmitResult | None:
        """Record a schema failure that happened *before* submit, at the model call.

        `call_structured` validates in the graph node, so an assignee that cannot
        produce a conforming object never reaches `submit` and the whole
        schema-failure apparatus below — the error list, the three-strike cap, the
        escalation — was unreachable from the path that actually fails. The
        observed cost: `last_schema_errors` was NULL on every task in the database
        while runs failed on `OutputSchemaViolation` all evening, so the one
        question worth asking ("which field?") had no answer, and the correction
        block both work graphs assemble from `last_schema_errors` was dead code.

        The caller re-raises afterwards. This records what happened; it does not
        decide whether the run survives.
        """
        task = await self.get(task_id)
        if task is None:
            return None
        return await self._handle_schema_failure(
            task, errors, organization_id=organization_id, manager_name=manager_name
        )

    async def _handle_schema_failure(
        self,
        task: TaskRow,
        as_json: list[dict[str, str]],
        *,
        organization_id: OrganizationId,
        manager_name: str,
    ) -> SubmitResult:
        async with self._uow.transaction() as uow:
            cap = await uow.tasks.record_schema_failure(task.id, as_json)
            await uow.audit.record(
                organization_id=organization_id,
                action="task.schema_failure",
                target=str(task.id),
                severity=AuditSeverity.HIGH if cap.capped else AuditSeverity.NORMAL,
                outcome=f"{cap.count}/{MAX_SCHEMA_FAILURES}",
                detail={"schema_ref": task.output_schema_ref, "errors": as_json[:10]},
            )
        log.warning(
            "task.schema_failure",
            task_id=str(task.id),
            failures=cap.count,
            schema=task.output_schema_ref,
            error_count=len(as_json),
        )
        if not cap.capped:
            return SubmitResult(
                ok=False, task_id=task.id, errors=as_json, schema_failures=cap.count
            )

        await self.close(
            task.id,
            organization_id=organization_id,
            outcome=TaskOutcome.REJECTED,
            reason=RejectionReason.SCHEMA_FAILURE.value,
            manager_name=manager_name,
            require_submitted=False,
            escalate=(
                f"{task.title!r} failed its output schema {MAX_SCHEMA_FAILURES} times "
                f"and has been rejected. Last errors: "
                f"{'; '.join(e['pointer'] + ': ' + e['message'] for e in as_json[:3])}"
            ),
        )
        return SubmitResult(
            ok=False, task_id=task.id, errors=as_json, schema_failures=cap.count, closed=True
        )

    async def _store_output(
        self,
        *,
        organization_id: OrganizationId,
        run_id: RunId | None,
        task: TaskRow,
        payload: dict[str, Any],
    ) -> ArtifactId | None:
        """Put the validated output in the artifact store and link it to the task.

        Always, not only when it is large. A task output is the deliverable — it is
        what a human reads, what the evaluator judges and what the next task
        references — so it gets a content-addressed, immutable home regardless of
        size. The `tasks.output` column keeps a copy for querying; the artifact is
        the one with a digest.
        """
        if self._artifacts is None:
            return None
        data = canonical_json(payload).encode("utf-8")
        ref = await self._artifacts.put(
            data,
            organization_id=organization_id,
            run_id=run_id,
            kind=f"task_output:{task.output_schema_ref}",
            content_type="application/json",
        )
        await self._artifacts.link(
            ref.artifact_id, source_type="task", source_id=str(task.id), relation="produced"
        )
        return ref.artifact_id

    # --- closing -------------------------------------------------------------------

    async def close(
        self,
        task_id: TaskId,
        *,
        organization_id: OrganizationId,
        outcome: TaskOutcome,
        reason: str | None,
        manager_name: str,
        output: dict[str, Any] | None = None,
        require_submitted: bool = True,
        escalate: str | None = None,
    ) -> bool:
        """Terminal transition plus its notifications, in one transaction."""
        async with self._uow.transaction() as uow:
            task = await uow.tasks.get(task_id)
            if task is None:
                return False
            won = await uow.tasks.close(
                task_id,
                outcome=outcome,
                reason=reason,
                output=output,
                require_submitted=require_submitted,
            )
            if not won:
                return False
            if task.assignee_name:
                await self._inbox.send(
                    organization_id=organization_id,
                    kind=KIND_TASK_CLOSED,
                    recipient=task.assignee_name,
                    correlation_id=CorrelationId(task.correlation_id),
                    key=dedupe_key(KIND_TASK_CLOSED, task_id, outcome.value),
                    subject=f"{outcome.value}: {task.title}",
                    body={"task_id": str(task_id), "outcome": outcome.value, "reason": reason},
                    sender=manager_name,
                    task_id=task_id,
                    # Terminal by construction: nothing replies to a closure, so the
                    # hop count here can never grow a chain.
                    hop_count=2,
                    uow=uow,
                )
            if escalate:
                await self._inbox.send(
                    organization_id=organization_id,
                    kind=KIND_TASK_CLOSED,
                    recipient=ESCALATION_RECIPIENT,
                    correlation_id=CorrelationId(task.correlation_id),
                    key=dedupe_key("escalation", task_id, outcome.value),
                    subject=f"escalation: {task.title}",
                    body={"task_id": str(task_id), "message": escalate},
                    sender=manager_name,
                    task_id=task_id,
                    hop_count=2,
                    uow=uow,
                )
            await uow.audit.record(
                organization_id=organization_id,
                action="task.closed",
                target=str(task_id),
                severity=AuditSeverity.HIGH if escalate else AuditSeverity.NORMAL,
                outcome=outcome.value,
                detail={"reason": reason, "escalated": bool(escalate)},
            )
        log.info("task.closed", task_id=str(task_id), outcome=outcome.value, reason=reason)
        return True

    async def send_back_for_rework(
        self,
        task_id: TaskId,
        *,
        organization_id: OrganizationId,
        instructions: list[str],
        manager_name: str,
    ) -> bool:
        """Return a task to its assignee. False when the cap says no more.

        The caller closes it as REJECTED/REWORK_EXHAUSTED on False — the decision is
        the database's, made in the same statement as the increment, so there is no
        window in which two evaluations both see "one rework left". T16.
        """
        async with self._uow.transaction() as uow:
            task = await uow.tasks.get(task_id)
            if task is None or task.assignee_name is None:
                return False
            cap = await uow.tasks.request_rework(task_id)
            if cap.capped:
                return False
            await self._inbox.send(
                organization_id=organization_id,
                kind=KIND_TASK_REWORK,
                recipient=task.assignee_name,
                correlation_id=CorrelationId(task.correlation_id),
                key=dedupe_key(KIND_TASK_REWORK, task_id, cap.count),
                subject=f"rework ({cap.count}/2): {task.title}",
                body={
                    "task_id": str(task_id),
                    "attempt": cap.count,
                    "instructions": instructions,
                },
                sender=manager_name,
                task_id=task_id,
                hop_count=2,
                uow=uow,
            )
        log.info("task.rework", task_id=str(task_id), attempt=cap.count)
        return True

    async def set_dependency(self, downstream: TaskId, upstream: TaskId) -> bool:
        """Record that `downstream` consumes `upstream`'s output.

        Written as a merge into the input JSON rather than a rewrite, so a plan that
        already put other keys there keeps them. This is how artifact flow between
        tasks is wired (§2): the head names the dependency by title, the runtime
        resolves it to an id, and `content`'s load node follows it to an artifact.
        """
        from sqlalchemy import text

        async with self._uow.transaction() as uow:
            row = (
                await uow.session.execute(
                    text(
                        """
                        UPDATE tasks
                           SET input = input
                                     || jsonb_build_object(
                                            'source_task_id', CAST(:src AS text))
                             , parent_task_id = :src_uuid
                         WHERE id = :id AND status IN ('DRAFT','ASSIGNED')
                        RETURNING id
                        """
                    ),
                    {"src": str(upstream), "src_uuid": upstream, "id": downstream},
                )
            ).one_or_none()
        return row is not None

    # --- reads ---------------------------------------------------------------------

    async def open_tasks(self, organization_id: OrganizationId) -> list[TaskRow]:
        async with self._uow() as uow:
            return await uow.tasks.open_for_org(organization_id)

    async def for_correlation(self, correlation_id: CorrelationId) -> list[TaskRow]:
        async with self._uow() as uow:
            return await uow.tasks.for_correlation(correlation_id)
