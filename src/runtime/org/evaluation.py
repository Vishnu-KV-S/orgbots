"""Evaluation — the outcome state machine, the rework cap, and AUTO_ACCEPTED.

This module decides what a verdict *does*. The verdict itself comes from the
manager's `EVALUATION`-class model call (or from a human, via the sampling harness);
here it becomes a row in `task_evaluations` and a transition on the task.

Two rules are enforced here rather than trusted to the evaluator.

**The rework cap is the database's decision, not the manager's.** A verdict of
REWORK_REQUIRED on a task that has already been reworked twice does not open a third
cycle — `send_back_for_rework()` returns False and the task closes as
REJECTED/REWORK_EXHAUSTED. The manager does not get a vote, because an evaluator
that can extend its own budget has no budget. T16.

**AUTO_ACCEPTED is a sweep, not a verdict.** `EvaluationVerdict` refuses to carry
it (see `runtime.domain.outputs`), so it can only be written by `auto_accept_due()`
finding a submitted task past its `eval_deadline`. That keeps the meaning exact: an
AUTO_ACCEPTED task is one *nobody looked at*, which is why §7 excludes it from every
acceptance numerator and §9 caps its share at 20%. If it could also be returned by
an evaluator, the number would silently mean two different things. T17.

`edit_distance` is computed here, once, and stored. Recomputing it at query time
would let the dashboard's "mean edit distance trend" change retroactively every
time the distance function did — which is exactly the kind of drift §8 exists to
prevent.
"""

from __future__ import annotations

import datetime as dt
import difflib
import re
from dataclasses import dataclass
from typing import Any

from runtime.domain.enums import (
    AuditSeverity,
    EvaluatorKind,
    RejectionReason,
    TaskOutcome,
)
from runtime.domain.errors import OutputSchemaViolation, TaskStateError
from runtime.domain.hashing import canonical_json
from runtime.domain.ids import (
    CorrelationId,
    EvaluationId,
    OrganizationId,
    RunId,
    TaskId,
    new_evaluation_id,
)
from runtime.domain.outputs import EvaluationVerdict
from runtime.domain.schemas import SCHEMAS
from runtime.observability.logging import get_logger
from runtime.org.inbox import InboxService
from runtime.org.tasks import TaskService
from runtime.persistence.repositories.tasks import TaskRow
from runtime.persistence.uow import UnitOfWorkFactory

log = get_logger("org.evaluation")

_WORD = re.compile(r"\S+")
MAX_DISTANCE_TOKENS = 20_000
"""Cap on the token sequences fed to `difflib`. A 40 KB draft is ~8 000 tokens, so
this is generous; the cap exists because the comparison is quadratic in the worst
case and an evaluation must not be able to stall a worker."""


def edit_distance(before: Any, after: Any) -> int:
    """Word-level edits between two outputs: insertions plus deletions.

    Word-level rather than character-level because the number it produces is one a
    human can act on — "the manager rewrote 47 words" means something, "312
    characters" does not. Computed over the canonical JSON so field reordering is
    not counted as an edit.
    """
    left = _WORD.findall(canonical_json(before))[:MAX_DISTANCE_TOKENS]
    right = _WORD.findall(canonical_json(after))[:MAX_DISTANCE_TOKENS]
    matcher = difflib.SequenceMatcher(a=left, b=right, autojunk=False)
    distance = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        distance += (i2 - i1) + (j2 - j1)
    return distance


@dataclass(frozen=True, slots=True)
class VerdictResult:
    task_id: TaskId
    outcome: TaskOutcome
    evaluation_id: EvaluationId | None
    reworked: bool
    closed: bool
    edit_distance: int | None
    note: str | None = None

    @property
    def recorded(self) -> bool:
        """False when this (task, kind, attempt) already had a verdict.

        A replayed evaluation node hits this, and it must not double the
        denominator of every acceptance metric — so the second one is a no-op that
        says so rather than a second row.
        """
        return self.evaluation_id is not None


class EvaluationService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        *,
        tasks: TaskService | None = None,
        inbox: InboxService | None = None,
    ) -> None:
        self._uow = uow_factory
        self._inbox = inbox or InboxService(uow_factory)
        self._tasks = tasks or TaskService(uow_factory, inbox=self._inbox)

    # --- the manager's verdict -----------------------------------------------------

    async def apply_verdict(
        self,
        task: TaskRow,
        verdict: EvaluationVerdict,
        *,
        organization_id: OrganizationId,
        manager_name: str,
        evaluator_actor_id: Any = None,
        run_id: RunId | None = None,
        cost_cents: int = 0,
    ) -> VerdictResult:
        """Record the verdict and move the task. The cap overrides the verdict."""
        if task.status.value != "SUBMITTED":
            raise TaskStateError(
                f"task {task.id} is {task.status.value}, not SUBMITTED; nothing to evaluate"
            )

        outcome = verdict.outcome
        edited_payload: dict[str, Any] | None = None
        distance: int | None = None

        if outcome is TaskOutcome.ACCEPTED_WITH_EDITS:
            # Revalidate the manager's edit against the *task's own* schema. An
            # editor that produces a non-conforming artifact has not accepted the
            # work with edits, it has broken it, and shipping that downstream would
            # make the schema contract meaningless one level up.
            schema = SCHEMAS.get(task.output_schema_ref)
            errors = schema.check(verdict.edited_output)
            if errors:
                raise OutputSchemaViolation(
                    f"the edited output for task {task.id} does not satisfy "
                    f"{task.output_schema_ref}",
                    errors=[e.to_json() for e in errors],
                    schema_ref=task.output_schema_ref,
                )
            edited_payload = schema.validate(verdict.edited_output).model_dump(mode="json")
            distance = edit_distance(task.output or {}, edited_payload)
        elif outcome is TaskOutcome.ACCEPTED:
            distance = 0

        evaluation_id = await self._record(
            organization_id=organization_id,
            task=task,
            evaluator_kind=EvaluatorKind.MANAGER,
            outcome=outcome,
            verdict=verdict,
            evaluator_actor_id=evaluator_actor_id,
            evaluator_ref=manager_name,
            run_id=run_id,
            edit_distance=distance,
            cost_cents=cost_cents,
        )
        if evaluation_id is None:
            log.info("evaluation.duplicate", task_id=str(task.id), attempt=task.attempt)
            return VerdictResult(
                task_id=task.id,
                outcome=outcome,
                evaluation_id=None,
                reworked=False,
                closed=False,
                edit_distance=distance,
                note="already evaluated at this attempt",
            )

        if outcome is TaskOutcome.REWORK_REQUIRED:
            sent = await self._tasks.send_back_for_rework(
                task.id,
                organization_id=organization_id,
                instructions=list(verdict.rework_instructions),
                manager_name=manager_name,
            )
            if sent:
                return VerdictResult(
                    task_id=task.id,
                    outcome=outcome,
                    evaluation_id=evaluation_id,
                    reworked=True,
                    closed=False,
                    edit_distance=None,
                )
            # Cap reached. The verdict stands as recorded history; the *task*
            # closes as rejected, and the two disagreeing is the point — "the
            # manager wanted another cycle and did not get one" is a finding.
            await self._tasks.close(
                task.id,
                organization_id=organization_id,
                outcome=TaskOutcome.REJECTED,
                reason=RejectionReason.REWORK_EXHAUSTED.value,
                manager_name=manager_name,
                escalate=(
                    f"{task.title!r} exhausted its rework budget after "
                    f"{task.rework_count} cycles and has been rejected."
                ),
            )
            log.warning("evaluation.rework_exhausted", task_id=str(task.id))
            return VerdictResult(
                task_id=task.id,
                outcome=TaskOutcome.REJECTED,
                evaluation_id=evaluation_id,
                reworked=False,
                closed=True,
                edit_distance=None,
                note=RejectionReason.REWORK_EXHAUSTED.value,
            )

        reason = verdict.rejection_reason.value if verdict.rejection_reason else None
        closed = await self._tasks.close(
            task.id,
            organization_id=organization_id,
            outcome=outcome,
            reason=reason,
            manager_name=manager_name,
            output=edited_payload,
        )
        return VerdictResult(
            task_id=task.id,
            outcome=outcome,
            evaluation_id=evaluation_id,
            reworked=False,
            closed=closed,
            edit_distance=distance,
        )

    # --- the human's 20% sample ----------------------------------------------------

    async def record_human_review(
        self,
        task_id: TaskId,
        *,
        organization_id: OrganizationId,
        outcome: TaskOutcome,
        reasoning: str,
        rubric: list[dict[str, Any]] | None = None,
        reviewer: str = "operator",
        rejection_reason: str | None = None,
    ) -> VerdictResult:
        """Record the blind human review from §8.2.

        Deliberately does **not** change the task's outcome. The manager's decision
        already stands and the work has already shipped or not; the human row exists
        to make the confusion matrix real, and a sample that could retroactively
        rewrite production outcomes would stop being a measurement and start being a
        second control loop.

        It does set `human_touched`, because a person did look at this task, and the
        unassisted-completion rate should say so.
        """
        if outcome is TaskOutcome.AUTO_ACCEPTED:
            raise TaskStateError("a human review is a judgement; AUTO_ACCEPTED is its absence")

        async with self._uow.transaction() as uow:
            task = await uow.tasks.get(task_id)
            if task is None:
                raise TaskStateError(f"task {task_id} does not exist")
            evaluation_id = await uow.evaluations.record(
                new_evaluation_id(),
                organization_id=organization_id,
                task_id=task_id,
                # Pinned to the attempt the manager judged, so the self-join in
                # `v_metric_dashboard` lines the two rows up.
                attempt=task.attempt,
                evaluator_kind=EvaluatorKind.HUMAN,
                outcome=outcome,
                evaluator_ref=reviewer,
                rejection_reason=rejection_reason,
                rubric=rubric or [],
                reasoning=reasoning,
            )
            await uow.tasks.mark_human_touched(task_id)
            await uow.audit.record(
                organization_id=organization_id,
                action="task.human_review",
                target=str(task_id),
                severity=AuditSeverity.NORMAL,
                outcome=outcome.value,
                detail={"reviewer": reviewer, "recorded": evaluation_id is not None},
            )
        log.info(
            "evaluation.human",
            task_id=str(task_id),
            outcome=outcome.value,
            recorded=evaluation_id is not None,
        )
        return VerdictResult(
            task_id=task_id,
            outcome=outcome,
            evaluation_id=evaluation_id,
            reworked=False,
            closed=False,
            edit_distance=None,
        )

    # --- the deadline sweep --------------------------------------------------------

    async def auto_accept_due(
        self, *, manager_name: str, now: dt.datetime | None = None
    ) -> list[TaskId]:
        """Close every submitted task whose evaluation deadline has passed.

        `close(require_submitted=True)` re-checks `status = 'SUBMITTED' AND outcome
        IS NULL` inside the UPDATE, so a verdict that landed while this sweep was
        running wins and the task is left alone. Without that predicate a slow sweep
        would overwrite genuine evaluations with AUTO_ACCEPTED and the acceptance
        metrics would decay without anything looking wrong. T17.
        """
        _ = now
        closed: list[TaskId] = []
        async with self._uow() as uow:
            due = await uow.tasks.due_for_auto_accept()

        for task in due:
            async with self._uow.transaction() as uow:
                recorded = await uow.evaluations.record(
                    new_evaluation_id(),
                    organization_id=OrganizationId(task.organization_id),
                    task_id=task.id,
                    attempt=task.attempt,
                    evaluator_kind=EvaluatorKind.SYSTEM,
                    outcome=TaskOutcome.AUTO_ACCEPTED,
                    evaluator_ref="system:eval_deadline",
                    reasoning=(
                        "No evaluation was recorded before eval_deadline. This task was "
                        "not judged; it is excluded from every acceptance metric."
                    ),
                )
            won = await self._tasks.close(
                task.id,
                organization_id=OrganizationId(task.organization_id),
                outcome=TaskOutcome.AUTO_ACCEPTED,
                reason="eval_deadline elapsed",
                manager_name=manager_name,
                require_submitted=True,
            )
            if won:
                closed.append(task.id)
                log.warning(
                    "evaluation.auto_accepted",
                    task_id=str(task.id),
                    title=task.title,
                    recorded=recorded is not None,
                )
        return closed

    # --- reads ---------------------------------------------------------------------

    async def _record(
        self,
        *,
        organization_id: OrganizationId,
        task: TaskRow,
        evaluator_kind: EvaluatorKind,
        outcome: TaskOutcome,
        verdict: EvaluationVerdict,
        evaluator_actor_id: Any,
        evaluator_ref: str,
        run_id: RunId | None,
        edit_distance: int | None,
        cost_cents: int,
    ) -> EvaluationId | None:
        async with self._uow.transaction() as uow:
            evaluation_id = await uow.evaluations.record(
                new_evaluation_id(),
                organization_id=organization_id,
                task_id=task.id,
                attempt=task.attempt,
                evaluator_kind=evaluator_kind,
                outcome=outcome,
                evaluator_actor_id=evaluator_actor_id,
                evaluator_ref=evaluator_ref,
                run_id=run_id,
                rejection_reason=(
                    verdict.rejection_reason.value if verdict.rejection_reason else None
                ),
                rubric=[s.model_dump(mode="json") for s in verdict.rubric],
                reasoning=verdict.reasoning,
                rework_instructions=list(verdict.rework_instructions),
                edit_distance=edit_distance,
                cost_cents=cost_cents,
            )
        _ = CorrelationId
        return evaluation_id

    async def for_task(self, task_id: TaskId) -> list[dict[str, Any]]:
        async with self._uow() as uow:
            return await uow.evaluations.for_task(task_id)

    async def sampling_frame(
        self, organization_id: OrganizationId, since: dt.datetime
    ) -> list[dict[str, Any]]:
        """Manager-accepted tasks with no human row yet. §8.2's population."""
        async with self._uow() as uow:
            return await uow.evaluations.manager_accepted_unsampled(organization_id, since)
