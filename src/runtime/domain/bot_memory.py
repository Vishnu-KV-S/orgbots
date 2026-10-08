"""A bot's job brief and its long-term memory — the values. Pure, like `domain.bots`.

**The brief is the primary instruction**: what an employee is handed on day one. A
mission, the duties that follow from it, the lines it must not cross, how it should
work, and when to come back and ask. It is written by the person, or — for a helper —
by the bot that created it, and either the bot itself or its parent may revise it
later. Every revision is kept, with who made it and why, so a bot that rewrote its own
job is a diff a person can read and undo. A person can lock a brief; a locked brief is
one no bot may change.

**Memory is modelled on how people remember**, not on a notes field:

- *Kinds.* Facts, the person's preferences, people and contacts, skills (how a thing
  was done last time), and episodes — one diary line per finished turn, written
  automatically, which is what lets a bot answer "what did we do last week?" long
  after the conversation has scrolled out of its context.
- *Recall is selective.* Everything is kept (up to `MAX_MEMORIES`), but a prompt
  carries only what matters now: pinned memories and strong preferences always, then
  the best of the rest by **relevance** to the conversation, **importance**, and
  **recency** of last use — the same three signals a person's recall leans on. What
  is not shown can still be searched with the `recall` action.
- *Rehearsal.* A memory that is recalled is refreshed, so what keeps being useful
  stays easy to reach and what never is fades.
- *Consolidation.* Saving what is already known strengthens the existing memory
  instead of storing a second copy.
- *Forgetting.* Past the cap, the weakest unpinned memories go first — old, unused,
  unimportant — never a pinned one.
"""

from __future__ import annotations

import math
import re
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# --- the brief ---------------------------------------------------------------------------

_LINE = 400
_LINES = 12


def _clean_lines(value: Iterable[str]) -> list[str]:
    out: list[str] = []
    for item in value:
        line = " ".join(str(item).split()).removeprefix("- ").strip()
        if line and line not in out:
            out.append(line[:_LINE])
    return out[:_LINES]


class BotBrief(BaseModel):
    """A bot's primary instruction. Every field optional; an empty brief is allowed."""

    model_config = ConfigDict(extra="forbid")

    mission: str = Field(
        default="",
        max_length=1_500,
        description="The job in one or two sentences: what this bot is for.",
    )
    duties: list[str] = Field(default_factory=list, description="Responsibilities, one per line.")
    boundaries: list[str] = Field(
        default_factory=list, description="Lines it must never cross, one per line."
    )
    style: str = Field(default="", max_length=800, description="How it works and communicates.")
    escalation: str = Field(
        default="",
        max_length=800,
        description="When to stop and ask its person (or the bot that created it).",
    )
    notes: str = Field(
        default="", max_length=4_000, description="Standing instructions and anything else."
    )

    @field_validator("duties", "boundaries", mode="before")
    @classmethod
    def _lines(cls, value: object) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return _clean_lines(value.splitlines())
        if isinstance(value, list | tuple):
            return _clean_lines(str(v) for v in value)
        raise ValueError("expected a list of lines")

    def is_empty(self) -> bool:
        return not any(
            (self.mission, self.duties, self.boundaries, self.style, self.escalation, self.notes)
        )

    def render(self) -> str:
        parts: list[str] = []
        if self.mission.strip():
            parts.append(f"Mission: {self.mission.strip()}")
        if self.duties:
            parts.append("Responsibilities:\n" + "\n".join(f"- {d}" for d in self.duties))
        if self.boundaries:
            parts.append(
                "Boundaries — never cross these:\n" + "\n".join(f"- {b}" for b in self.boundaries)
            )
        if self.style.strip():
            parts.append(f"Working style: {self.style.strip()}")
        if self.escalation.strip():
            parts.append(f"When to stop and ask: {self.escalation.strip()}")
        if self.notes.strip():
            parts.append(f"Standing notes:\n{self.notes.strip()}")
        return "\n".join(parts)


class BriefPatch(BaseModel):
    """A change to a brief, as a bot writes it: only the fields it names change.

    Lists are replaced whole, not merged — a bot that wants to add a duty restates the
    list — so a patch always says exactly what the field will be afterwards.
    """

    model_config = ConfigDict(extra="forbid")

    mission: str | None = Field(default=None, max_length=1_500)
    duties: list[str] | None = None
    boundaries: list[str] | None = None
    style: str | None = Field(default=None, max_length=800)
    escalation: str | None = Field(default=None, max_length=800)
    notes: str | None = Field(default=None, max_length=4_000)

    def is_empty(self) -> bool:
        return all(getattr(self, name) is None for name in type(self).model_fields)

    def apply(self, brief: BotBrief) -> BotBrief:
        changes = {k: v for k, v in self.model_dump().items() if v is not None}
        return BotBrief.model_validate({**brief.model_dump(), **changes})


def brief_changes(before: BotBrief, after: BotBrief) -> list[str]:
    """The names of the fields that differ — what a revision line says changed."""
    return [name for name in BotBrief.model_fields if getattr(before, name) != getattr(after, name)]


def revision_id(run_id: object, step: int, target: object) -> uuid.UUID:
    """A brief revision written by a run's step. Derived, so a replay is a no-op."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"botbrief:{run_id}:{step}:{target}")


EditorKind = Literal["person", "self", "parent"]


# --- memory ------------------------------------------------------------------------------

MemoryKind = Literal["fact", "preference", "person", "skill", "episode"]
MEMORY_KINDS: tuple[MemoryKind, ...] = ("preference", "person", "fact", "skill", "episode")

KIND_TITLES: dict[str, str] = {
    "preference": "Your person's preferences",
    "person": "People and contacts",
    "fact": "Facts",
    "skill": "Skills — how you did things before",
    "episode": "Your diary — earlier work",
}

MAX_MEMORIES = 600
"""Kept per bot. Past this, the weakest unpinned memories are forgotten."""

MEMORY_CHARS = 600
"""One memory. Longer is a document, not a memory."""

PROMPT_MEMORY_CHARS = 3_500
"""Memory carried into one prompt."""

ALWAYS_SHOWN_IMPORTANCE = 4
"""Preferences at least this important are in every prompt, like pinned memories."""

RECENCY_HALF_LIFE_DAYS = 14.0

SourceKind = Literal["self", "person", "parent", "system"]


@dataclass(frozen=True, slots=True)
class Memory:
    id: uuid.UUID
    kind: str
    content: str
    importance: int
    pinned: bool
    created_at: datetime
    last_recalled_at: datetime | None
    recall_count: int = 0
    source_kind: str = "self"
    source_name: str = ""

    @property
    def handle(self) -> str:
        return memory_handle(self.id)


def memory_handle(memory_id: uuid.UUID) -> str:
    """The short id a bot uses to revise or forget a memory: 6 hex characters."""
    return memory_id.hex[:6]


def memory_id(scope: str, *parts: object) -> uuid.UUID:
    """A memory written by a run: same run, same step → same id, so replay is a no-op."""
    return uuid.uuid5(uuid.NAMESPACE_URL, f"botmem:{scope}:" + ":".join(map(str, parts)))


_WORD = re.compile(r"[a-z0-9][a-z0-9'.@-]*")
_STOP = frozenset(
    """a an and are as at be but by can do for from has have he her his i if in into is it
    its me my no not of on or our she so that the their them then there they this to us
    was we were what when which who will with you your""".split()  # noqa: SIM905
)


def terms(text: str) -> set[str]:
    """Content words, lightly stemmed — enough for "flights" to find "flight"."""
    out: set[str] = set()
    for word in _WORD.findall(text.lower()):
        word = word.strip(".'-")
        if len(word) < 2 or word in _STOP:
            continue
        for suffix in ("ing", "ies", "es", "s", "ed"):
            if len(word) > len(suffix) + 3 and word.endswith(suffix):
                word = word[: -len(suffix)]
                break
        out.add(word)
    return out


def similarity(a: str, b: str) -> float:
    """Overlap of content words, 0..1 — how near-duplicate two memories are."""
    ta, tb = terms(a), terms(b)
    if not ta or not tb:
        return 1.0 if " ".join(a.lower().split()) == " ".join(b.lower().split()) else 0.0
    return len(ta & tb) / len(ta | tb)


DUPLICATE_SIMILARITY = 0.8


def find_duplicate(memories: Sequence[Memory], kind: str, content: str) -> Memory | None:
    """An existing memory of the same kind that says the same thing."""
    best: tuple[float, Memory] | None = None
    for m in memories:
        if m.kind != kind:
            continue
        score = similarity(m.content, content)
        if score >= DUPLICATE_SIMILARITY and (best is None or score > best[0]):
            best = (score, m)
    return None if best is None else best[1]


def clean_memory(content: str) -> str:
    text = " ".join(content.split()).removeprefix("- ").strip()
    return text[:MEMORY_CHARS]


def _age_days(memory: Memory, now: datetime) -> float:
    seen = memory.last_recalled_at or memory.created_at
    return max(0.0, (now - seen).total_seconds() / 86_400)


def recency(memory: Memory, now: datetime) -> float:
    """1.0 just now, 0.5 after `RECENCY_HALF_LIFE_DAYS` without use, and so on."""
    return math.pow(0.5, _age_days(memory, now) / RECENCY_HALF_LIFE_DAYS)


def relevance(memory: Memory, query: set[str]) -> float:
    if not query:
        return 0.0
    mine = terms(memory.content)
    if not mine:
        return 0.0
    hits = len(mine & query)
    # Share of the memory's own words the conversation touches, softened so one shared
    # word in a long memory still counts for something.
    return min(1.0, hits / math.sqrt(len(mine) * max(1, min(len(query), 12))))


def strength(memory: Memory, now: datetime, query: set[str] | None = None) -> float:
    """How readily this memory comes to mind: relevance, importance and recency,
    with a little credit for having been useful before."""
    rehearsal = min(0.2, 0.04 * memory.recall_count)
    return (
        1.6 * relevance(memory, query or set())
        + 0.8 * (memory.importance / 5)
        + 0.7 * recency(memory, now)
        + rehearsal
    )


def always_shown(memory: Memory) -> bool:
    return memory.pinned or (
        memory.kind == "preference" and memory.importance >= ALWAYS_SHOWN_IMPORTANCE
    )


@dataclass(frozen=True, slots=True)
class Recollection:
    shown: list[Memory]
    hidden: int
    """How many memories exist that this prompt does not carry."""


def select_for_prompt(
    memories: Sequence[Memory],
    context: str,
    now: datetime,
    *,
    budget: int = PROMPT_MEMORY_CHARS,
    episodes: int = 5,
) -> Recollection:
    """What comes to mind for this conversation.

    In order: pinned memories and strong preferences; the latest few diary lines (so
    the bot always knows what it was last doing); then the strongest of the rest for
    this context — until the character budget is spent.
    """
    query = terms(context)
    chosen: list[Memory] = []
    used = 0

    def take(m: Memory) -> bool:
        nonlocal used
        cost = len(m.content) + 12
        if used + cost > budget:
            return False
        chosen.append(m)
        used += cost
        return True

    first = sorted((m for m in memories if always_shown(m)), key=lambda m: -m.importance)
    for m in first:
        take(m)
    recent = sorted(
        (m for m in memories if m.kind == "episode" and m not in chosen),
        key=lambda m: m.created_at,
        reverse=True,
    )[:episodes]
    for m in recent:
        take(m)
    rest = sorted(
        (m for m in memories if m not in chosen),
        key=lambda m: strength(m, now, query),
        reverse=True,
    )
    for m in rest:
        if not take(m) and used > budget * 0.9:
            break
    return Recollection(shown=chosen, hidden=len(memories) - len(chosen))


def search(memories: Sequence[Memory], query: str, now: datetime, limit: int = 8) -> list[Memory]:
    """`recall`: the memories that match a query, best first. A memory must share at
    least one word with the query — recall is a search, not a free association."""
    q = terms(query)
    if not q:
        return []
    hits = [m for m in memories if relevance(m, q) > 0]
    return sorted(hits, key=lambda m: strength(m, now, q), reverse=True)[:limit]


def to_forget(memories: Sequence[Memory], now: datetime, cap: int = MAX_MEMORIES) -> list[Memory]:
    """Which memories go when there are more than `cap`: the weakest unpinned ones."""
    excess = len(memories) - cap
    if excess <= 0:
        return []
    candidates = sorted((m for m in memories if not m.pinned), key=lambda m: strength(m, now))
    return candidates[:excess]


def render_memories(recollection: Recollection) -> str:
    if not recollection.shown:
        return ""
    lines = [
        "Your memory — what you know from earlier work. The [id] lets you revise "
        "(remember with `memory`) or forget one."
    ]
    for kind in MEMORY_KINDS:
        group = [m for m in recollection.shown if m.kind == kind]
        if kind == "episode":
            group.sort(key=lambda m: m.created_at)
        if not group:
            continue
        lines.append(f"{KIND_TITLES[kind]}:")
        for m in group:
            mark = " (pinned)" if m.pinned else ""
            when = f"{m.created_at.date().isoformat()}: " if kind == "episode" else ""
            source = (
                f" — from {m.source_name}"
                if m.source_kind in ("person", "parent") and m.source_name
                else ""
            )
            lines.append(f"- [{m.handle}] {when}{m.content}{mark}{source}")
    if recollection.hidden:
        lines.append(
            f"({recollection.hidden} older or less relevant memories are not shown; use "
            "`recall` to search them.)"
        )
    return "\n".join(lines)


def episode_from_turn(asked: str, reply: str, kind: str) -> str:
    """The fallback diary line when the model did not write one."""
    asked = " ".join(asked.split())[:160]
    reply = " ".join(reply.split())[:240]
    verb = "asked them" if kind == "ask_user" else "replied"
    return clean_memory(f"Asked: {asked} — I {verb}: {reply}" if asked else f"I {verb}: {reply}")
