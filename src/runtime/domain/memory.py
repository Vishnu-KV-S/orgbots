"""Memory as values.

Everything here is a value or a pure function over values: what a memory *is*, which
scopes a run may read, how a retrieved set is ordered, and how a run's trust collapses
to one level. Nothing embeds, nothing queries, nothing decides when to write.

It lives in `domain` for the same reason `authority` and `trust` do — see
`ARCHITECTURE.md` §3. A `ScopeFilter` is the frozen answer to "what may this run read",
`run_trust` is a pure function of the blocks that were in a prompt, and both have to be
immutable and I/O-free to be worth anything. The things that *decide* — `MemoryService`,
`ContextPlanner` — sit above the gateway where they always would have.

Three of the functions here are load-bearing for a security property rather than for a
feature, and each says so at its definition: `ScopeFilter.validate`, `run_trust`, and
`readable_scopes`.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from runtime.domain.enums import (
    MemoryScope,
    MemoryStatus,
    MemoryTrust,
    MemoryType,
    TrustLevel,
)
from runtime.domain.errors import MemoryScopeViolation
from runtime.domain.ids import MemoryId

# --- scope -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, order=True)
class ScopeKey:
    """One addressable memory scope: a level and the thing at that level.

    `scope_id` is a UUID for every level. For `PRIVATE_ACTOR` it is the actor id, for
    `SESSION` the session id, for `DEPARTMENT` and `COMPANY` a stable id derived from
    the organization and the department name — never the *name*, because a department
    renamed in week nine would otherwise orphan every memory written in week eight.
    """

    scope: MemoryScope
    scope_id: UUID

    def __str__(self) -> str:
        return f"{self.scope.value}:{self.scope_id}"


def readable_scopes(
    *,
    session: UUID | None,
    actor: UUID,
    department: UUID | None,
    company: UUID,
    requested: Sequence[MemoryScope] | None = None,
) -> tuple[ScopeKey, ...]:
    """The scopes one run may read, widest-inclusive and sibling-exclusive.

    **This is the isolation rule, stated once.** A run reads its own session, its own
    actor's private store, its own department, and the company — and nothing else. It
    can never name another actor's private scope or another department, because the
    ids come from the run's own context rather than from anything the run supplies.
    T48 and T49 are the tests, and they are security tests: the property they check is
    that a *query* cannot widen a *scope*, which is a different claim from "the filter
    string was well formed".

    `requested` narrows — `RunSpec.memory_scopes` may ask for less than the run is
    entitled to — and can never widen. A requested scope the run has no id for is
    dropped rather than raising: an actor with no department is a real configuration,
    and refusing to retrieve at all would be a worse answer than retrieving less.
    """
    available: dict[MemoryScope, UUID] = {
        MemoryScope.COMPANY: company,
        MemoryScope.PRIVATE_ACTOR: actor,
    }
    if session is not None:
        available[MemoryScope.SESSION] = session
    if department is not None:
        available[MemoryScope.DEPARTMENT] = department

    wanted = set(requested) if requested is not None else set(available)
    return tuple(
        ScopeKey(scope, available[scope])
        for scope in sorted(available, key=lambda s: s.rank)
        if scope in wanted
    )


_SAFE_SCOPE_VALUE = re.compile(r"\A[0-9a-fA-F-]{36}\Z")


@dataclass(frozen=True, slots=True)
class ScopeFilter:
    """A validated retrieval filter, ready to hand to a store.

    §3.4 is about a class of bug rather than about two specific CVEs: *a filter that
    silently matches something other than what you asked for*. Two of those were fixed
    in the library; the next one has not been found yet. So the filter is validated
    here, on our side of the port, before any adapter sees it — and `validate` refuses
    rather than coerces, because a filter that repairs a malformed value is a filter
    that matches something nobody asked for.
    """

    organization_id: UUID
    scopes: tuple[ScopeKey, ...]
    include_quarantined_for_actor: UUID | None = None
    """§6: a quarantined memory is retrievable *by the actor that produced it* and by
    nobody else. `None` — the default — means quarantine is invisible. This is never
    set from a query; it is set from the run's own actor id."""
    statuses: frozenset[MemoryStatus] = frozenset({MemoryStatus.ACTIVE})
    memory_types: frozenset[MemoryType] | None = None

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Refuse anything a store could misread. Raises `MemoryScopeViolation`.

        The empty-scope check is the one worth pausing on. An empty scope list is not
        "match nothing" in every store — in some it is "no filter", which is
        "everything". Refusing to build the query is the only reading that is safe in
        both, and T46 asserts it.
        """
        if not self.scopes:
            raise MemoryScopeViolation(
                "a scope filter with no scopes is ambiguous: some stores read an empty "
                "filter as 'no filter', which is 'every scope'. Name the scopes."
            )
        if not isinstance(self.scopes, tuple):  # pragma: no cover - dataclass guarantees it
            raise MemoryScopeViolation(
                f"scopes must be a tuple, got {type(self.scopes).__name__}; a bare string "
                "is iterated character by character by some filter builders (§3.4)"
            )
        for key in self.scopes:
            if not isinstance(key, ScopeKey):
                raise MemoryScopeViolation(f"not a ScopeKey: {key!r}")
            if not isinstance(key.scope_id, UUID):
                raise MemoryScopeViolation(
                    f"scope {key.scope.value} has a non-UUID id {key.scope_id!r}; "
                    "scope ids come from the run context, never from a query string"
                )
        if not self.statuses:
            raise MemoryScopeViolation("a scope filter with no statuses matches nothing usefully")

    def scope_values(self) -> list[str]:
        """The `scope:id` strings, as a **list**, for a store's `in` filter.

        A list and never a string. §3.4's first bug was a filter builder iterating a
        string into a one-character-per-element array, so a filter for one id matched
        several unrelated single characters. `store_filter` below is the only place
        this is called and it is typed to return a list for that reason.
        """
        return [str(key) for key in self.scopes]

    def store_filter(self) -> dict[str, Any]:
        """The filter handed *down* to the vector store.

        Defence in depth, not the boundary. The authoritative predicate is the join
        against `memory_metadata` in `MemoryMetadataRepository.authorize` — see §13
        risk 3 and `docs/M3_LIBRARY_FACTS.md` fact 4. If this filter regressed to
        matching nothing we would lose recall; if it regressed to matching everything
        the join would still refuse the rows.
        """
        values = self.scope_values()
        for value in values:
            level, _, ident = value.partition(":")
            if not _SAFE_SCOPE_VALUE.match(ident) or level not in set(MemoryScope):
                raise MemoryScopeViolation(f"scope value {value!r} is not of the form 'level:uuid'")
        return {"organization_id": str(self.organization_id), "scope_key": {"in": values}}


# --- records -----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    """One row of `memory_metadata`, as a value. The sidecar, not the vector.

    The text lives in the store; this is everything the *policy* needs and none of
    what the *search* needs, which is the split §4 draws: "Mem0 owns its tables inside
    `mem`. You own the metadata sidecar."
    """

    memory_id: MemoryId
    organization_id: UUID
    scope: MemoryScope
    scope_id: UUID
    memory_type: MemoryType
    trust: MemoryTrust
    embedding_version: str
    status: MemoryStatus = MemoryStatus.ACTIVE
    source_run_id: UUID | None = None
    source_actor_version: int | None = None
    source_artifact_id: UUID | None = None
    confidence: float = 0.5
    importance: float = 0.5
    access_count: int = 0
    age_days: float = 0.0
    supersedes: MemoryId | None = None
    superseded_by: MemoryId | None = None

    @property
    def scope_key(self) -> ScopeKey:
        return ScopeKey(self.scope, self.scope_id)


@dataclass(frozen=True, slots=True)
class RetrievedMemory:
    """A candidate, with why it is a candidate. What `context_traces.retrieved` holds."""

    memory_id: MemoryId
    text: str
    similarity: float
    record: MemoryRecord
    rank: int = 0
    score: float = 0.0
    injected: bool = False

    def trace_entry(self) -> dict[str, Any]:
        """The shape written into `context_traces.retrieved`.

        Includes the *component* scores, not just the total. When eval 2's precision
        starts drifting, "which term is doing the damage" is the first question, and a
        trace holding one number cannot answer it.
        """
        return {
            "memory_id": str(self.memory_id),
            "score": round(self.score, 6),
            "similarity": round(self.similarity, 6),
            "rank": self.rank,
            "injected": self.injected,
            "scope": self.record.scope.value,
            "trust": self.record.trust.value,
            "importance": round(self.record.importance, 4),
            "access_count": self.record.access_count,
            "age_days": round(self.record.age_days, 3),
        }


# --- reranking ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RerankWeights:
    """§7's four terms. `[CHOSEN]` — every one of these is a guess.

    They are a value object rather than four constants so that the tuning shows up in a
    diff, which is what §7 asks for: *"Keep them in config, not buried in code — you
    will tune them, and the tuning should show up in a diff."* `ContextPlanner` reads
    them from `Settings`; this is the default.
    """

    similarity: float = 1.0
    importance: float = 0.3
    recency: float = 0.2
    access: float = 0.1

    half_life_days: float = 30.0
    """`[CHOSEN]`. A fact from a month ago scores half the recency term of one from
    today. Nothing derived this; it is a starting position that says "a marketing
    department's world turns over in about a month"."""

    access_saturation: float = 5.0
    """`[CHOSEN]`. Access count is evidence of usefulness with sharply diminishing
    returns — the difference between never-used and used-twice is the signal, and the
    difference between forty and eighty is noise. Saturating rather than logging raw
    counts also stops a single hot memory from dominating every query it is adjacent
    to, which is how a store develops a favourite."""


def rerank(
    candidates: Sequence[RetrievedMemory],
    *,
    weights: RerankWeights | None = None,
    top_k: int,
) -> list[RetrievedMemory]:
    """Order candidates by `recency x importance x similarity x access decay` (§7).

    Additive over normalised terms rather than multiplicative, despite §7's wording:
    a product makes any single zero term annihilate the row, so a genuinely relevant
    fact that nobody has read yet (`access_count = 0`) would score zero and never be
    read — which is the state every new memory starts in. The weights are what make it
    behave multiplicatively where that is wanted.

    Ties break on `memory_id`, not on input order. A retrieval whose output depends on
    the order the store happened to return rows is a retrieval that is not comparable
    between the attempt that crashed and the attempt that resumes, and the shadow-mode
    traces would be measuring the store's stability rather than ours.
    """
    w = weights or RerankWeights()
    scored: list[RetrievedMemory] = []
    for candidate in candidates:
        record = candidate.record
        recency = 0.5 ** (max(0.0, record.age_days) / w.half_life_days) if w.half_life_days else 1.0
        access = 1.0 - math.exp(-record.access_count / max(w.access_saturation, 1e-9))
        score = (
            w.similarity * candidate.similarity
            + w.importance * record.importance
            + w.recency * recency
            + w.access * access
        )
        scored.append(
            RetrievedMemory(
                memory_id=candidate.memory_id,
                text=candidate.text,
                similarity=candidate.similarity,
                record=record,
                score=score,
            )
        )

    scored.sort(key=lambda c: (-c.score, str(c.memory_id)))
    return [
        RetrievedMemory(
            memory_id=c.memory_id,
            text=c.text,
            similarity=c.similarity,
            record=c.record,
            rank=i,
            score=c.score,
            injected=False,
        )
        for i, c in enumerate(scored[:top_k])
    ]


# --- trust -------------------------------------------------------------------------


def run_trust(block_trusts: Iterable[TrustLevel]) -> MemoryTrust:
    """Collapse a run's context blocks to the trust every fact extracted from it gets.

    **`min` over the blocks, deliberately blunt** (§6). One untrusted block quarantines
    the whole extraction, not the facts that look like they came from it.

    The reason is that the alternative does not exist. Extraction reads a whole prompt
    and emits facts; nothing in that process records which span each fact came from,
    and a model asked to self-report the provenance of its own extraction is being
    asked by the same mechanism the attacker is talking to. So the honest choices are
    "quarantine everything" or "quarantine nothing", and T52 is the test that we picked
    the first one.

    An empty context is `TRUSTED` — a run that read nothing from outside read nothing
    from outside.
    """
    return (
        MemoryTrust.UNTRUSTED_QUARANTINE
        if any(t is TrustLevel.UNTRUSTED for t in block_trusts)
        else MemoryTrust.TRUSTED
    )


def status_for(trust: MemoryTrust) -> MemoryStatus:
    """§6's last line: *"if run_trust == UNTRUSTED → status='quarantined'"*."""
    return MemoryStatus.QUARANTINED if trust.is_quarantined else MemoryStatus.ACTIVE


# --- token budget ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InjectionBudget:
    """What may be spent on retrieved memory in one call. §7's "hard token cap".

    Eval 8 has no bar on purpose — *"You cannot know what injected memory costs per
    call until you measure it"* — so this object's defaults are a cap that stops the
    unbounded case, not a budget derived from anything. The measured budget replaces
    `max_tokens` once shadow mode has produced a number.
    """

    max_memories: int = 6
    """`[CHOSEN]`. §7's `K=3-6` injected, at the top of the range."""
    max_tokens: int = 900
    """`[CHOSEN]`. A hard stop, not an estimate of what is worthwhile."""
    max_chars_per_memory: int = 600

    def fits(self, used_tokens: int, next_tokens: int, count: int) -> bool:
        return count < self.max_memories and used_tokens + next_tokens <= self.max_tokens


def estimate_tokens(text: str) -> int:
    """Four characters to a token, the same estimator `_estimate_cents` uses.

    Wrong in the third significant figure and consistent with every other number in
    this codebase, which is the property that matters: eval 8 compares injected tokens
    against an M2 baseline computed the same way, and two estimators would make the
    comparison a measurement of the estimators.
    """
    return max(1, len(text) // 4)


@dataclass(slots=True)
class PlannedContext:
    """What `ContextPlanner` decided, and enough to explain it afterwards.

    Carries the candidates it *rejected* as well as the ones it kept, because
    `context_traces` is graded offline against "would this have helped" and a trace
    holding only the winners cannot answer "was the right one available and passed
    over" — which is eval 1 failing differently from eval 2.
    """

    query_text: str
    retrieved: list[RetrievedMemory] = field(default_factory=list)
    injected: list[RetrievedMemory] = field(default_factory=list)
    injected_tokens: int = 0
    would_have_injected_tokens: int = 0
    """Eval 8's numerator during shadow mode, and equal to `injected_tokens` when live.

    A separate field rather than a derived one because the two are genuinely different
    measurements: one is spend, the other is spend avoided, and a single column would
    make "what did memory cost us this week" unanswerable for the fourteen days when
    the answer was zero and the counterfactual was not."""
    shadow_mode: bool = True

    @property
    def block(self) -> str:
        """The prompt section, or empty in shadow mode.

        Empty is not an accident of having no memories: in shadow mode the planner
        retrieves, scores and traces, then returns nothing, so the prompt is
        byte-identical to M2's. That identity is the whole value of phase one.
        """
        if self.shadow_mode or not self.injected:
            return ""
        lines = "\n".join(f"- {m.text}" for m in self.injected)
        return (
            "## What you already know\n"
            "Recalled from earlier work in this organization. Treat it as your own "
            "prior notes: useful, and not necessarily still true. If it contradicts "
            "what you are looking at now, what you are looking at now wins, and say so.\n\n"
            f"{lines}"
        )
