"""Closed vocabularies.

Every one of these is persisted as `text` rather than a Postgres enum: adding a
value must not require a migration, and the application is the authority on which
values are legal. The DB stores what the domain wrote.
"""

from __future__ import annotations

from enum import StrEnum


class ActorKind(StrEnum):
    """What an actor is allowed to do at all.

    `HYBRID` exists so the enum is closed over the eventual design, but it is not
    implemented in M0 or M1: `compile_actor_spec` raises `NotImplementedError` on
    it. Not building it is a stronger guarantee than policing it. When a real case
    arrives it ships with a mandatory `allowed_model_call_sites`.
    """

    LLM_AGENT = "llm_agent"
    DETERMINISTIC_WORKER = "deterministic_worker"
    HYBRID = "hybrid"


class RunStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    ABANDONED = "ABANDONED"
    """Lease expired more times than the cap allows. A human decides what next."""

    WAITING_CHILD = "WAITING_CHILD"
    """M5. The run has spawned a child and is waiting for it.

    **Non-terminal, and the lease is still held.** A parent that waits is still
    working — it owns the run, it heartbeats, and the reaper still reclaims it if the
    worker dies. That is why `claim`, `heartbeat` and `reap_expired` all name this
    status alongside `RUNNING` rather than treating it as a parked state.

    Distinct from `RUNNING` for one reason, and it is a diagnostic one: a department
    with delegation on will have runs whose wall clock is dominated by waiting, and a
    dashboard that could not separate "this run is thinking" from "this run is waiting
    for someone else to think" would make the delegation overhead M5b has to measure
    invisible. It is what turns a five-minute run into an answerable question.
    """

    LIMIT_REACHED = "LIMIT_REACHED"
    """M2. Admission refused the run: a budget pool in its chain had no headroom, or
    a kill switch was engaged.

    A distinct status rather than FAILED because the two need different responses —
    a FAILED run is a bug or a bad input, a LIMIT_REACHED run is the organization
    working correctly and being out of money. Collapsing them is how "we quietly
    stopped working" hides inside a failure rate (edge case 21). The run row exists,
    carries `status_reason`, and emits an event; silence was the alternative and
    silence is the failure mode.
    """

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_RUN_STATUSES


_TERMINAL_RUN_STATUSES = frozenset(
    {
        RunStatus.SUCCESS,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
        RunStatus.ABANDONED,
        RunStatus.LIMIT_REACHED,
    }
)


LIVE_RUN_STATUSES = frozenset({RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.WAITING_CHILD})
"""The three non-terminal statuses, stated once.

Every fan-out count and every cascade walks this set, and the SQL that does it names
the same three values in a partial index predicate (migration 035). Two places, one
list — `test_m5_delegation` asserts they agree, because a status added to one and not
the other is a descendant the cascade would not cancel.
"""


class DelegationStatus(StrEnum):
    """A row in `delegations`. M5.

    `REFUSED` is the value that justifies the table existing: checks 1-6 refuse before
    a run is created, so without a row the refusal leaves no trace anywhere — the same
    silence `LIMIT_REACHED` was invented to avoid (edge case 21).
    """

    SPAWNED = "SPAWNED"
    COMPLETED = "COMPLETED"
    REFUSED = "REFUSED"
    CANCELLED = "CANCELLED"


class ExhaustionPolicy(StrEnum):
    """What a subtree does when it hits its ceiling (M5 §2).

    The two answers are genuinely different decisions and neither is always right,
    which is why this is configuration and not a constant.
    """

    DRAIN = "drain"
    """In-flight children finish, no new ones start, the parent summarises what it
    got. The default: work already paid for is work worth collecting, and a parent
    that can say "I got two of the three" is more useful than one that says nothing."""

    STRICT = "strict"
    """Descendants are cancelled and the parent ends `LIMIT_REACHED`. For a subtree
    whose partial output is worse than no output — where two thirds of an answer reads
    like a whole one."""


class RunPriority(StrEnum):
    """What to shed first when a pool runs short.

    Three values, not a number, because admission has to answer "is this one of the
    ones we drop" and a 0-100 integer makes that a threshold argument every time. The
    numeric `runs.priority` stays — it orders the queue — and this decides admission.
    """

    LOW = "LOW"
    NORMAL = "NORMAL"
    CRITICAL = "CRITICAL"

    @property
    def rank(self) -> int:
        return _PRIORITY_RANK[self]


_PRIORITY_RANK = {RunPriority.LOW: 0, RunPriority.NORMAL: 1, RunPriority.CRITICAL: 2}


class WorkClass(StrEnum):
    """Why a model is being called.

    I12: every model call declares one. It selects the model profile, it is the
    unit the usage ledger aggregates on, and it is what makes "this run spent
    $4 on classification" answerable without parsing prompts.

    Two groups, and the split is what the M1 gate measures.

    The first four are M1's *organizational* classes — the vocabulary v3 §17's
    thesis is stated in. `WORK` is the only one that produces the thing a human
    asked for; everything else is the organization talking to itself, and the
    coordination ratio is the price of that conversation. The M1 department uses
    these four exclusively, which `test_m1_work_class.py` (T25) enforces.

    The rest are M0's finer-grained "what kind of thinking" labels. They remain
    legal — a future actor may want them — but a call tagged with one lands in
    `overhead_ratio`, never in `WORK`. That asymmetry is deliberate: an untagged
    or mis-tagged call should make the numbers look *worse*, not better, so no
    accident can flatter the gate.
    """

    WORK = "work"
    """Produced something a human asked for: a report, a draft, an analysis."""

    COORDINATION = "coordination"
    """Planning, decomposition, assignment, status. Pure overhead by definition."""

    EVALUATION = "evaluation"
    """Judging work that already exists. Overhead, but often worth paying for."""

    SUMMARIZATION = "summarization"
    """Compressing history so the next call is cheaper. Overhead with a payback."""

    REASONING = "reasoning"
    PLANNING = "planning"
    EXTRACTION = "extraction"
    CLASSIFICATION = "classification"
    GENERATION = "generation"
    CRITIQUE = "critique"
    EMBEDDING = "embedding"

    MEMORY = "memory"
    """M3. Extraction, consolidation, promotion review — everything the memory
    subsystem spends off the hot path.

    A separate class rather than reusing `SUMMARIZATION`, because M3 §13 risk 4 is
    that *"consolidation cost is invisible until you look"*, and a cost that lands in
    an existing bucket is invisible by construction. It is deliberately outside
    `M1_WORK_CLASSES`, so memory spend lands in `overhead_ratio` and makes the
    coordination ratio worse rather than better — the same asymmetry that protects
    every other number here.
    """

    PERCEPTION = "perception"
    """Reading an image: a bot looking at its own screen (`look`). Its own class
    because it is the one call that goes to a vision model, with an image in it — a
    cost and a data flow that should be findable as themselves, not folded into the
    step they serve. Overhead, like every class but `WORK`."""


M1_WORK_CLASSES = frozenset(
    {WorkClass.WORK, WorkClass.COORDINATION, WorkClass.EVALUATION, WorkClass.SUMMARIZATION}
)
"""The closed vocabulary the four M1 actors are allowed to use. T25 asserts it."""


class TaskStatus(StrEnum):
    """Where a task is. Distinct from *how it was judged* — see `TaskOutcome`.

    A task can be `SUBMITTED` and not yet judged, or `CLOSED` having been judged
    badly. Collapsing the two into one column is the mistake that makes
    "how many tasks are waiting on the manager" unanswerable.
    """

    DRAFT = "DRAFT"
    ASSIGNED = "ASSIGNED"
    IN_PROGRESS = "IN_PROGRESS"
    SUBMITTED = "SUBMITTED"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL_TASK_STATUSES


_TERMINAL_TASK_STATUSES = frozenset({TaskStatus.CLOSED, TaskStatus.CANCELLED})


class TaskOutcome(StrEnum):
    """How a submitted task was judged. `NULL` in the database until evaluated."""

    ACCEPTED = "ACCEPTED"
    ACCEPTED_WITH_EDITS = "ACCEPTED_WITH_EDITS"
    REWORK_REQUIRED = "REWORK_REQUIRED"
    REJECTED = "REJECTED"

    AUTO_ACCEPTED = "AUTO_ACCEPTED"
    """Nobody judged it before `eval_deadline`. Counted, reported, and deliberately
    excluded from every acceptance metric: an unexamined task is not evidence that
    the organization produced acceptable work. See M1 §7 and T17."""

    @property
    def is_accepted(self) -> bool:
        """Accepted *by a judgement*. `AUTO_ACCEPTED` is excluded on purpose."""
        return self in _ACCEPTED_OUTCOMES

    @property
    def is_terminal(self) -> bool:
        return self is not TaskOutcome.REWORK_REQUIRED


_ACCEPTED_OUTCOMES = frozenset({TaskOutcome.ACCEPTED, TaskOutcome.ACCEPTED_WITH_EDITS})


class RejectionReason(StrEnum):
    """Why a task was rejected. Drives the §10 diagnosis, so it is a closed set."""

    SCHEMA_FAILURE = "SCHEMA_FAILURE"
    """Three validation failures against the pinned output schema. T15."""

    REWORK_EXHAUSTED = "REWORK_EXHAUSTED"
    """The rework cap was reached. T16."""

    QUALITY = "QUALITY"
    """The evaluator judged the work wrong or unusable."""

    OFF_BRIEF = "OFF_BRIEF"
    """Right-looking work that does not answer the task. The §10 common case."""

    CANCELLED = "CANCELLED"


class EvaluatorKind(StrEnum):
    """Who produced an evaluation row.

    Both kinds land in the same table so the confusion matrix is a self-join
    rather than a reconciliation between two stores. §8.2 depends on this.
    """

    MANAGER = "manager"
    HUMAN = "human"
    SYSTEM = "system"
    """Not a judgement: the deadline sweeper recording an AUTO_ACCEPTED."""


class MessageStatus(StrEnum):
    PENDING = "PENDING"
    DELIVERED = "DELIVERED"
    DROPPED = "DROPPED"
    """Refused at the hop limit or as a duplicate. Kept, never deleted — a dropped
    message is the evidence that a loop was cut. T20."""


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    GRANTED = "GRANTED"
    DENIED = "DENIED"
    EXPIRED = "EXPIRED"
    """TTL elapsed with `on_expiry = deny`. Terminal, denying, and counted: a run
    blocked by an unanswered approval is a real signal about operating cost. T22."""

    @property
    def is_decided(self) -> bool:
        return self is not ApprovalStatus.PENDING

    @property
    def permits(self) -> bool:
        return self is ApprovalStatus.GRANTED


class AuthorityLevel(StrEnum):
    """Who may authorise an action.

    M1's resolver was a hardcoded dict with two answers. M2 adds the third, and the
    third is the one that changes the character of the model: with only AUTO and
    HUMAN, "no policy" has to mean AUTO, and an authority model whose default is yes
    is a naming convention. `DENIED` is what lets `ResolvedAuthority` default-deny.
    """

    AUTO = "auto"
    HUMAN = "human"
    DENIED = "denied"
    """No one may authorise this. Not "ask a person" — there is nobody to ask."""


class OnExpiry(StrEnum):
    """What happens when an approval's TTL elapses with nobody having answered.

    M1 had `deny` and `grant` and only ever used `deny`. `escalate` is what makes an
    escalation chain more than a list, and `fail_run` exists because for some actions
    "denied, carry on" is wrong — the work that depended on the approval should stop
    rather than route around it.
    """

    DENY = "deny"
    GRANT = "grant"
    ESCALATE = "escalate"
    FAIL_RUN = "fail_run"


class KillMode(StrEnum):
    """How hard a kill switch stops things.

    `drain` is the default (M2 §7). `halt` stops in-flight gateway calls at the next
    check, which means a call that has already fired its effect leaves an `INTENT`
    row to reconcile by hand — sometimes the right trade, never the right default.
    """

    HALT = "halt"
    DRAIN = "drain"


class KillScope(StrEnum):
    """What a switch covers. `scope_id` names the subject, except for `org`.

    `department` is the operator-shaped one. The other four are the subjects a
    *gateway call* already knows about — this tool, this actor, this connection —
    and they are what an incident is usually about. A department is what a person
    points at: "stop growth" is a sentence somebody says, and expressing it as four
    actor switches engaged one at a time is how one gets missed.

    It resolves through the actor: a department switch covers a call if the actor
    making it belongs to that department. A check with no actor — a tool- or
    connection-only check — therefore cannot be covered by one. See
    `KillSwitchService.check`.
    """

    ORG = "org"
    TOOL = "tool"
    ACTOR = "actor"
    CONNECTION = "connection"
    DEPARTMENT = "department"


class GatewayDecision(StrEnum):
    """The three answers a gateway check can give. One audit row each (§2)."""

    ALLOWED = "allowed"
    DENIED = "denied"
    APPROVAL_REQUIRED = "approval_required"


class CatchupPolicy(StrEnum):
    """What a scheduler does about occurrences it slept through.

    `SKIP` is the default and the only sane one for a weekly plan: waking up after
    a 48-hour outage and firing 48 hourly plans is an incident, not a recovery.
    T19.
    """

    SKIP = "skip"
    """Fire the most recent due occurrence only; record the rest as skipped."""

    ALL = "all"
    """Fire every missed occurrence. For triggers whose work genuinely accumulates."""


class EffectStatus(StrEnum):
    """Lifecycle of one logical side effect.

    `INTENT` is the only status that can be observed after a crash. Everything the
    recovery machinery does is a decision about what an `INTENT` row means.
    """

    INTENT = "INTENT"
    COMMITTED = "COMMITTED"
    FAILED = "FAILED"
    ORPHANED = "ORPHANED"
    """Probe could not determine whether the effect landed. Needs a human."""
    COMPENSATED = "COMPENSATED"


class BlastRadius(StrEnum):
    """How bad it is if this tool runs when it should not have.

    Drives policy at registration, not just recovery: read → auto; reversible →
    auto under a threshold; irreversible → approval, no retries, elevated audit.
    """

    READ = "read"
    REVERSIBLE = "reversible"
    IRREVERSIBLE = "irreversible"


class RecoveryPolicy(StrEnum):
    """What to do when a replay finds an `INTENT` row for this tool."""

    REPLAY_SAFE = "replay_safe"
    """No external mutation. Just run it again."""

    IDEMPOTENCY_KEY = "idempotency_key"
    """Provider dedupes on a key we supply. Re-send with the same key."""

    PROBE = "probe"
    """Search the provider for our marker; commit if found, else re-execute."""

    MANUAL = "manual"
    """We cannot tell. Mark ORPHANED and stop."""


class TrustLevel(StrEnum):
    """Provenance of a value crossing back into the runtime."""

    TRUSTED = "trusted"
    """Produced by the runtime itself."""

    UNTRUSTED = "untrusted"
    """Came from outside — a fetched page, a model completion, a user string."""


class AuditSeverity(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


class ReservationStatus(StrEnum):
    HELD = "HELD"
    RELEASED = "RELEASED"
    EXPIRED = "EXPIRED"


class AllocationStatus(StrEnum):
    """An allocation is advisory (v3 §8) — it declares intent, it holds nothing.

    That is what makes soft oversubscription to `limit x (1 + K)` safe: the hard
    reservation path is still bounded by `limit` at every level, so admitting 1.3x of
    intent cannot become 1.3x of spend. T31.
    """

    LIVE = "LIVE"
    CLOSED = "CLOSED"


class CredentialStatus(StrEnum):
    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"
    """Superseded by a rotation. Kept, so a call that fails against the new version
    can be told which one it should have used."""


# --- M3: memory --------------------------------------------------------------------


class MemoryScope(StrEnum):
    """Who a memory belongs to. The isolation axis, and therefore a security boundary.

    Ordered from narrowest to widest, and the order is used: `wider_than` decides
    whether a promotion is a promotion, and a retrieval for a narrow scope may read
    the wider ones above it but never sideways into a sibling.

    `SESSION` is *not* the same thing as M1's `sessions.summary`. The summary is one
    row the summarizer rewrites; these are extracted facts that happen to be scoped to
    one conversation and die with it.
    """

    SESSION = "session"
    PRIVATE_ACTOR = "private"
    DEPARTMENT = "department"
    COMPANY = "company"

    @property
    def rank(self) -> int:
        return _SCOPE_RANK[self]

    def wider_than(self, other: MemoryScope) -> bool:
        return self.rank > other.rank


_SCOPE_RANK = {
    MemoryScope.SESSION: 0,
    MemoryScope.PRIVATE_ACTOR: 1,
    MemoryScope.DEPARTMENT: 2,
    MemoryScope.COMPANY: 3,
}

PROMOTION_PATH: dict[MemoryScope, MemoryScope] = {
    MemoryScope.PRIVATE_ACTOR: MemoryScope.DEPARTMENT,
    MemoryScope.DEPARTMENT: MemoryScope.COMPANY,
}
"""§8's ladder, one rung at a time: `PRIVATE ──review──► DEPARTMENT ──review──► COMPANY`.

A dict rather than "any wider scope" so that private → company is not reachable in one
step. Two reviews for two rungs is the point: the second reviewer is looking at
something a first reviewer already thought was worth widening, which is a different
question from the first one.

`SESSION` is absent. A session-scoped fact is promoted by being *re-extracted* at a
wider scope, not by being moved — a fact that mattered only inside one conversation has
not yet demonstrated anything.
"""


class MemoryType(StrEnum):
    """What kind of thing a memory row is. Selects nothing structural; it is a filter
    and a reporting axis, and the evals are grouped on it."""

    FACT = "fact"
    EPISODE = "episode"
    PROCEDURE = "procedure"
    ENTITY_REF = "entity_ref"


class MemoryTrust(StrEnum):
    """Provenance of an extracted memory. §6's blunt instrument.

    Distinct from `TrustLevel`, which describes one *block of content* crossing into a
    prompt. This describes a *stored fact* and what it is allowed to reach. The two
    are related by `run_trust`: if any block in a run's context was `UNTRUSTED`, every
    fact extracted from that run is `UNTRUSTED_QUARANTINE`.
    """

    TRUSTED = "TRUSTED"
    """Extracted from a run whose whole context was produced by the runtime."""

    DERIVED = "DERIVED"
    """Produced by consolidation from other memories, all of them trusted. Kept
    separate from TRUSTED because a consolidated fact is a model's summary of facts
    rather than a fact, and eval 7 measures exactly that gap."""

    UNTRUSTED_QUARANTINE = "UNTRUSTED_QUARANTINE"
    """Some part of the originating run's context came from outside. Retrievable only
    by the actor that produced it, never cross-scope, until a human reviews it.

    Deliberately blunt (§6): you cannot reliably tell which extracted fact came from
    which context block, so the whole run's extraction is quarantined rather than the
    parts that look suspicious. A defence that depends on spotting the payload is not
    a defence against the payload you did not spot.
    """

    @property
    def is_quarantined(self) -> bool:
        return self is MemoryTrust.UNTRUSTED_QUARANTINE


class MemoryStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    """A later memory contradicted it. Kept, never deleted: eval 6 is a measurement
    over the pair, and a superseded fact that vanished would make "did supersession
    work" indistinguishable from "the fact was never written"."""
    QUARANTINED = "quarantined"
    RETIRED = "retired"
    """Aged out by importance decay or pruned for never being accessed."""


class PromotionStatus(StrEnum):
    """§8: promotion is a queue an actor proposes into, not a side effect of writing."""

    PROPOSED = "PROPOSED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    WITHDRAWN = "WITHDRAWN"
    """The source memory was superseded or retired before anyone reviewed the
    proposal. Not a judgement, and counted separately from one."""


class RetrievalGrade(StrEnum):
    """The offline grading harness's vocabulary (§5), and eval 3's denominator.

    Three values because the bar is stated over two of them: *"at least 60%
    helpful-or-neutral and at most 5% harmful"*. A four-point scale would have made
    the bar an argument about where the line sits.
    """

    HELPFUL = "helpful"
    NEUTRAL = "neutral"
    HARMFUL = "harmful"
    """Would have made the output worse: wrong, stale, or confidently off-topic
    enough to pull the answer with it. §13 risk 1 is that this is the failure mode
    memory has and governance did not."""

    UNGRADED = "ungraded"
