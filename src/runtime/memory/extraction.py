"""Turning a finished run into candidate facts.

One model call, `work_class=MEMORY`, made through `ModelGateway.complete_detached`
after the run has already returned — §6: *"never the hot path"*.

**This is the call §13 risk 6 is about.** *"The extraction model determines memory
quality permanently. A noisy fact written today keeps being retrieved."* Every other
model call in this codebase produces something a human or an evaluator will look at
once and then close; this one produces something that will be re-read by future runs
indefinitely, and nothing downstream can improve it. `department.MEMORY_PROFILE` is
the pro model rather than flash for that reason.

**The prompt is written against the failure mode, not the goal.** An extractor told
"extract the important facts" returns a summary of the run, which is exactly the wrong
thing: a summary is about *this* run and memory is for the *next* one. The instruction
below is built around four refusals, and each one is a category of noise that would
otherwise be retrieved forever.

**Everything the extractor reads is untrusted.** A run's output is model text, its tool
observations are fetched pages, and its artifacts are whatever produced them — so the
whole payload is fenced with `runtime.domain.trust` before it reaches the extractor,
and the extractor gets the standing trust rule in its system prompt like any other
caller. That fencing does **not** make the extracted facts trusted: `run_trust`
quarantines them regardless (§6), and the fence is here to stop the *extraction call
itself* being steered, which is a different attack from the one quarantine handles.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from runtime.domain.enums import MemoryType, WorkClass
from runtime.domain.hashing import canonical_json
from runtime.domain.ids import BudgetPoolId, OrganizationId, RunId
from runtime.domain.specs import ModelProfile
from runtime.domain.trust import TRUST_SYSTEM_RULE, UntrustedBlock, render_blocks
from runtime.gateway.models import ModelGateway, ModelRequest
from runtime.memory.store import ExtractedFact
from runtime.observability.logging import get_logger

log = get_logger("memory.extraction")

MAX_FACTS = 8
"""`[CHOSEN]`. A cap on how much one run may add to the store.

Not a quality judgement — a rate limit on accumulation. §13 risk 2 is that precision
decays as the store grows, and the growth rate is the product of runs and facts per
run. The second factor is the one we control, and an extractor with no cap will happily
find twenty facts in a run that contained two."""

MAX_SOURCE_CHARS = 24_000

SYSTEM = """You extract durable facts from a completed piece of work, so that whoever \
does the next piece does not have to rediscover them.

You are writing for a stranger who will read one line of what you write, six weeks from \
now, out of context, and act on it. That constraint decides everything below.

**Refuse these four, always. They are the entire failure mode of this job.**

1. **Anything about this run.** "The research task was completed", "three sources were \
fetched", "the draft was submitted for review". A record of what happened is what the \
run log is for. A memory is something that will still be true and still be useful when \
nobody remembers this run existed.

2. **Anything already obvious to anyone doing this work.** "Competitors have pricing \
pages." "Marketing content should be relevant to the audience." A fact that every future \
actor already knows costs tokens on every retrieval and returns nothing.

3. **Anything you are guessing.** If the material says a competitor "appears to have" \
raised prices, either write that uncertainty into the statement or omit the fact. A \
confident memory built on a hedge is worse than no memory, because the hedge is what \
would have saved the reader.

4. **Anything that is a moment rather than a state.** "The site was down on Tuesday" \
is a moment. "The status page is at status.example.com" is a state. Prefer states; \
where a moment genuinely matters, date it inside the statement.

**Shape.** Each fact is a `subject` and a `statement`.

The `subject` is what the fact is *about* — an entity, a system, a decision, a \
convention. It is how this fact will later be recognised as the same topic as a fact \
written next month, so it must be the stable part: "Competitor Acme pricing", not \
"the pricing we found". Reuse the same subject wording when you mean the same thing.

The `statement` is what is true about it, in one sentence, standing alone. It must make \
sense with nothing else on screen.

**`importance`** is your estimate of how often this will actually be worth retrieving, \
0 to 1. Be harsh. Most facts are 0.3. A fact that changes how the next person works is \
0.8. If you find yourself giving everything 0.7, you are describing the run again.

**`confidence`** is how sure you are the fact is true, 0 to 1, based only on the \
material you were given.

Return fewer facts than you think you should. Zero is a valid and common answer."""

INSTRUCTION = """Return JSON and nothing else:

{"facts": [{"subject": "...", "statement": "...", "type": "fact|episode|procedure",
            "confidence": 0.0-1.0, "importance": 0.0-1.0, "entities": ["..."]}]}

`type` is `fact` for a state of the world, `episode` for something that happened and \
matters as an event, `procedure` for a way of doing something that worked. When in \
doubt it is `fact`.

`entities` names the organizations, products, people or systems the fact is about, \
spelled as the source spelled them.

At most MAX_FACTS facts. `{"facts": []}` if nothing here is worth a stranger's \
attention in six weeks."""
"""The placeholder is `MAX_FACTS` and it is substituted with `str.replace`, not with
`str.format`. This prompt is mostly a JSON literal, and `.format()` reads every `{` in
it as a field reference — which fails at run time with `KeyError: '"facts"'`, in the
worker, on a prompt that looks fine. It cost a red test to find; leaving it as a
`replace` is cheaper than escaping every brace in a schema that will be edited again."""


@dataclass(frozen=True, slots=True)
class ExtractionSource:
    """What the worker gathered from a finished run (§6's "gather" step).

    Every field is a *reference to text produced outside the runtime*, which is why
    `render` fences all of them rather than trusting the structure. A task title was
    written by the head actor, whose own input may have been a fetched page — §M2's
    "the trust does not survive the schema" applies here exactly as it does in
    `graphs.common.context`.
    """

    run_id: RunId
    actor_name: str
    task_title: str | None = None
    task_objective: str | None = None
    output: dict[str, Any] | None = None
    artifact_summaries: Sequence[str] = ()
    tool_observations: Sequence[str] = ()
    outcome: str | None = None

    def render(self) -> str:
        blocks = (
            [
                UntrustedBlock(
                    content=canonical_json(self.output)[:MAX_SOURCE_CHARS],
                    source=f"run_output:{self.run_id}",
                    ingress="artifact",
                )
            ]
            if self.output
            else []
        )
        for i, summary in enumerate(self.artifact_summaries):
            blocks.append(
                UntrustedBlock(content=summary, source=f"artifact_summary:{i}", ingress="artifact")
            )
        for i, observation in enumerate(self.tool_observations):
            blocks.append(
                UntrustedBlock(
                    content=observation, source=f"tool_observation:{i}", ingress="web.fetch@1"
                )
            )
        if self.task_objective:
            blocks.append(
                UntrustedBlock(
                    content=self.task_objective, source="task_input:objective", ingress="task_input"
                )
            )

        header = (
            f"## The work that just finished\n"
            f"Actor: `{self.actor_name}`\n"
            f"Task: {self.task_title or '(none)'}\n"
            f"Outcome: {self.outcome or 'unknown'}\n"
        )
        return f"{header}\n{render_blocks(blocks, max_chars=8_000)}"


async def extract_facts(
    models: ModelGateway,
    *,
    organization_id: OrganizationId,
    source: ExtractionSource,
    profile: ModelProfile,
    pool_id: BudgetPoolId | None = None,
    max_facts: int = MAX_FACTS,
) -> tuple[list[ExtractedFact], int]:
    """Extract, parse defensively, return `(facts, cost_cents)`.

    **A malformed response yields zero facts and does not raise.** Every other
    structured call in this codebase retries and then fails the task, because there the
    output *is* the deliverable. Here the deliverable already shipped — the run
    succeeded, the human has the report — and failing the memory write would turn a
    successful run into a failed one for the sake of a fact nobody asked for. It is
    logged, the outbox event is consumed, and the run is not re-processed: a retry loop
    against an extractor that reliably returns prose is a loop that spends WORK-class
    money forever.
    """
    request = ModelRequest(
        prompt=f"{source.render()}\n\n{INSTRUCTION.replace('MAX_FACTS', str(max_facts))}",
        system=f"{SYSTEM}\n\n{TRUST_SYSTEM_RULE.strip()}",
        metadata={"json_schema": _FACTS_SCHEMA},
    )
    response = await models.complete_detached(
        organization_id,
        request,
        profile=profile,
        work_class=WorkClass.MEMORY,
        call_site="memory.extract",
        run_id=source.run_id,
        pool_id=pool_id,
        actor_name=source.actor_name,
    )

    facts = _parse(response.text, max_facts=max_facts)
    log.info(
        "memory.extracted",
        run_id=str(source.run_id),
        actor=source.actor_name,
        facts=len(facts),
        cost_cents=response.cost_cents,
    )
    return facts, response.cost_cents


def _parse(raw: str, *, max_facts: int) -> list[ExtractedFact]:
    """Parse, clamp and drop. Never raise.

    Each fact is validated independently: one malformed entry costs that entry, not the
    batch. An extractor that returns seven good facts and one with a missing statement
    has done seven-eighths of a useful job, and discarding all of it would be a
    correctness posture applied to a best-effort enrichment.
    """
    payload = _as_object(raw)
    if payload is None:
        log.warning("memory.extract_unparseable", head=raw[:200])
        return []

    facts: list[ExtractedFact] = []
    for entry in payload.get("facts", [])[:max_facts]:
        if not isinstance(entry, dict):
            continue
        subject = str(entry.get("subject", "")).strip()
        statement = str(entry.get("statement", "")).strip()
        if not subject or not statement:
            continue
        try:
            memory_type = MemoryType(str(entry.get("type", "fact")))
        except ValueError:
            memory_type = MemoryType.FACT
        if memory_type is MemoryType.ENTITY_REF:
            # Not the extractor's to mint. `entity_ref` rows are written by the entity
            # linker from `entities`, and an extractor that could emit them would be
            # writing rows that point at nothing.
            memory_type = MemoryType.FACT
        entities = tuple(str(e).strip() for e in entry.get("entities", []) if str(e).strip())[:8]
        facts.append(
            ExtractedFact(
                subject=subject[:200],
                statement=statement[:1_000],
                memory_type=memory_type,
                confidence=_unit(entry.get("confidence"), default=0.5),
                importance=_unit(entry.get("importance"), default=0.4),
                entities=entities,
            )
        )
    return facts


def _unit(value: Any, *, default: float) -> float:
    """Clamp to [0, 1]. A model that returns 95 meaning 95% must not get a 95x weight
    in the reranker — which is what an unclamped `importance` would be."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return min(1.0, max(0.0, number))


def _as_object(raw: str) -> dict[str, Any] | None:
    stripped = raw.strip()
    if not stripped:
        return None
    if stripped.startswith("```"):
        parts = stripped.split("```", 2)
        if len(parts) >= 2:
            stripped = parts[1]
            if stripped.startswith("json"):
                stripped = stripped[4:]
            stripped = stripped.strip()
    try:
        loaded = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


_FACTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["facts"],
    "additionalProperties": False,
    "properties": {
        "facts": {
            "type": "array",
            "maxItems": MAX_FACTS,
            "items": {
                "type": "object",
                "required": ["subject", "statement"],
                "additionalProperties": False,
                "properties": {
                    "subject": {"type": "string", "minLength": 1, "maxLength": 200},
                    "statement": {"type": "string", "minLength": 1, "maxLength": 1000},
                    "type": {"enum": ["fact", "episode", "procedure"]},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "importance": {"type": "number", "minimum": 0, "maximum": 1},
                    "entities": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
                },
            },
        }
    },
}
"""Sent to the provider as `output_config.format` where it supports one.

Not registered in `domain.schemas.SCHEMAS`, deliberately. That registry is the pinned
*task output* schemas — the ones M1 freezes so a task written Monday can be judged
Friday against the schema it was written against. An extraction schema has no task, no
evaluation and no rework cycle, and putting it in the registry would put it in
`test_m1_schemas.py`'s inventory of things the department produces."""
