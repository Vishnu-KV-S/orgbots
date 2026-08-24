"""The schema registry and the four pinned outputs.

Pure — no database, no Redis, no event loop. These are the contracts M1 is measured
against and they should be testable with nothing running.

The interesting assertions are on the **cross-field** validators. §10 asks "are the
output schemas actually constraining?" and answers that a `recommendation: str` with
`min_length=50` permits fifty characters of confident nonsense. Every test below
takes a *valid* instance and breaks exactly one cross-field relationship, because
that is the failure a fluent model actually produces — well-formed fields that do
not agree with each other.
"""

from __future__ import annotations

import datetime as dt

import pytest
from pydantic import BaseModel, ValidationError

from runtime.domain.errors import OutputSchemaViolation, SpecError
from runtime.domain.outputs import (
    COMPETITOR_REPORT_V1,
    CONTENT_DRAFT_V1,
    EVALUATION_VERDICT_V1,
    METRICS_REPORT_V1,
    TASK_OUTPUT_SCHEMAS,
    WEEKLY_PLAN_V1,
    WEEKLY_SUMMARY_V1,
    CompetitorReport,
    ContentDraft,
    EvaluationVerdict,
    MetricsReport,
    WeeklyPlan,
    WeeklySummary,
    word_count,
)
from runtime.domain.schemas import SCHEMAS, SchemaRegistry
from tests.conftest_m1 import (
    TODAY,
    sample_competitor_report,
    sample_content_draft,
    sample_metrics_report,
    sample_verdict,
    sample_weekly_plan,
    sample_weekly_summary,
)

# --- the registry --------------------------------------------------------------------


def test_the_six_schemas_m1_pins() -> None:
    assert SCHEMAS.refs() >= {
        COMPETITOR_REPORT_V1,
        CONTENT_DRAFT_V1,
        METRICS_REPORT_V1,
        WEEKLY_PLAN_V1,
        WEEKLY_SUMMARY_V1,
        EVALUATION_VERDICT_V1,
    }
    assert {
        COMPETITOR_REPORT_V1,
        CONTENT_DRAFT_V1,
        METRICS_REPORT_V1,
        WEEKLY_SUMMARY_V1,
    } == TASK_OUTPUT_SCHEMAS


def test_a_registered_version_is_immutable() -> None:
    """Changing fields means `@2`.

    Without this a task written on Monday and evaluated on Friday could be judged
    against a schema that did not exist when the work was assigned, and "did it meet
    the spec" would stop being answerable.
    """
    registry = SchemaRegistry()

    class Report(BaseModel):
        a: int

    class Different(BaseModel):
        a: int
        b: int

    ref = registry.register(Report, version=1)
    assert registry.register(Report, version=1) == ref, "same model is idempotent"

    with pytest.raises(SpecError, match="immutable"):
        registry.register(Different, version=1, name="Report")

    assert registry.register(Different, version=2, name="Report") == "Report@2"


def test_an_unknown_ref_names_what_is_known() -> None:
    with pytest.raises(SpecError, match="known:"):
        SCHEMAS.get("Imaginary@1")


def test_validation_returns_pointers_not_a_stringified_exception() -> None:
    """The error list is what the retry prompt and the rework instruction are built
    from, so it has to survive a round trip through a JSONB column."""
    schema = SCHEMAS.get(COMPETITOR_REPORT_V1)
    errors = schema.check(sample_competitor_report(sources=[]))

    assert errors
    assert all(e.pointer.startswith("/") for e in errors)
    assert all(isinstance(e.to_json(), dict) for e in errors)
    assert any("/sources" in e.pointer for e in errors)


def test_the_registry_exposes_json_schema_for_the_provider() -> None:
    """Sent to the model, not described to it. `output_config.format` takes this."""
    schema = SCHEMAS.get(COMPETITOR_REPORT_V1)
    assert schema.json_schema["type"] == "object"
    assert "competitors" in schema.json_schema["properties"]


def test_validate_raises_a_typed_error_carrying_the_ref() -> None:
    schema = SCHEMAS.get(CONTENT_DRAFT_V1)
    with pytest.raises(OutputSchemaViolation) as exc:
        schema.validate({"title": "too short"})
    assert exc.value.schema_ref == CONTENT_DRAFT_V1
    assert exc.value.errors


# --- CompetitorReport -----------------------------------------------------------------


def test_a_valid_competitor_report_validates() -> None:
    assert CompetitorReport.model_validate(sample_competitor_report())


def test_a_recommendation_citing_a_source_that_does_not_exist_fails() -> None:
    """An agent that invents a citation index fails validation rather than producing
    a plausible report with a dangling reference."""
    broken = sample_competitor_report()
    broken["recommendations"][0]["source_indices"] = [0, 99]

    with pytest.raises(ValidationError, match="cites source indices"):
        CompetitorReport.model_validate(broken)


def test_a_theme_naming_a_competitor_the_report_does_not_cover_fails() -> None:
    broken = sample_competitor_report()
    broken["themes"][0]["competitors"] = ["A Vendor Nobody Researched"]

    with pytest.raises(ValidationError, match="does not cover"):
        CompetitorReport.model_validate(broken)


def test_extra_keys_are_forbidden() -> None:
    """Otherwise an agent returns the required fields plus a caveat that goes
    nowhere — the schema says complete while the work says otherwise."""
    broken = sample_competitor_report()
    broken["note"] = "I could not actually find pricing for any of these"

    with pytest.raises(ValidationError, match=r"[Ee]xtra"):
        CompetitorReport.model_validate(broken)


def test_a_source_needs_a_substantial_quote() -> None:
    """A citation with a URL and no quote is unfalsifiable, so an agent learns it
    can emit any plausible URL."""
    broken = sample_competitor_report()
    broken["sources"][0]["quote"] = "yes"

    with pytest.raises(ValidationError):
        CompetitorReport.model_validate(broken)


def test_one_competitor_is_not_a_competitor_report() -> None:
    with pytest.raises(ValidationError):
        CompetitorReport.model_validate(
            sample_competitor_report(competitors=[sample_competitor_report()["competitors"][0]])
        )


# --- ContentDraft ----------------------------------------------------------------------


def test_a_valid_content_draft_validates() -> None:
    assert ContentDraft.model_validate(sample_content_draft())


def test_a_word_count_that_disagrees_with_the_body_fails() -> None:
    """The cheapest possible lie to catch. An agent that will misreport a number it
    could have counted will misreport others."""
    broken = sample_content_draft()
    broken["word_count"] = 5000

    with pytest.raises(ValidationError, match="disagrees with body_markdown"):
        ContentDraft.model_validate(broken)


def test_a_word_count_within_ten_percent_is_accepted() -> None:
    """Tokenisation differences are real; a 10% band is the tolerance."""
    draft = sample_content_draft()
    actual = word_count(draft["body_markdown"])
    draft["word_count"] = int(actual * 1.05)
    assert ContentDraft.model_validate(draft)


def test_a_claim_citing_a_missing_source_fails() -> None:
    broken = sample_content_draft()
    broken["claims"][0]["source_index"] = 7

    with pytest.raises(ValidationError, match="cites source indices"):
        ContentDraft.model_validate(broken)


# --- MetricsReport ----------------------------------------------------------------------


def test_a_rate_inconsistent_with_its_counts_fails() -> None:
    """The deterministic actor computes both, so a mismatch means the SQL is wrong.
    That is most of what T26 buys beyond its own assertions."""
    with pytest.raises(ValidationError, match="rejection_rate"):
        MetricsReport.model_validate(
            sample_metrics_report(submitted_tasks=10, bounced_tasks=3, rejection_rate=0.9)
        )


def test_accepted_cannot_exceed_evaluated() -> None:
    with pytest.raises(ValidationError, match="exceeds evaluated_tasks"):
        MetricsReport.model_validate(
            sample_metrics_report(submitted_tasks=5, evaluated_tasks=1, accepted_tasks=4)
        )


def test_cost_per_accepted_with_zero_accepted_is_incoherent() -> None:
    """ "$4 per accepted outcome" with nothing accepted is not a number anybody can
    defend, which §9 explicitly requires."""
    with pytest.raises(ValidationError, match="zero accepted tasks"):
        MetricsReport.model_validate(
            sample_metrics_report(accepted_tasks=0, cost_per_accepted_cents=400)
        )


def test_false_accepts_cannot_exceed_the_sample() -> None:
    with pytest.raises(ValidationError, match="exceeds the number of tasks sampled"):
        MetricsReport.model_validate(sample_metrics_report(human_sampled_tasks=2, false_accepts=3))


def test_an_empty_metrics_report_is_valid() -> None:
    """A week with nothing in it is a real week, not a validation failure."""
    assert MetricsReport.model_validate(sample_metrics_report())


# --- WeeklyPlan -------------------------------------------------------------------------


def test_a_valid_plan_validates() -> None:
    assert WeeklyPlan.model_validate(sample_weekly_plan())


def test_a_plan_pinning_an_unregistered_schema_fails() -> None:
    """The task would be unevaluable, and it would not be discovered until the
    assignee tried to submit — days later, on the wrong actor."""
    broken = sample_weekly_plan()
    broken["tasks"][0]["output_schema_ref"] = "SomethingImagined@1"

    with pytest.raises(ValidationError, match="not a registered schema"):
        WeeklyPlan.model_validate(broken)


def test_an_uncheckable_acceptance_criterion_fails() -> None:
    broken = sample_weekly_plan()
    broken["tasks"][0]["acceptance_criteria"] = ["good", "fast"]

    with pytest.raises(ValidationError, match="too short to be checkable"):
        WeeklyPlan.model_validate(broken)


def test_a_plan_of_two_tasks_is_not_a_week() -> None:
    broken = sample_weekly_plan()
    broken["tasks"] = broken["tasks"][:2]

    with pytest.raises(ValidationError):
        WeeklyPlan.model_validate(broken)


def test_a_thin_objective_fails() -> None:
    """§10: most rejected work is right about a different question, and a thin
    objective is where that starts."""
    broken = sample_weekly_plan()
    broken["tasks"][0]["objective"] = "Research competitors."

    with pytest.raises(ValidationError):
        WeeklyPlan.model_validate(broken)


# --- EvaluationVerdict -------------------------------------------------------------------


def test_rework_without_instructions_fails() -> None:
    with pytest.raises(ValidationError, match="must say what to change"):
        EvaluationVerdict.model_validate(sample_verdict("REWORK_REQUIRED", rework_instructions=[]))


def test_rejection_without_a_reason_fails() -> None:
    with pytest.raises(ValidationError, match="must name a rejection_reason"):
        EvaluationVerdict.model_validate(sample_verdict("REJECTED", rejection_reason=None))


def test_accepted_with_edits_must_ship_the_edit() -> None:
    with pytest.raises(ValidationError, match="must ship the edited output"):
        EvaluationVerdict.model_validate(sample_verdict("ACCEPTED_WITH_EDITS", edited_output=None))


def test_accepting_with_an_unmet_criterion_fails() -> None:
    """The schema's share of the anti-rubber-stamp work. An evaluator that notes a
    failure and accepts anyway has to pick a different outcome."""
    broken = sample_verdict("ACCEPTED")
    broken["rubric"][0]["met"] = False

    with pytest.raises(ValidationError, match="unmet rubric criterion"):
        EvaluationVerdict.model_validate(broken)


def test_an_evaluator_cannot_return_auto_accepted() -> None:
    with pytest.raises(ValidationError, match="deadline sweeper"):
        EvaluationVerdict.model_validate(sample_verdict("AUTO_ACCEPTED"))


def test_a_verdict_needs_reasoning_of_substance() -> None:
    with pytest.raises(ValidationError):
        EvaluationVerdict.model_validate(sample_verdict("ACCEPTED", reasoning="lgtm"))


# --- WeeklySummary ------------------------------------------------------------------------


def test_a_week_with_nothing_shipped_and_nothing_slipped_is_not_a_week() -> None:
    with pytest.raises(ValidationError, match="not a week"):
        WeeklySummary.model_validate(sample_weekly_summary(shipped=[], not_shipped=[]))


def test_a_valid_summary_validates() -> None:
    assert WeeklySummary.model_validate(sample_weekly_summary())
    assert TODAY.weekday() == 0, "the fixture week starts on a Monday"
    assert dt.date.fromisoformat(sample_weekly_summary()["week_of"]) == TODAY


# --- rubrics ------------------------------------------------------------------------------


def test_every_task_output_schema_has_a_written_rubric() -> None:
    """§8.1 requires one paragraph per task type *before* anything of that type is
    evaluated. A missing rubric is a task type that cannot be judged honestly."""
    from runtime.org.rubrics import RUBRICS, rubric_for

    for ref in TASK_OUTPUT_SCHEMAS:
        rubric = rubric_for(ref)
        assert len(rubric.what_good_looks_like) > 400, "one paragraph, not one line"
        assert len(rubric.criteria) >= 4
        assert ref in rubric.as_prompt()
    assert set(RUBRICS) >= TASK_OUTPUT_SCHEMAS


def test_a_schema_with_no_rubric_raises_rather_than_defaulting() -> None:
    """A soft default would let a task type be added without anyone writing the
    paragraph — exactly the drift §8 exists to prevent."""
    from runtime.org.rubrics import rubric_for

    with pytest.raises(KeyError, match=r"§8\.1 requires"):
        rubric_for("WeeklyPlan@1")
