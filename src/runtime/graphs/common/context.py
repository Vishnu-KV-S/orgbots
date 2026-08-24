"""Context assembly — a fixed template, not an engine.

§2, verbatim: *"Context assembly is a fixed template, not an engine. System prompt +
actor spec block + task input (validated object) + session summary + last 6 messages
+ referenced artifact summaries. No retrieval, no reranking, no adaptive budget. If
token counts get uncomfortable, tighten the template — don't build M3 early."*

That is the whole specification and `assemble()` below is a literal transcription of
it. The order of the six parts is fixed and it is not arbitrary:

    system prompt        ← identical across every call this actor makes
    actor spec block     ← changes only when the actor is republished
    ─────────────────────  cache breakpoint goes here
    session summary      ← changes every dozen messages
    recent messages      ← changes every message
    artifact summaries   ← changes per task
    task input           ← changes per call

Stable first, volatile last. Prompt caching is a prefix match, so anything that
varies per call sitting above something that does not costs a full-price re-read of
everything below it. Getting this order backwards would not break anything — it
would just quietly triple the input cost, and the number it would inflate is the
one §9 asks us to defend.

**What this module deliberately cannot do**, because doing it would be M3 arriving
early wearing a disguise: it does not retrieve, rank, score, or decide what to
include. Every part is either present or absent for a structural reason. If the
result is too long, the fix is to lower a constant here and say so in the retro.

**M2 adds the trust boundary, and it changes the shape in one visible way.** Material
that came from outside the runtime is no longer interpolated as plain text: it is
rendered inside a fence by `runtime.domain.trust`, and the system prompt carries the
rule that says instructions inside a fence are content to be reported, never followed.

Four ingresses are fenced, and §6 requires the injection corpus to be run through
every one of them:

    web fetch results   → tool output, reaching a prompt through task input or state
    inbox messages      → another actor's words, or a human's
    artifact contents   → whatever produced the artifact
    task input fields   → the free-text fields of a task somebody else wrote

The last one is the one people forget. A task input is *structured*, which makes it
feel trusted — but its string fields were written by an upstream actor whose own
input may have been a fetched page, and the trust does not survive the schema.

The fenced sections sit **after** the stable prefix and before the instruction, which
keeps the cache breakpoint where it was. Fencing costs tokens; putting it above the
breakpoint would have cost the whole prefix.

**M3 adds one part, and adds it as a seventh line of a fixed template rather than as an
engine.** The paragraph above says this module "does not retrieve, rank, score, or
decide what to include" — it still does not. `ContextPlanner` does all of that, in
`runtime.memory`, and hands `assemble()` a finished string. This module's contribution
is the one decision that is genuinely about the *template*: where the block goes.

    system prompt        ← identical across every call this actor makes
    actor spec block     ← changes only when the actor is republished
    ─────────────────────  cache breakpoint goes here
    session summary      ← changes every dozen messages
    recalled memory      ← changes per call        ← M3
    recent messages      ← changes every message
    artifact summaries   ← changes per task
    task input           ← changes per call

Below the breakpoint, for the reason everything else volatile is. Above the messages
and the task, because a recalled memory is background the actor brings *to* the
conversation rather than part of it — and because putting it last, next to the
instruction, is how a stale fact ends up reading as the most recent thing anybody said.

**In shadow mode the parameter arrives empty and this file does nothing at all**, which
is the property §5's phase one rests on: the assembled prompt is byte-identical to M2's.
`test_m3_shadow.py::test_shadow_prompt_is_byte_identical` asserts exactly that against
the same inputs with memory on and off.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from runtime.domain.hashing import canonical_json
from runtime.domain.specs import RunSpec
from runtime.domain.trust import TRUST_SYSTEM_RULE, UntrustedBlock, render_blocks
from runtime.graphs.common.state import ArtifactRefView, summarise
from runtime.persistence.repositories.inbox import InboxRow

RECENT_MESSAGE_WINDOW = 6
"""§2: "last 6 messages". A constant, not a budget — nothing here adapts."""

MAX_MESSAGE_CHARS = 400
MAX_INPUT_CHARS = 6_000
MAX_UNTRUSTED_CHARS = 8_000
"""Tighten these rather than building a budgeter. §2 is explicit about that.

`MAX_UNTRUSTED_CHARS` is per block and larger than the others because truncating
untrusted material is a *security*-relevant edit, not just a cost one: a payload split
across the boundary can leave its second half looking like ordinary prose. The renderer
says how much it cut, so a truncated block is visibly truncated rather than quietly
shortened.
"""

UNTRUSTED_TASK_INPUT_FIELDS = frozenset(
    {"brief", "notes", "summary", "context", "body", "text", "source_text", "quote"}
)
"""Task-input fields treated as untrusted rather than as structure.

A curated set rather than "every string", because fencing the *whole* input would put
the task's own objective behind a "do not follow this" instruction — which is the one
piece of text the actor is supposed to act on. These are the fields whose content
demonstrably originates upstream.
"""


@dataclass(slots=True)
class AssembledContext:
    system: str
    prompt: str
    parts: dict[str, int] = field(default_factory=dict)
    """Character count per part. Not decoration: when the cost per accepted outcome
    comes out high, "which part of the prompt is big" is the first question, and
    reconstructing it from logs afterwards is not possible."""

    @property
    def total_chars(self) -> int:
        return len(self.system) + len(self.prompt)


def actor_spec_block(spec: RunSpec) -> str:
    """What the actor is and what it may do, from the *frozen* spec.

    Read from the RunSpec rather than from live config for the same reason the
    worker does: an actor republished mid-flight must not change what an in-flight
    run believes about itself, including in its own prompt.
    """
    compiled = spec.spec
    tools = ", ".join(sorted(compiled.allowed_tools)) or "none"
    return (
        f"You are `{compiled.actor_name}` (v{compiled.actor_version}), a "
        f"{compiled.kind.value} in a four-actor marketing department.\n"
        f"Tools available to you: {tools}.\n"
        f"Hard limits for this run: at most {compiled.ceilings.max_llm_calls} model "
        f"calls and {compiled.ceilings.max_tool_calls} tool calls, "
        f"{compiled.ceilings.max_wall_clock_s:.0f}s wall clock, "
        f"{compiled.ceilings.max_cost_cents} cents.\n"
        "These are enforced by the runtime, not by you. If you run out, the run "
        "fails — so spend them on the work, not on deliberation."
    )


def assemble(
    *,
    system_prompt: str,
    spec: RunSpec,
    task_input: dict[str, Any] | None = None,
    session_summary: str | None = None,
    recent_messages: list[InboxRow] | None = None,
    artifacts: list[ArtifactRefView] | None = None,
    instruction: str = "",
    untrusted: list[UntrustedBlock] | None = None,
    fence_trust: bool = True,
    memory: str = "",
) -> AssembledContext:
    """Build the two strings a model call needs. Six parts, fixed order.

    `untrusted` carries material from outside the runtime — fetched pages, artifact
    bodies — which is rendered fenced and labelled as data. Inbox messages and the
    free-text task-input fields named in `UNTRUSTED_TASK_INPUT_FIELDS` are fenced
    automatically, because a caller that had to remember to pass them is a caller that
    will one day forget, and the failure would be silent.

    `fence_trust=False` exists for one caller: the evaluation node, which builds its
    context by hand and has no untrusted material at all. It is not an escape hatch —
    passing untrusted blocks with it False raises, because that combination is always
    a mistake.
    """
    blocks = list(untrusted or [])
    if blocks and not fence_trust:
        raise ValueError(
            "fence_trust=False with untrusted blocks present: that combination would "
            "interpolate outside material as instruction. If the content is genuinely "
            "trusted, do not pass it as an UntrustedBlock."
        )

    system = f"{system_prompt.strip()}\n\n{actor_spec_block(spec)}"
    if fence_trust:
        system = f"{system}\n\n{TRUST_SYSTEM_RULE.strip()}"

    sections: list[tuple[str, str]] = []

    if session_summary:
        sections.append(
            (
                "session_summary",
                "## What has happened in this session so far\n" + session_summary.strip(),
            )
        )

    if memory:
        # Already rendered by `ContextPlanner`, already inside its token cap, and empty
        # in shadow mode. Not fenced: a memory is the organization's own extracted note,
        # and the material it came from was fenced when it was *read* — fencing it again
        # here would tell the actor not to act on its own prior conclusions, which is the
        # one thing recalled memory is for. Untrusted provenance is handled where it can
        # actually be handled, at the write path, by quarantine (§6).
        sections.append(("memory", memory))

    if recent_messages:
        window = recent_messages[-RECENT_MESSAGE_WINDOW:]
        if fence_trust:
            # A message is another actor's words, and that actor may have been reading
            # a web page. The *envelope* — who sent it, to whom, what kind — is ours
            # and stays outside the fence; the body is theirs and goes inside.
            for m in window:
                body = m.subject or summarise(m.body, MAX_MESSAGE_CHARS)
                blocks.append(
                    UntrustedBlock(
                        content=f"[{m.kind}] {m.sender_name or 'system'} → "
                        f"{m.recipient_name}: {body}",
                        source=f"inbox:{m.kind}:{m.sender_name or 'system'}",
                        ingress="inbox",
                    )
                )
        else:
            rendered = "\n".join(
                f"- [{m.kind}] {m.sender_name or 'system'} → {m.recipient_name}: "
                f"{m.subject or summarise(m.body, MAX_MESSAGE_CHARS)}"
                for m in window
            )
            sections.append(("recent_messages", "## Recent messages\n" + rendered))

    if artifacts:
        rendered = "\n".join(
            f"- `{a.kind}` {a.artifact_id} ({a.size_bytes} bytes): {a.summary}" for a in artifacts
        )
        sections.append(
            (
                "artifacts",
                "## Referenced artifacts\n"
                + rendered
                + "\n\nThese are summaries. The full text of anything you need has "
                "already been placed in your task input.",
            )
        )

    if task_input and fence_trust:
        # Pull the free-text fields out of the structured input and fence them, leaving
        # the structure — ids, schema refs, the objective — where the actor can act on
        # it. A task input feels trusted because it is typed; its string fields were
        # written upstream and the schema does not launder them.
        carved = {k: v for k, v in task_input.items() if k not in UNTRUSTED_TASK_INPUT_FIELDS}
        for key in sorted(set(task_input) & UNTRUSTED_TASK_INPUT_FIELDS):
            value = task_input[key]
            if value in (None, ""):
                continue
            blocks.append(
                UntrustedBlock(
                    content=value if isinstance(value, str) else canonical_json(value),
                    source=f"task_input:{key}",
                    ingress="task_input",
                )
            )
        task_input = carved

    if blocks:
        sections.append(("untrusted", render_blocks(blocks, max_chars=MAX_UNTRUSTED_CHARS)))

    if task_input:
        body = canonical_json(task_input)
        if len(body) > MAX_INPUT_CHARS:
            # Truncate loudly. A silently shortened input produces work that is
            # wrong for reasons nobody can see, and §10's "right-but-not-what-was-
            # asked" diagnosis would be looking at the wrong half of the problem.
            body = (
                body[:MAX_INPUT_CHARS] + f"\n\n[TRUNCATED at {MAX_INPUT_CHARS} characters of "
                f"{len(body)}. Say so in your output rather than guessing at what "
                "was cut.]"
            )
        sections.append(("task_input", "## Your task\n```json\n" + body + "\n```"))

    if instruction:
        sections.append(("instruction", instruction.strip()))

    prompt = "\n\n".join(text for _, text in sections)
    parts = {name: len(text) for name, text in sections}
    parts["system"] = len(system)
    return AssembledContext(system=system, prompt=prompt, parts=parts)
