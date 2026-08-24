"""Tasks and their evaluations.

Every state transition here is a single conditional UPDATE with a `RETURNING`, for
the same reason the run lease is: a read-then-write is a lost update, and a lost
update on a task means either two assignees working the same brief or a rework
cycle that quietly becomes a fourth.

The transitions that carry a cap — `record_schema_failure`, `request_rework` — do
the increment and the cap check in one statement and report which side of the cap
they landed on. The caller does not get to decide; it gets told. That is what makes
T15 and T16 assertions about the database rather than about the service layer.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.domain.enums import EvaluatorKind, TaskOutcome, TaskStatus
from runtime.domain.ids import EvaluationId, TaskId
from runtime.persistence.json import to_jsonb, to_jsonb_or_none

MAX_REWORK = 2
"""§2: rework cap. The third request is a rejection, not a fourth cycle."""

MAX_SCHEMA_FAILURES = 3
"""§6 T15: the third validation failure closes the task as SCHEMA_FAILURE."""


@dataclass(frozen=True, slots=True)
class TaskRow:
    id: TaskId
    organization_id: uuid.UUID
    correlation_id: uuid.UUID
    title: str
    objective: str
    acceptance_criteria: list[str]
    assignee_name: str | None
    input_schema_ref: str | None
    output_schema_ref: str
    input: dict[str, Any]
    output: dict[str, Any] | None
    output_artifact_id: uuid.UUID | None
    status: TaskStatus
    outcome: TaskOutcome | None
    outcome_reason: str | None
    rework_count: int
    schema_failures: int
    last_schema_errors: Any
    human_touched: bool
    version: int
    due_at: dt.datetime | None
    submitted_at: dt.datetime | None
    eval_deadline: dt.datetime | None
    created_at: dt.datetime

    @property
    def attempt(self) -> int:
        """The evaluation attempt this task is on. Part of `uq_evaluation_once`."""
        return self.rework_count


@dataclass(frozen=True, slots=True)
class CapResult:
    """Outcome of an increment that has a ceiling.

    `capped` is the interesting bit and the reason this is not just an int: the
    caller must branch on "you have used your last chance" and doing that by
    comparing an int to a constant it also holds is how the two drift apart.
    """

    count: int
    capped: bool


_SELECT = """
    SELECT id, organization_id, correlation_id, title, objective, acceptance_criteria,
           assignee_name, input_schema_ref, output_schema_ref, input, output,
           output_artifact_id, status, outcome, outcome_reason, rework_count,
           schema_failures, last_schema_errors, human_touched, version, due_at,
           submitted_at, eval_deadline, created_at
      FROM tasks
"""


def _row(r: Any) -> TaskRow:
    return TaskRow(
        id=TaskId(r.id),
        organization_id=r.organization_id,
        correlation_id=r.correlation_id,
        title=r.title,
        objective=r.objective,
        acceptance_criteria=list(r.acceptance_criteria or []),
        assignee_name=r.assignee_name,
        input_schema_ref=r.input_schema_ref,
        output_schema_ref=r.output_schema_ref,
        input=dict(r.input or {}),
        output=dict(r.output) if r.output is not None else None,
        output_artifact_id=r.output_artifact_id,
        status=TaskStatus(r.status),
        outcome=TaskOutcome(r.outcome) if r.outcome else None,
        outcome_reason=r.outcome_reason,
        rework_count=r.rework_count,
        schema_failures=r.schema_failures,
        last_schema_errors=r.last_schema_errors,
        human_touched=r.human_touched,
        version=r.version,
        due_at=r.due_at,
        submitted_at=r.submitted_at,
        eval_deadline=r.eval_deadline,
        created_at=r.created_at,
    )


class TaskRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    # --- creation ------------------------------------------------------------------

    async def create(self, row: dict[str, Any]) -> TaskId | None:
        """Insert a task. `None` when `dedupe_id` already produced one.

        The plan node runs under a run that can be replayed, so creating tasks has
        to be idempotent on something stable. `id` is derived from
        `(correlation_id, title)` by the caller, which makes the replay of a plan
        node write the same six task rows rather than six more.
        """
        stmt = text(
            """
            INSERT INTO tasks (
                id, organization_id, project_id, goal_id, correlation_id, parent_task_id,
                title, objective, acceptance_criteria, created_by_actor_id,
                assignee_actor_id, assignee_name, input_schema_ref, output_schema_ref,
                input, status, due_at, eval_deadline
            ) VALUES (
                :id, :organization_id, :project_id, :goal_id, :correlation_id, :parent_task_id,
                :title, :objective, CAST(:acceptance_criteria AS jsonb), :created_by_actor_id,
                :assignee_actor_id, :assignee_name, :input_schema_ref, :output_schema_ref,
                CAST(:input AS jsonb), :status, :due_at, :eval_deadline
            )
            ON CONFLICT (id) DO NOTHING
            RETURNING id
            """
        )
        payload = {
            **row,
            "acceptance_criteria": to_jsonb(row.get("acceptance_criteria", [])),
            "input": to_jsonb(row.get("input", {})),
        }
        got = (await self._s.execute(stmt, payload)).scalar_one_or_none()
        return TaskId(got) if got is not None else None

    # --- reads ---------------------------------------------------------------------

    async def get(self, task_id: TaskId) -> TaskRow | None:
        r = (
            await self._s.execute(text(_SELECT + " WHERE id = :id"), {"id": task_id})
        ).one_or_none()
        return _row(r) if r is not None else None

    async def for_correlation(self, correlation_id: uuid.UUID) -> list[TaskRow]:
        rows = (
            await self._s.execute(
                text(_SELECT + " WHERE correlation_id = :c ORDER BY created_at"),
                {"c": correlation_id},
            )
        ).all()
        return [_row(r) for r in rows]

    async def open_for_org(self, organization_id: uuid.UUID, limit: int = 50) -> list[TaskRow]:
        rows = (
            await self._s.execute(
                text(
                    _SELECT
                    + """ WHERE organization_id = :org
                            AND status NOT IN ('CLOSED','CANCELLED')
                          ORDER BY created_at LIMIT :limit"""
                ),
                {"org": organization_id, "limit": limit},
            )
        ).all()
        return [_row(r) for r in rows]

    async def closed_since(
        self, organization_id: uuid.UUID, since: dt.datetime, limit: int = 200
    ) -> list[TaskRow]:
        rows = (
            await self._s.execute(
                text(
                    _SELECT
                    + """ WHERE organization_id = :org AND evaluated_at >= :since
                          ORDER BY evaluated_at LIMIT :limit"""
                ),
                {"org": organization_id, "since": since, "limit": limit},
            )
        ).all()
        return [_row(r) for r in rows]

    # --- lifecycle -----------------------------------------------------------------

    async def assign(
        self, task_id: TaskId, *, assignee_name: str, assignee_actor_id: uuid.UUID | None
    ) -> bool:
        """DRAFT → ASSIGNED. Also the return path from a rework request."""
        row = (
            await self._s.execute(
                text(
                    """
                    UPDATE tasks
                       SET assignee_name = :name, assignee_actor_id = :actor,
                           status = 'ASSIGNED', version = version + 1,
                           lease_worker = NULL, lease_until = NULL
                     WHERE id = :id AND status IN ('DRAFT','ASSIGNED')
                    RETURNING id
                    """
                ),
                {"id": task_id, "name": assignee_name, "actor": assignee_actor_id},
            )
        ).one_or_none()
        return row is not None

    async def claim(
        self, task_id: TaskId, worker_id: uuid.UUID, lease_seconds: float
    ) -> int | None:
        """Take ownership of a task. Returns the new `version`, or None if lost.

        The predicate re-checks `lease_until` *inside* the UPDATE. Under two
        concurrent claims Postgres serialises on the row lock and re-evaluates the
        WHERE clause for the loser, so exactly one claim succeeds without either an
        exception path or an advisory lock. T21.
        """
        row = (
            await self._s.execute(
                text(
                    """
                    UPDATE tasks
                       SET lease_worker = :worker,
                           lease_until = now() + make_interval(secs => :secs),
                           status = 'IN_PROGRESS',
                           version = version + 1
                     WHERE id = :id
                       AND status IN ('ASSIGNED','IN_PROGRESS')
                       AND (lease_until IS NULL OR lease_until < now())
                    RETURNING version
                    """
                ),
                {"id": task_id, "worker": worker_id, "secs": lease_seconds},
            )
        ).one_or_none()
        return int(row.version) if row is not None else None

    async def submit(
        self,
        task_id: TaskId,
        *,
        worker_id: uuid.UUID,
        output: dict[str, Any],
        artifact_id: uuid.UUID | None,
        eval_deadline: dt.datetime,
    ) -> bool:
        """IN_PROGRESS → SUBMITTED, guarded by the lease.

        Guarded because a worker whose task lease expired must not be able to
        submit over the replacement's work — the task equivalent of the run fence.
        """
        row = (
            await self._s.execute(
                text(
                    """
                    UPDATE tasks
                       SET output = CAST(:output AS jsonb),
                           output_artifact_id = :artifact,
                           status = 'SUBMITTED',
                           submitted_at = now(),
                           eval_deadline = :deadline,
                           outcome = NULL,
                           outcome_reason = NULL,
                           lease_until = NULL,
                           version = version + 1
                     WHERE id = :id AND status = 'IN_PROGRESS' AND lease_worker = :worker
                    RETURNING id
                    """
                ),
                {
                    "id": task_id,
                    "worker": worker_id,
                    "output": to_jsonb(output),
                    "artifact": artifact_id,
                    "deadline": eval_deadline,
                },
            )
        ).one_or_none()
        return row is not None

    async def record_schema_failure(self, task_id: TaskId, errors: Any) -> CapResult:
        """Increment `schema_failures` and report whether the cap was reached.

        Increment and cap-check in one statement: a read-then-increment would let
        two concurrent failures both see two and both write three, which is how a
        cap of three becomes a cap of four. T15.
        """
        row = (
            await self._s.execute(
                text(
                    """
                    UPDATE tasks
                       SET schema_failures = LEAST(schema_failures + 1, :cap),
                           last_schema_errors = CAST(:errors AS jsonb),
                           version = version + 1
                     WHERE id = :id
                    RETURNING schema_failures
                    """
                ),
                {"id": task_id, "cap": MAX_SCHEMA_FAILURES, "errors": to_jsonb(errors)},
            )
        ).one()
        count = int(row.schema_failures)
        return CapResult(count=count, capped=count >= MAX_SCHEMA_FAILURES)

    async def request_rework(self, task_id: TaskId) -> CapResult:
        """Send a submitted task back, unless that would exceed the cap.

        When the cap is already reached nothing is incremented and `capped` is
        True — the caller closes the task as REJECTED/REWORK_EXHAUSTED instead of
        opening a fourth cycle. T16.
        """
        row = (
            await self._s.execute(
                text(
                    """
                    UPDATE tasks
                       SET rework_count = rework_count + 1,
                           status = 'ASSIGNED',
                           outcome = 'REWORK_REQUIRED',
                           evaluated_at = now(),
                           lease_worker = NULL,
                           lease_until = NULL,
                           version = version + 1
                     WHERE id = :id AND status = 'SUBMITTED' AND rework_count < :cap
                    RETURNING rework_count
                    """
                ),
                {"id": task_id, "cap": MAX_REWORK},
            )
        ).one_or_none()
        if row is None:
            current = (
                await self._s.execute(
                    text("SELECT rework_count FROM tasks WHERE id = :id"), {"id": task_id}
                )
            ).scalar_one()
            return CapResult(count=int(current), capped=True)
        return CapResult(count=int(row.rework_count), capped=False)

    async def close(
        self,
        task_id: TaskId,
        *,
        outcome: TaskOutcome,
        reason: str | None,
        output: dict[str, Any] | None = None,
        require_submitted: bool = True,
    ) -> bool:
        """Terminal transition. `require_submitted` guards the AUTO_ACCEPTED sweep.

        The sweeper must only auto-accept a task that is still SUBMITTED *and* still
        unjudged; if the manager's verdict landed a millisecond earlier, the
        predicate fails and the real evaluation stands. Without `outcome IS NULL` in
        the WHERE clause, a slow sweep would overwrite genuine verdicts with
        AUTO_ACCEPTED and the acceptance metrics would decay silently. T17.
        """
        guard = "AND status = 'SUBMITTED' AND outcome IS NULL" if require_submitted else ""
        row = (
            await self._s.execute(
                text(
                    f"""
                    UPDATE tasks
                       SET status = 'CLOSED',
                           outcome = :outcome,
                           outcome_reason = :reason,
                           output = COALESCE(CAST(:output AS jsonb), output),
                           evaluated_at = COALESCE(evaluated_at, now()),
                           closed_at = now(),
                           lease_until = NULL,
                           version = version + 1
                     WHERE id = :id {guard}
                    RETURNING id
                    """
                ),
                {
                    "id": task_id,
                    "outcome": outcome.value,
                    "reason": reason,
                    "output": to_jsonb_or_none(output),
                },
            )
        ).one_or_none()
        return row is not None

    async def mark_human_touched(self, task_id: TaskId) -> None:
        """Any human intervention at all. Feeds the unassisted-completion metric.

        Set by the approval gate, by a CLI nudge, and by a human evaluation that
        disagreed — anything that means a person had to be involved for this task to
        reach its outcome. Never unset.
        """
        await self._s.execute(
            text("UPDATE tasks SET human_touched = true WHERE id = :id"), {"id": task_id}
        )

    async def due_for_auto_accept(self, limit: int = 100) -> list[TaskRow]:
        rows = (
            await self._s.execute(
                text(
                    _SELECT
                    + """ WHERE status = 'SUBMITTED' AND outcome IS NULL
                            AND eval_deadline IS NOT NULL AND eval_deadline < now()
                          ORDER BY eval_deadline LIMIT :limit
                          FOR UPDATE SKIP LOCKED"""
                ),
                {"limit": limit},
            )
        ).all()
        return [_row(r) for r in rows]


class EvaluationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def record(
        self,
        evaluation_id: EvaluationId,
        *,
        organization_id: uuid.UUID,
        task_id: TaskId,
        attempt: int,
        evaluator_kind: EvaluatorKind,
        outcome: TaskOutcome,
        evaluator_actor_id: uuid.UUID | None = None,
        evaluator_ref: str | None = None,
        run_id: uuid.UUID | None = None,
        rejection_reason: str | None = None,
        rubric: Any = None,
        reasoning: str = "",
        rework_instructions: Any = None,
        edit_distance: int | None = None,
        cost_cents: int = 0,
    ) -> EvaluationId | None:
        """Append one evaluation. `None` when this (task, kind, attempt) already has
        one — a retried evaluation run must not double the denominator."""
        got = (
            await self._s.execute(
                text(
                    """
                    INSERT INTO task_evaluations (
                        id, organization_id, task_id, attempt, evaluator_kind,
                        evaluator_actor_id, evaluator_ref, run_id, outcome,
                        rejection_reason, rubric, reasoning, rework_instructions,
                        edit_distance, cost_cents
                    ) VALUES (
                        :id, :org, :task_id, :attempt, :kind, :actor, :ref, :run_id, :outcome,
                        :reason, CAST(:rubric AS jsonb), :reasoning,
                        CAST(:instructions AS jsonb), :edit_distance, :cost
                    )
                    ON CONFLICT ON CONSTRAINT uq_evaluation_once DO NOTHING
                    RETURNING id
                    """
                ),
                {
                    "id": evaluation_id,
                    "org": organization_id,
                    "task_id": task_id,
                    "attempt": attempt,
                    "kind": evaluator_kind.value,
                    "actor": evaluator_actor_id,
                    "ref": evaluator_ref,
                    "run_id": run_id,
                    "outcome": outcome.value,
                    "reason": rejection_reason,
                    "rubric": to_jsonb(rubric or []),
                    "reasoning": reasoning,
                    "instructions": to_jsonb(rework_instructions or []),
                    "edit_distance": edit_distance,
                    "cost": cost_cents,
                },
            )
        ).scalar_one_or_none()
        return EvaluationId(got) if got is not None else None

    async def for_task(self, task_id: TaskId) -> list[dict[str, Any]]:
        rows = (
            await self._s.execute(
                text(
                    """
                    SELECT id, attempt, evaluator_kind, outcome, rejection_reason, rubric,
                           reasoning, rework_instructions, edit_distance, cost_cents,
                           created_at
                      FROM task_evaluations WHERE task_id = :id
                     ORDER BY created_at
                    """
                ),
                {"id": task_id},
            )
        ).all()
        return [dict(r._mapping) for r in rows]

    async def manager_accepted_unsampled(
        self, organization_id: uuid.UUID, since: dt.datetime, limit: int = 200
    ) -> list[dict[str, Any]]:
        """Manager-accepted tasks with no human row yet — the sampling frame (§8.2).

        Deliberately *not* filtered to a random subset here. Sampling is the
        harness's job and it stratifies; a repository that returned a random slice
        would make the stratification unauditable.
        """
        rows = (
            await self._s.execute(
                text(
                    """
                    SELECT e.task_id, e.attempt, e.outcome, e.edit_distance,
                           t.title, t.output_schema_ref, t.assignee_name, t.created_at
                      FROM task_evaluations e
                      JOIN tasks t ON t.id = e.task_id
                     WHERE e.evaluator_kind = 'manager'
                       AND e.outcome IN ('ACCEPTED','ACCEPTED_WITH_EDITS')
                       AND e.organization_id = :org
                       AND e.created_at >= :since
                       AND NOT EXISTS (
                            SELECT 1 FROM task_evaluations h
                             WHERE h.task_id = e.task_id AND h.evaluator_kind = 'human'
                       )
                     ORDER BY t.output_schema_ref, e.created_at
                     LIMIT :limit
                    """
                ),
                {"org": organization_id, "since": since, "limit": limit},
            )
        ).all()
        return [dict(r._mapping) for r in rows]
