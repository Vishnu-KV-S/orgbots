"""The offline grading harness. PR-30.

§5: *"Grade a sample by hand: would this memory have helped, been neutral, or been
harmful in this specific call?"* This is the machinery for that, and §13 risk 5 is the
reason it is a first-class module with a CLI rather than a notebook: *"Golden-set
labelling will get skipped. Unglamorous, no visible output, and without it every number
in §11 is a guess with a decimal point."*

**It is deliberately not a model-graded harness.** Asking a model whether a retrieved
memory would have helped a model is asking the system under test to mark its own work,
and the failure it would miss is the interesting one — a confidently wrong fact reads as
relevant to a grader that has not checked whether it is true. §11 makes eval 3
(irrelevant rate) a tracked number precisely because that judgement has to come from
outside.

**What a grader sees.** One trace: the query the actor was about to answer, and every
candidate that was retrieved, in rank order, with which ones would have been injected.
That is the whole unit of judgement and it deliberately does not include the run's
eventual output — knowing the answer changes what looks relevant, and a grader who has
seen the report will find the memory that agrees with it helpful.

**Sampling.** Oldest-first over the ungraded queue, without replacement. §5 wants a
sample of a *window*; grading whatever is newest re-samples the end of the window and
never reaches the beginning, which is how a "sample of 100" turns out to be a sample of
last Tuesday. `reservoir` exists for the case where the window is much larger than the
sample and every part of it should be equally likely to be looked at.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from runtime.domain.enums import RetrievalGrade
from runtime.domain.ids import ContextTraceId, OrganizationId
from runtime.observability.logging import get_logger
from runtime.persistence.repositories.memory import TraceRow
from runtime.persistence.uow import UnitOfWorkFactory

log = get_logger("memory.grading")

DEFAULT_SAMPLE = 100
"""§5: *"a hand-graded sample of around 100 retrievals"*."""


@dataclass(frozen=True, slots=True)
class GradingBar:
    """§5's bar, held as a value so that it is set *before* the data is seen.

    *"What matters is that the bar exists before you see the data, not what the bar
    is."* Making it an object with a default, recorded in `docs/M3_SHADOW.md` and read
    by the CLI, is the mechanical form of that sentence: the number cannot be adjusted
    after the fact without the adjustment appearing in a diff.
    """

    min_helpful_or_neutral_pct: float = 60.0
    max_harmful_pct: float = 5.0
    min_sample: int = 100

    def verdict(self, summary: dict[str, Any]) -> tuple[bool, list[str]]:
        """`(passes, reasons)`. Reasons are given for a pass too.

        A pass with its numbers spelled out is what gets pasted into the decision
        record; a bare `True` is what gets pasted into a decision nobody can reconstruct.
        """
        reasons: list[str] = []
        graded = int(summary.get("graded") or 0)
        if graded < self.min_sample:
            reasons.append(f"sample is {graded}, bar wants at least {self.min_sample}")
        hon = summary.get("helpful_or_neutral_pct")
        harm = summary.get("harmful_pct")
        if hon is None or harm is None:
            reasons.append("nothing graded yet")
            return False, reasons
        if hon < self.min_helpful_or_neutral_pct:
            reasons.append(
                f"helpful-or-neutral {hon:.1f}% is below the "
                f"{self.min_helpful_or_neutral_pct:.0f}% bar"
            )
        else:
            reasons.append(
                f"helpful-or-neutral {hon:.1f}% (bar {self.min_helpful_or_neutral_pct:.0f}%)"
            )
        if harm > self.max_harmful_pct:
            reasons.append(f"harmful {harm:.1f}% is above the {self.max_harmful_pct:.0f}% ceiling")
        else:
            reasons.append(f"harmful {harm:.1f}% (ceiling {self.max_harmful_pct:.0f}%)")
        passes = (
            graded >= self.min_sample
            and hon >= self.min_helpful_or_neutral_pct
            and harm <= self.max_harmful_pct
        )
        return passes, reasons


class GradingHarness:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow = uow_factory

    async def queue(
        self, organization_id: OrganizationId, *, limit: int = 25, offset: int = 0
    ) -> list[TraceRow]:
        async with self._uow() as uow:
            return await uow.traces.ungraded(organization_id, limit=limit, offset=offset)

    async def sample(
        self, organization_id: OrganizationId, *, size: int = DEFAULT_SAMPLE, seed: str = "m3"
    ) -> list[TraceRow]:
        """A deterministic pseudo-random sample of the ungraded window.

        Deterministic — hashed on `(seed, trace id)` — so two people grading "the
        sample" grade the same traces, and so a re-run after a crash resumes the same
        sample rather than drawing a new one. That is the same reason M1's human review
        sampler is seeded: a sample nobody can reproduce is a sample nobody can check.
        """
        pool = await self.queue(organization_id, limit=max(size * 8, size), offset=0)
        if len(pool) <= size:
            return pool
        ranked = sorted(pool, key=lambda t: hashlib.sha256(f"{seed}:{t.id}".encode()).hexdigest())
        return ranked[:size]

    async def grade(
        self,
        trace_id: ContextTraceId,
        *,
        grade: RetrievalGrade,
        graded_by: str,
        note: str | None = None,
    ) -> bool:
        if grade is RetrievalGrade.UNGRADED:
            raise ValueError("UNGRADED is the absence of a grade, not a grade")
        async with self._uow.transaction() as uow:
            return await uow.traces.grade(trace_id, grade=grade, graded_by=graded_by, note=note)

    async def summary(self, organization_id: OrganizationId) -> dict[str, Any]:
        async with self._uow() as uow:
            return await uow.traces.grade_summary(organization_id)

    async def verdict(
        self, organization_id: OrganizationId, *, bar: GradingBar | None = None
    ) -> tuple[bool, list[str], dict[str, Any]]:
        """§5's flip decision, computed. The answer to *"may we turn injection on"*."""
        summary = await self.summary(organization_id)
        passes, reasons = (bar or GradingBar()).verdict(summary)
        return passes, reasons, summary

    async def token_effect(self, organization_id: OrganizationId) -> list[dict[str, Any]]:
        """Eval 8's raw material: what injection did or would have cost, per week."""
        async with self._uow() as uow:
            return await uow.traces.token_effect(organization_id)

    @staticmethod
    def render(trace: TraceRow) -> str:
        """One trace, formatted for a person to judge in about thirty seconds.

        The output deliberately stops at the candidates. No run output, no eventual
        task outcome — see the module docstring: a grader who has seen the answer finds
        the memory that agrees with it helpful.
        """
        lines = [
            f"trace {trace.id}   run {trace.run_id}   {trace.actor_name}/{trace.node}",
            f"mode: {'shadow' if trace.shadow_mode else 'live'}"
            f"   would-have-cost: {trace.would_have_injected_tokens} tokens",
            "",
            "QUERY",
            f"  {trace.query_text or '(none)'}",
            "",
            "RETRIEVED (rank order; ★ = would have been injected)",
        ]
        injected_ids = {
            e["memory_id"] for e in trace.retrieved if e.get("injected")
        } or _would_inject(trace)
        for entry in trace.retrieved:
            mark = "★" if entry["memory_id"] in injected_ids else " "
            lines.append(
                f"  {mark} [{entry['rank']}] score={entry['score']:.3f} "
                f"sim={entry.get('similarity', 0):.3f} "
                f"scope={entry.get('scope')} trust={entry.get('trust')}"
            )
        lines += [
            "",
            "Would each ★ memory have helped this specific call, been neutral, or been harmful?",
            "  helpful | neutral | harmful",
        ]
        return "\n".join(lines)


def _would_inject(trace: TraceRow) -> set[str]:
    """In shadow mode nothing is flagged injected, so reconstruct the top-K.

    From `injected_count` where it is non-zero, else from the rank order truncated to
    however many the trace says would have fitted. This is display only — the authority
    on what was injected is the `injected` flag on each entry, and in shadow mode the
    honest answer to "what was injected" is "nothing", which is why this function is
    named for the counterfactual rather than for the fact.
    """
    count = trace.injected_count or sum(
        1 for e in trace.retrieved if e.get("rank", 99) < len(trace.retrieved)
    )
    return {e["memory_id"] for e in trace.retrieved[:count]}


def grade_from_string(value: str) -> RetrievalGrade:
    """Accept `h`, `n`, `x` as well as the full words.

    A harness that requires typing "harmful" a hundred times is a harness that gets a
    hundred "neutral"s, because neutral is the shortest word that is never wrong. The
    single letters make the three answers equally cheap, which is the only way the
    distribution means anything.
    """
    normalised = value.strip().lower()
    shorthand = {
        "h": RetrievalGrade.HELPFUL,
        "n": RetrievalGrade.NEUTRAL,
        "x": RetrievalGrade.HARMFUL,
    }
    if normalised in shorthand:
        return shorthand[normalised]
    return RetrievalGrade(normalised)


__all__ = [
    "DEFAULT_SAMPLE",
    "GradingBar",
    "GradingHarness",
    "Sequence",
    "grade_from_string",
]
