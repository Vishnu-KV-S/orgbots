"""The human evaluation sampling harness. §8.2.

*"The human sample is 20%, stratified, and non-negotiable. Every week, review 20% of
manager-accepted tasks against the rubric, blind to the manager's reasoning. Record
your outcome as a second row in `task_evaluations`."*

Four properties, each of which is a way this harness could be quietly useless.

**Blind.** `render()` shows the task, the artifact and the rubric. It does not show
the manager's outcome, its reasoning, its rubric scores or its edit distance, and
`--show-verdict` does not exist. A reviewer who has seen the manager say "accepted,
all criteria met" is not producing an independent judgement, and the false-accept
rate — the number §8.2 says to watch, because it flatters every other metric while
quality falls — would measure agreement with a prompt rather than agreement about
work.

**Stratified.** Sampling 20% of everything would, in a week where research produced
six tasks and content produced one, review the content task about one week in five.
The strata are output schemas, and each gets its own 20% with a floor of one.

**Deterministic.** The selection is seeded by the week, so running the harness twice
produces the same sample. §8's whole concern is that a measurement can be gamed by
the person who wants the answer to be yes; a sample that could be re-rolled until it
looked better would be exactly that, and this makes re-rolling visibly impossible
rather than merely discouraged.

**Idempotent.** `uq_evaluation_once` means a second review of the same task
conflicts rather than appending. The harness reports it as already-reviewed instead
of silently doubling a denominator.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from runtime.domain.enums import TaskOutcome
from runtime.domain.ids import OrganizationId, TaskId
from runtime.org.evaluation import EvaluationService
from runtime.org.rubrics import RUBRIC_VERSION, rubric_for
from runtime.org.tasks import TaskService
from runtime.persistence.uow import UnitOfWorkFactory

SAMPLE_FRACTION = 0.20
"""§8.2. Non-negotiable — changing it resets the §8.4 clean-run clock."""

MIN_PER_STRATUM = 1
"""A stratum with any accepted work gets at least one review. Rounding 20% of two
tasks down to zero is how a task type goes unaudited for a month."""


@dataclass(frozen=True, slots=True)
class SampleItem:
    task_id: TaskId
    title: str
    schema_ref: str
    assignee: str
    created_at: dt.datetime

    def sort_key(self, seed: str) -> str:
        """Deterministic, unpredictable ordering.

        Hashed with the week's seed rather than sorted by id: a plain sort would
        always review the same corner of the id space, and an unseeded shuffle would
        let the sample be re-rolled until it looked better.
        """
        return hashlib.sha256(f"{seed}\x1f{self.task_id}".encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Sample:
    week_start: dt.date
    population: int
    selected: list[SampleItem]
    by_stratum: dict[str, tuple[int, int]]
    """schema_ref → (population, selected)."""

    @property
    def fraction(self) -> float:
        return len(self.selected) / self.population if self.population else 0.0


class SamplingHarness:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow = uow_factory
        self._evaluation = EvaluationService(uow_factory)
        self._tasks = TaskService(uow_factory)

    async def draw(
        self,
        organization_id: OrganizationId,
        week_start: dt.date,
        *,
        fraction: float = SAMPLE_FRACTION,
    ) -> Sample:
        """Draw this week's sample from manager-accepted, not-yet-reviewed tasks."""
        since = dt.datetime.combine(week_start, dt.time.min, tzinfo=dt.UTC)
        until = since + dt.timedelta(days=7)
        frame = await self._evaluation.sampling_frame(organization_id, since)

        strata: dict[str, list[SampleItem]] = defaultdict(list)
        for row in frame:
            created = row["created_at"]
            if not (since <= created < until):
                continue
            strata[row["output_schema_ref"]].append(
                SampleItem(
                    task_id=TaskId(row["task_id"]),
                    title=row["title"],
                    schema_ref=row["output_schema_ref"],
                    assignee=row["assignee_name"] or "",
                    created_at=created,
                )
            )

        seed = f"{organization_id}:{week_start.isoformat()}"
        selected: list[SampleItem] = []
        by_stratum: dict[str, tuple[int, int]] = {}
        for schema_ref, items in sorted(strata.items()):
            take = max(MIN_PER_STRATUM, math.ceil(len(items) * fraction))
            take = min(take, len(items))
            ordered = sorted(items, key=lambda i: i.sort_key(seed))
            selected.extend(ordered[:take])
            by_stratum[schema_ref] = (len(items), take)

        return Sample(
            week_start=week_start,
            population=sum(len(v) for v in strata.values()),
            selected=selected,
            by_stratum=by_stratum,
        )

    async def render(self, item: SampleItem) -> str:
        """What the reviewer reads. The manager's verdict is deliberately absent."""
        task = await self._tasks.get(item.task_id)
        if task is None:
            return f"task {item.task_id} no longer exists"
        rubric = rubric_for(task.output_schema_ref)
        body = json.dumps(task.output or {}, indent=2, sort_keys=True, default=str)
        return "\n".join(
            [
                "=" * 78,
                f"TASK   {task.id}",
                f"TITLE  {task.title}",
                f"WHO    {task.assignee_name}   SCHEMA {task.output_schema_ref}",
                f"ATTEMPT {task.rework_count + 1}   SCHEMA FAILURES {task.schema_failures}",
                "=" * 78,
                "",
                "OBJECTIVE",
                task.objective,
                "",
                "ACCEPTANCE CRITERIA",
                *(f"  - {c}" for c in task.acceptance_criteria),
                "",
                rubric.as_prompt(),
                "",
                "-" * 78,
                "OUTPUT",
                "-" * 78,
                body,
                "",
                "-" * 78,
                "Judge it against the rubric above. You have not been shown what the",
                "manager decided, and there is no flag to show you — §8.2 requires this",
                "review to be blind, because the number being tracked is how often the",
                "manager accepted something you would reject.",
                "-" * 78,
            ]
        )

    async def record(
        self,
        task_id: TaskId,
        *,
        organization_id: OrganizationId,
        outcome: TaskOutcome,
        reasoning: str,
        criteria_met: dict[str, bool] | None = None,
        reviewer: str = "operator",
    ) -> bool:
        """Write the human row. False when this task was already reviewed."""
        task = await self._tasks.get(task_id)
        rubric = rubric_for(task.output_schema_ref) if task else None
        payload = [
            {
                "criterion": criterion,
                "met": bool(met),
                "note": "recorded by the human sampling harness",
                "rubric_version": RUBRIC_VERSION,
            }
            for criterion, met in (criteria_met or {}).items()
        ] or (
            [
                {
                    "criterion": c,
                    "met": outcome.is_accepted,
                    "note": "not scored individually",
                    "rubric_version": RUBRIC_VERSION,
                }
                for c in (rubric.criteria if rubric else ())
            ]
        )
        result = await self._evaluation.record_human_review(
            task_id,
            organization_id=organization_id,
            outcome=outcome,
            reasoning=reasoning,
            rubric=payload,
            reviewer=reviewer,
        )
        return result.recorded

    async def confusion(
        self, organization_id: OrganizationId, week_start: dt.date
    ) -> dict[str, Any]:
        """The §8.2 confusion matrix, straight out of `v_metric_dashboard`.

        Read from the view rather than recomputed here, so the number in this
        report and the number on the dashboard cannot drift apart.
        """
        async with self._uow() as uow:
            rows = await uow.metrics.weekly("v_metric_dashboard", organization_id, week_start)
        if not rows:
            return {"week_start": week_start.isoformat(), "sampled": 0}
        row = rows[0]
        return {
            "week_start": week_start.isoformat(),
            "sampled": int(row["human_sampled_tasks"] or 0),
            "agreements": int(row["agreements"] or 0),
            "agreement_rate": _f(row["human_agreement"]),
            "false_accepts": int(row["false_accepts"] or 0),
            "false_accept_rate": _f(row["false_accept_rate"]),
        }


def _f(value: Any) -> float | None:
    return None if value is None else float(value)
