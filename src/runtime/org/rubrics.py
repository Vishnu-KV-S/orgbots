"""What "accepted" means, per task type. §8.1.

*"Not 'the head agent said yes.' Write, per task type, what a human would have to
see to sign off. One paragraph each. This is the rubric your sampling uses."*

These paragraphs are **the** definition of acceptance in this system. They are used
in exactly two places and it is the same text in both:

1. `marketing-head`'s EVALUATION prompt, verbatim. §10's remedy for a rubber-stamp
   evaluator is that its prompt carries the rubric verbatim and the call is a
   separate one with only the artifact and the rubric in context.
2. The human sampling harness (`runtime.cli.sample`), which shows the reviewer the
   same criteria the manager was shown.

That shared text is what makes the confusion matrix mean something. If the manager
and the human were working from different definitions, a disagreement would tell you
they disagreed about the rubric, not about the work — and false-accept rate, the
number §8.2 says to watch, would be measuring the wrong thing entirely.

**Changing anything in this file resets the §8.4 clean-run clock.** It is part of the
frozen measurement protocol and it is versioned for that reason: `RUBRIC_VERSION`
goes into every evaluation's `rubric` payload, so a verdict recorded under one
version is never silently compared against one recorded under another.
"""

from __future__ import annotations

from dataclasses import dataclass

from runtime.domain.outputs import (
    COMPETITOR_REPORT_V1,
    CONTENT_DRAFT_V1,
    METRICS_REPORT_V1,
    WEEKLY_SUMMARY_V1,
)

RUBRIC_VERSION = 1
"""Bump only with a deliberate protocol change, and reset the clean-run clock."""


@dataclass(frozen=True, slots=True)
class Rubric:
    schema_ref: str
    what_good_looks_like: str
    criteria: tuple[str, ...]

    def as_prompt(self) -> str:
        numbered = "\n".join(f"{i + 1}. {c}" for i, c in enumerate(self.criteria))
        return (
            f"## Rubric for {self.schema_ref} (v{RUBRIC_VERSION})\n\n"
            f"{self.what_good_looks_like}\n\n"
            f"### Criteria — judge each one explicitly\n{numbered}"
        )


COMPETITOR_REPORT_RUBRIC = Rubric(
    schema_ref=COMPETITOR_REPORT_V1,
    what_good_looks_like=(
        "A marketing lead reads this instead of spending an afternoon on the "
        "competitors' websites, and comes away able to say something specific in "
        "next week's meeting. That means the competitors named are real and current, "
        "every characterisation of one is traceable to a quoted source that actually "
        "says it, the themes are patterns across competitors rather than a restatement "
        "of the individual entries, and at least one recommendation is concrete enough "
        "to act on this week. A report that reads well and cites sources which do not "
        "support the claims made from them is worse than no report, because it will be "
        "believed. Gaps that are stated honestly do not count against it; gaps that "
        "have been papered over with plausible-sounding detail are the single worst "
        "failure this task type has."
    ),
    criteria=(
        "Every competitor named is a real company in this category, not a "
        "hallucination or a generic placeholder.",
        "Each cited source, read on its own, actually supports the claim citing it.",
        "The themes identify a pattern across competitors rather than restating "
        "individual entries.",
        "At least one recommendation is specific enough that a person could start on "
        "it this week without asking a follow-up question.",
        "What the research could not establish is stated in `gaps` rather than "
        "filled in with plausible detail.",
    ),
)

CONTENT_DRAFT_RUBRIC = Rubric(
    schema_ref=CONTENT_DRAFT_V1,
    what_good_looks_like=(
        "This could be published after a copy-edit, not a rewrite. It has a real "
        "argument rather than a survey of a topic; a named reader with a job to do "
        "rather than 'businesses'; and every factual claim traceable to the research "
        "it was given. The prose sounds like a person wrote it — no throat-clearing "
        "introduction, no 'in today's fast-paced landscape', no conclusion that "
        "restates the introduction. Length matches what was asked. A draft that is "
        "competent, on-topic and says nothing anyone would disagree with is a reject: "
        "it is the failure mode this task type produces by default and the one most "
        "likely to be waved through."
    ),
    criteria=(
        "There is a thesis someone could disagree with, not a neutral survey.",
        "The audience is a specific reader with a specific job, not a category.",
        "Every factual claim is supported by the research provided, and cited.",
        "The prose would survive a copy-edit rather than needing a rewrite: no "
        "filler openings, no restated conclusions, no throat-clearing.",
        "Length and channel match what the task asked for.",
    ),
)

METRICS_REPORT_RUBRIC = Rubric(
    schema_ref=METRICS_REPORT_V1,
    what_good_looks_like=(
        "The numbers are right and the caveats are present. Because this actor is "
        "deterministic, 'right' is checkable rather than a matter of judgement: the "
        "rates agree with the counts, the counts agree with what is in the database, "
        "and AUTO_ACCEPTED tasks are absent from every acceptance numerator. The "
        "caveats matter as much as the numbers — a week with no human sample must say "
        "so, and a week that trips a §9 stop threshold must say that too. A metrics "
        "report that renders a rate over a zero denominator as 0% rather than as "
        "absent is wrong in the most dangerous direction available, because it reads "
        "as a perfect week."
    ),
    criteria=(
        "Every rate is arithmetically consistent with the counts it is derived from.",
        "AUTO_ACCEPTED tasks appear only in their own share, never in an acceptance "
        "numerator or in the cost-per-accepted denominator.",
        "A rate with no denominator is absent, not zero.",
        "A week with no human sample says so in `notes`.",
        "Any §9 stop threshold that was tripped is named in `notes`.",
    ),
)

WEEKLY_SUMMARY_RUBRIC = Rubric(
    schema_ref=WEEKLY_SUMMARY_V1,
    what_good_looks_like=(
        "A person who was away all week reads this and knows what happened, what it "
        "cost, and what needs them. It is specific: named pieces of work with named "
        "outcomes, not 'progress was made'. It reports what did not ship as plainly "
        "as what did — a summary that only lists successes is not a summary, it is "
        "advocacy, and it is the exact failure that lets a department drift for a "
        "month. Decisions needing a human are stated as decisions, with enough "
        "context to make them."
    ),
    criteria=(
        "Named work with named outcomes, not general statements of progress.",
        "What did not ship is reported as plainly as what did.",
        "The metrics note engages with the actual numbers, including bad ones.",
        "Anything needing a human decision is stated as a decision with enough context to make it.",
        "Next week's focus follows from this week rather than restating the goal.",
    ),
)

RUBRICS: dict[str, Rubric] = {
    r.schema_ref: r
    for r in (
        COMPETITOR_REPORT_RUBRIC,
        CONTENT_DRAFT_RUBRIC,
        METRICS_REPORT_RUBRIC,
        WEEKLY_SUMMARY_RUBRIC,
    )
}


def rubric_for(schema_ref: str) -> Rubric:
    """The rubric for a task's output schema.

    Raises on an unknown schema rather than returning a generic fallback. A task
    type with no written definition of acceptance cannot be evaluated honestly, and
    a soft default would let one get added without anyone writing the paragraph —
    which is precisely the drift §8 exists to prevent.
    """
    try:
        return RUBRICS[schema_ref]
    except KeyError as exc:
        raise KeyError(
            f"no rubric for {schema_ref!r}. §8.1 requires a written definition of "
            f"acceptance per task type before anything of that type is evaluated; "
            f"known: {sorted(RUBRICS)}"
        ) from exc
