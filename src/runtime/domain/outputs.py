"""The pinned task output schemas.

PR-14 writes these *first*, before any actor, because they are the contract the
whole milestone is measured against. An agent's job is to produce one of these; the
evaluator's job is to judge one of these; a schema failure is a task lifecycle event
with a cap on it. Nothing else in M1 is meaningful until these are.

§10 says the thing worth repeating here: *are the output schemas actually
constraining?* A `recommendation: str` with `min_length=50` permits fifty
characters of confident nonsense, and tightening a schema is free and measurable
where tightening a prompt is neither. So every model below carries at least one
**cross-field** validator — a constraint that cannot be satisfied by padding a
string:

- `CompetitorReport` — every recommendation must cite a source that exists, and
  every theme must name competitors the report actually covers. An agent that
  invents a citation index fails validation rather than producing a plausible
  report with a dangling reference.
- `ContentDraft` — the declared `word_count` must match the body it describes to
  within 10%, and every claim must cite a source. Self-reported metadata that
  disagrees with the artifact is the cheapest possible lie to catch.
- `MetricsReport` — the rates must be arithmetically consistent with the counts
  they are derived from. The deterministic actor computes both; a mismatch means a
  bug in the SQL, which is exactly what T26 exists to find.
- `EvaluationVerdict` — the fields required depend on the outcome. `REWORK_REQUIRED`
  without instructions and `REJECTED` without a reason both fail. This is the
  schema-level half of "the head agent is a rubber stamp" (§10); the 20% human
  sample is the other half.

Pure Pydantic. No I/O, no database, no imports outside `runtime.domain`.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from runtime.domain.enums import RejectionReason, TaskOutcome
from runtime.domain.schemas import SCHEMAS

Url = Annotated[str, Field(pattern=r"^https?://\S+$", max_length=2048)]
Confidence = Literal["low", "medium", "high"]
Effort = Literal["S", "M", "L"]

_WORD = re.compile(r"\b[\w'-]+\b")


def word_count(text: str) -> int:
    return len(_WORD.findall(text))


class Output(BaseModel):
    """Base for every pinned output.

    `extra="forbid"` is the load-bearing setting. Without it an agent can return
    the required fields plus a `"note": "I couldn't find pricing"` and validation
    passes while the caveat goes nowhere — the failure mode where the schema says
    the work is complete and the work says otherwise.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


# --- shared pieces -----------------------------------------------------------------


class Source(Output):
    """One citation. `quote` is required and must be substantial.

    A citation with a URL and no quote is unfalsifiable — nobody checks it, and an
    agent learns it can emit any plausible URL. Requiring twenty characters of
    quoted text means a fabricated source is visibly fabricated.
    """

    url: Url
    title: str = Field(min_length=3, max_length=300)
    quote: str = Field(min_length=20, max_length=800)
    retrieved_at: dt.date


# --- CompetitorReport@1 ------------------------------------------------------------


class CompetitorEntry(Output):
    name: str = Field(min_length=2, max_length=120)
    url: Url | None = None
    positioning: str = Field(min_length=60, max_length=600)
    pricing_note: str | None = Field(default=None, max_length=400)
    source_indices: list[int] = Field(min_length=1, max_length=10)
    """Indices into `CompetitorReport.sources`. Validated for existence."""


class Theme(Output):
    statement: str = Field(min_length=60, max_length=500)
    competitors: list[str] = Field(min_length=1, max_length=8)
    """Names that must appear in `CompetitorReport.competitors`."""
    confidence: Confidence


class Recommendation(Output):
    action: str = Field(min_length=40, max_length=400)
    rationale: str = Field(min_length=60, max_length=800)
    effort: Effort
    source_indices: list[int] = Field(min_length=1, max_length=10)


class CompetitorReport(Output):
    """What `research` produces. WORK class."""

    subject: str = Field(min_length=3, max_length=200)
    as_of: dt.date
    summary: str = Field(min_length=120, max_length=1500)
    competitors: list[CompetitorEntry] = Field(min_length=2, max_length=8)
    themes: list[Theme] = Field(min_length=2, max_length=6)
    recommendations: list[Recommendation] = Field(min_length=1, max_length=5)
    sources: list[Source] = Field(min_length=3, max_length=30)
    gaps: list[str] = Field(default_factory=list, max_length=8)
    """What the research could not establish. Explicitly modelled so "I didn't find
    pricing" has somewhere to go other than a fabricated pricing note."""

    @model_validator(mode="after")
    def _citations_resolve(self) -> Self:
        n = len(self.sources)
        for entry in self.competitors:
            _check_indices(entry.source_indices, n, f"competitor {entry.name!r}")
        for rec in self.recommendations:
            _check_indices(rec.source_indices, n, "recommendation")
        known = {c.name for c in self.competitors}
        for theme in self.themes:
            unknown = sorted(set(theme.competitors) - known)
            if unknown:
                raise ValueError(
                    f"theme names competitors the report does not cover: {unknown}; "
                    f"covered: {sorted(known)}"
                )
        return self


# --- ContentDraft@1 ----------------------------------------------------------------


class Section(Output):
    heading: str = Field(min_length=3, max_length=140)
    key_point: str = Field(min_length=40, max_length=600)


class Claim(Output):
    statement: str = Field(min_length=20, max_length=500)
    source_index: int = Field(ge=0)


class ContentDraft(Output):
    """What `content` produces. WORK class."""

    title: str = Field(min_length=10, max_length=140)
    channel: Literal["blog", "newsletter", "landing_page", "social"]
    audience: str = Field(min_length=10, max_length=200)
    thesis: str = Field(min_length=60, max_length=500)
    sections: list[Section] = Field(min_length=2, max_length=10)
    body_markdown: str = Field(min_length=600, max_length=40_000)
    call_to_action: str = Field(min_length=20, max_length=300)
    claims: list[Claim] = Field(default_factory=list, max_length=20)
    sources: list[Source] = Field(min_length=1, max_length=20)
    word_count: int = Field(ge=100, le=8000)

    @model_validator(mode="after")
    def _self_report_matches_artifact(self) -> Self:
        actual = word_count(self.body_markdown)
        if not 0.9 * actual <= self.word_count <= 1.1 * actual:
            raise ValueError(
                f"word_count={self.word_count} disagrees with body_markdown "
                f"({actual} words); metadata must describe the artifact it ships with"
            )
        for claim in self.claims:
            _check_indices([claim.source_index], len(self.sources), "claim")
        return self


# --- MetricsReport@1 ---------------------------------------------------------------

Ratio = Annotated[float, Field(ge=0.0, le=1.0)]


class MetricsReport(Output):
    """What `analytics` produces. DETERMINISTIC — zero model calls.

    The one output in M1 with no LLM anywhere in its provenance, which is why it is
    also the one the go/no-go decision is read off. If this is wrong, every number
    in §9 is wrong and nothing else would catch it — see T26.
    """

    period_start: dt.date
    period_end: dt.date

    submitted_tasks: int = Field(ge=0)
    evaluated_tasks: int = Field(ge=0)
    accepted_tasks: int = Field(ge=0)
    """ACCEPTED + ACCEPTED_WITH_EDITS. Excludes AUTO_ACCEPTED, deliberately."""
    auto_accepted_tasks: int = Field(ge=0)
    rejected_tasks: int = Field(ge=0)
    bounced_tasks: int = Field(default=0, ge=0)
    """Submitted tasks that were sent back at least once, whatever became of them
    afterwards. This — not `rejected_tasks` — is what `rejection_rate` is over: a
    task reworked twice and then accepted still cost two cycles, and scoring it as
    a clean pass would hide exactly the number the metric exists to expose."""

    total_spend_cents: int = Field(ge=0)
    cost_per_accepted_cents: int | None = Field(default=None, ge=0)
    rejection_rate: Ratio | None = None
    unassisted_completion_rate: Ratio | None = None
    coordination_ratio: Ratio | None = None
    overhead_ratio: Ratio | None = None
    auto_accepted_share: Ratio | None = None
    mean_edit_distance: float | None = Field(default=None, ge=0.0)

    human_sampled_tasks: int = Field(default=0, ge=0)
    false_accepts: int = Field(default=0, ge=0)
    human_agreement: Ratio | None = None

    spend_by_work_class_cents: dict[str, int] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def _counts_and_rates_agree(self) -> Self:
        if self.period_end < self.period_start:
            raise ValueError("period_end precedes period_start")
        if self.evaluated_tasks > self.submitted_tasks:
            raise ValueError(
                f"evaluated_tasks={self.evaluated_tasks} exceeds "
                f"submitted_tasks={self.submitted_tasks}"
            )
        judged = self.accepted_tasks + self.auto_accepted_tasks + self.rejected_tasks
        if judged > self.evaluated_tasks:
            raise ValueError(
                f"accepted+auto+rejected={judged} exceeds evaluated_tasks={self.evaluated_tasks}"
            )
        if self.bounced_tasks > self.submitted_tasks:
            raise ValueError(
                f"bounced_tasks={self.bounced_tasks} exceeds submitted_tasks={self.submitted_tasks}"
            )
        if self.false_accepts > self.human_sampled_tasks:
            raise ValueError("false_accepts exceeds the number of tasks sampled")
        _check_rate(self.rejection_rate, self.bounced_tasks, self.submitted_tasks, "rejection_rate")
        _check_rate(
            self.auto_accepted_share,
            self.auto_accepted_tasks,
            self.evaluated_tasks,
            "auto_accepted_share",
        )
        if self.cost_per_accepted_cents is not None and self.accepted_tasks == 0:
            raise ValueError("cost_per_accepted_cents is defined with zero accepted tasks")
        return self


# --- WeeklyPlan@1 ------------------------------------------------------------------


class PlannedTask(Output):
    title: str = Field(min_length=10, max_length=200)
    assignee: str = Field(min_length=2, max_length=64)
    objective: str = Field(min_length=80, max_length=1200)
    """The §10 diagnosis says most rejections are decomposition failures, not
    capability failures. Eighty characters is the floor at which an objective can
    carry enough context for the assignee to stop guessing."""
    output_schema_ref: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_]*@[1-9][0-9]*$")
    acceptance_criteria: list[str] = Field(min_length=2, max_length=6)
    task_input: dict[str, str | int | float | bool | list[str]] = Field(default_factory=dict)
    due_offset_days: int = Field(ge=1, le=6)

    @model_validator(mode="after")
    def _criteria_are_substantial(self) -> Self:
        for criterion in self.acceptance_criteria:
            if len(criterion.strip()) < 25:
                raise ValueError(f"acceptance criterion {criterion!r} is too short to be checkable")
        return self


class WeeklyPlan(Output):
    """`marketing-head`'s Monday output. COORDINATION class."""

    week_of: dt.date
    goal_restatement: str = Field(min_length=60, max_length=800)
    tasks: list[PlannedTask] = Field(min_length=3, max_length=6)
    rationale: str = Field(min_length=80, max_length=1500)

    @model_validator(mode="after")
    def _schemas_are_registered(self) -> Self:
        for task in self.tasks:
            if not SCHEMAS.has(task.output_schema_ref):
                raise ValueError(
                    f"task {task.title!r} pins {task.output_schema_ref!r}, which is not a "
                    f"registered schema; known: {sorted(SCHEMAS.refs())}"
                )
        return self


# --- WeeklySummary@1 ---------------------------------------------------------------


class WeeklySummary(Output):
    """`marketing-head`'s Friday output — the artifact a human actually reads."""

    week_of: dt.date
    headline: str = Field(min_length=30, max_length=300)
    shipped: list[str] = Field(default_factory=list, max_length=10)
    not_shipped: list[str] = Field(default_factory=list, max_length=10)
    metrics_note: str = Field(min_length=40, max_length=1200)
    decisions_needed: list[str] = Field(default_factory=list, max_length=6)
    next_week_focus: str = Field(min_length=60, max_length=800)

    @model_validator(mode="after")
    def _says_something(self) -> Self:
        if not self.shipped and not self.not_shipped:
            raise ValueError("a week in which nothing shipped and nothing slipped is not a week")
        return self


# --- EvaluationVerdict@1 -----------------------------------------------------------


class RubricScore(Output):
    criterion: str = Field(min_length=10, max_length=300)
    met: bool
    note: str = Field(min_length=20, max_length=600)


class EvaluationVerdict(Output):
    """`marketing-head`'s judgement of one submitted task. EVALUATION class.

    The conditional requirements below are the schema's share of the anti-rubber-
    stamp work: an evaluator cannot say REWORK_REQUIRED without saying what to
    change, or REJECTED without saying which kind of wrong it was. What the schema
    cannot catch — a well-formed verdict that is simply too generous — is what the
    20% human sample in §8.2 is for.
    """

    outcome: TaskOutcome
    rubric: list[RubricScore] = Field(min_length=1, max_length=10)
    reasoning: str = Field(min_length=80, max_length=2000)
    rework_instructions: list[str] = Field(default_factory=list, max_length=6)
    rejection_reason: RejectionReason | None = None
    edited_output: dict[str, object] | None = None
    """Present only for ACCEPTED_WITH_EDITS: the corrected artifact, revalidated
    against the task's own schema before it is stored. Its distance from the
    submission is the `edit_distance` the dashboard trends."""

    @model_validator(mode="after")
    def _outcome_implies_its_evidence(self) -> Self:
        if self.outcome is TaskOutcome.AUTO_ACCEPTED:
            raise ValueError(
                "AUTO_ACCEPTED is recorded by the deadline sweeper, never returned by "
                "an evaluator; an evaluator that reached the task has evaluated it"
            )
        if self.outcome is TaskOutcome.REWORK_REQUIRED and not self.rework_instructions:
            raise ValueError("REWORK_REQUIRED must say what to change")
        if self.outcome is TaskOutcome.REJECTED and self.rejection_reason is None:
            raise ValueError("REJECTED must name a rejection_reason")
        if self.outcome is TaskOutcome.ACCEPTED_WITH_EDITS and self.edited_output is None:
            raise ValueError("ACCEPTED_WITH_EDITS must ship the edited output")
        if self.outcome is TaskOutcome.ACCEPTED and any(not s.met for s in self.rubric):
            raise ValueError(
                "ACCEPTED with an unmet rubric criterion; use ACCEPTED_WITH_EDITS or "
                "REWORK_REQUIRED"
            )
        return self


# --- helpers -----------------------------------------------------------------------


def _check_indices(indices: list[int], size: int, what: str) -> None:
    bad = [i for i in indices if i < 0 or i >= size]
    if bad:
        raise ValueError(f"{what} cites source indices {bad} but the report has {size} sources")


def _check_rate(rate: float | None, numerator: int, denominator: int, name: str) -> None:
    """Rates are derived, so a mismatch means the deriving code is wrong."""
    if rate is None:
        return
    if denominator == 0:
        raise ValueError(f"{name} is defined with a zero denominator")
    expected = numerator / denominator
    if abs(rate - expected) > 1e-6:
        raise ValueError(f"{name}={rate!r} disagrees with {numerator}/{denominator}={expected!r}")


COMPETITOR_REPORT_V1 = SCHEMAS.register(CompetitorReport, version=1)
CONTENT_DRAFT_V1 = SCHEMAS.register(ContentDraft, version=1)
METRICS_REPORT_V1 = SCHEMAS.register(MetricsReport, version=1)
WEEKLY_PLAN_V1 = SCHEMAS.register(WeeklyPlan, version=1)
WEEKLY_SUMMARY_V1 = SCHEMAS.register(WeeklySummary, version=1)
EVALUATION_VERDICT_V1 = SCHEMAS.register(EvaluationVerdict, version=1)

TASK_OUTPUT_SCHEMAS = frozenset(
    {COMPETITOR_REPORT_V1, CONTENT_DRAFT_V1, METRICS_REPORT_V1, WEEKLY_SUMMARY_V1}
)
"""The four a task may pin. `WeeklyPlan` and `EvaluationVerdict` are the head's own
call outputs, not deliverables anyone is assigned."""
