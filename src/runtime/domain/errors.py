"""Error taxonomy.

Split by who has to act. `RuntimeFault` is ours; `PolicyViolation` means the caller
asked for something the runtime refuses; `TransientFault` is worth retrying and
nothing else is.
"""

from __future__ import annotations


class RuntimeError_(Exception):
    """Base for every error this runtime raises. Named with a trailing underscore
    so it never shadows the builtin at an import site."""


# --- configuration / registration -------------------------------------------------


class SpecError(RuntimeError_):
    """A spec cannot be compiled."""


class IncoherentEffectPolicy(SpecError):
    """A tool's declared `recovery_policy` is not entailed by its capabilities.

    Registration fails. This is the check that stops "probe" from being declared on
    a tool that has no searchable marker, which would silently degrade to
    re-execution on every replay.
    """


class UnknownToolError(SpecError):
    pass


class UnknownActorError(SpecError):
    pass


# --- policy -----------------------------------------------------------------------


class PolicyViolation(RuntimeError_):
    """The call is well-formed but not permitted."""


class ToolNotAllowed(PolicyViolation):
    pass


class ModelCallNotAllowed(PolicyViolation):
    """A deterministic worker tried to call a model, or a HYBRID actor called from
    an undeclared call site."""


class MissingWorkClass(PolicyViolation):
    """I12: a model call arrived without a `work_class`."""


class CeilingExceeded(PolicyViolation):
    pass


class ApprovalRequired(PolicyViolation):
    pass


class KillSwitchEngaged(PolicyViolation):
    pass


class MissingCredentials(SpecError):
    """A provider has no resolvable credential.

    A configuration fault, not a transient one — deliberately *not* a
    `TransientFault`, because retrying a missing API key only spends the retries.
    Distinct from `ProviderUnavailable` for exactly that reason: one is worth
    waiting out and the other needs a person.
    """


# --- budget -----------------------------------------------------------------------


class BudgetError(RuntimeError_):
    pass


class BudgetExceeded(BudgetError):
    """I8: `committed + reserved` would exceed `limit`. The DB constraint is the
    backstop; this is the check that produces a decent message first."""


class ReservationNotFound(BudgetError):
    pass


# --- lease / concurrency ----------------------------------------------------------


class LeaseError(RuntimeError_):
    pass


class StaleFence(LeaseError):
    """The run's fence advanced past the one this worker holds.

    Raised at every gateway entry. This is what stops a thawed zombie from
    committing an effect for a run that someone else now owns.
    """

    def __init__(self, run_id: object, held: int, current: int) -> None:
        super().__init__(f"run {run_id}: held fence {held}, current fence {current}")
        self.held = held
        self.current = current


class LeaseLost(LeaseError):
    pass


# --- effects ----------------------------------------------------------------------


class EffectError(RuntimeError_):
    pass


class EffectOrphaned(EffectError):
    """Recovery could not determine whether the effect landed. A human decides."""


class DuplicateEffect(EffectError):
    """A committed effect was replayed under a policy that forbids re-execution."""


# --- artifacts --------------------------------------------------------------------


class ArtifactError(RuntimeError_):
    pass


class ArtifactWriteFailed(ArtifactError):
    """Fail-closed: the run fails rather than reporting success with no output."""


class ArtifactNotFound(ArtifactError):
    pass


# --- transient --------------------------------------------------------------------


class TransientFault(RuntimeError_):
    """Retryable. Nothing else is retryable."""


class ToolTimeout(TransientFault):
    pass


class ProviderUnavailable(TransientFault):
    pass


# --- validation -------------------------------------------------------------------


class ValidationFailed(RuntimeError_):
    """A value did not match its declared model.

    Raised by the tool gateway for tool args and results, and by the schema
    registry for a task output. `errors` carries the structured failure list when
    the raiser has one — the assignee is told *what* to fix, not merely that
    something was wrong, which is the difference between a rework cycle that
    converges and one that does not. See T14.
    """

    def __init__(self, message: str, *, errors: object = None, schema_ref: str | None = None):
        super().__init__(message)
        self.errors = errors
        self.schema_ref = schema_ref


class OutputSchemaViolation(ValidationFailed):
    """A task result did not match its pinned `output_schema_ref`.

    Distinct from a tool-argument failure because the response is different: a bad
    tool call fails the run, a bad task output sends the task back to its assignee
    with the error list, up to the schema-failure cap. T14/T15.
    """


class HopLimitExceeded(PolicyViolation):
    """A message would exceed `max_hop_count`. The loop stops here. T20."""


class TaskStateError(RuntimeError_):
    """An illegal task transition — evaluating a task that was never submitted,
    reworking one past the cap. The state machine refuses rather than repairing."""


class UpstreamNotReady(TaskStateError):
    """A task declares a `source_task_id` that has produced no output.

    Fail-closed, and the reason is the failure it replaces. An assignee handed an
    empty dependency still has a schema to satisfy — `ContentDraft` requires at
    least one `Source` with a real URL and a quote — so the honest answer ("there
    was no research") does not validate and the *dishonest* one does. The observed
    result was a draft citing `https://example.com/...` that passed validation and
    was indistinguishable from work. A run that stops here costs one wasted
    dispatch; one that continues costs a fabricated deliverable nobody can tell
    from a real one.

    Not a `TransientFault`: waiting is the dispatcher's job (it holds the wake-up
    until the dependency lands), and by the time a run has started the upstream is
    finished and empty."""


class ApprovalPending(PolicyViolation):
    """An approval exists for this action but nobody has decided it yet.

    Not a failure of the run so much as a fact about the organization: the work
    stopped because a human has not answered. Counted as such."""


class ApprovalDenied(PolicyViolation):
    """A human said no, or the TTL elapsed under `on_expiry = deny`. T22."""


# --- M2: governance ---------------------------------------------------------------


class AuthorityError(SpecError):
    """A policy set that cannot be compiled. Raised when a spec is applied, not when
    a run hits it — catching it at compile is free, catching it at runtime is a
    deadlock."""


class EscalationCycle(AuthorityError):
    """An approver is not strictly up-hierarchy from the requester.

    v3 edge case 30: A approves for B and B approves for A, directly or transitively.
    At runtime this is two runs each waiting for the other's approver; at compile time
    it is a graph walk that costs nothing.
    """


class DelegationWidening(AuthorityError):
    """A child spec claims authority its parent does not have. Dormant until M5."""


class AuthorityDenied(PolicyViolation):
    """The resolved authority for this action is `denied`. Nobody can approve it —
    distinct from `ApprovalPending`, where somebody can and has not."""


class GrantRevoked(PolicyViolation):
    """The actor held a grant for this tool at admission and does not now.

    Separate from `ToolNotAllowed` because the two mean different things to whoever
    reads the denial stream: one is an actor mis-scoped from the start, the other is
    governance working — someone took a permission away and the runtime noticed
    inside the ≤30s cache window (edge case 64).
    """


class RateLimited(TransientFault):
    """A token bucket was empty. Transient by construction: the bucket refills."""


class ResumeTokenInvalid(PolicyViolation):
    """A resume token was unknown, already spent, or bound to a different interrupt.

    v3 edge case 28. The third case is the one worth having a name for: an approval
    for *this* run's publish must not resume that same run's *other* pause.
    """


class ApproverBudgetExceeded(PolicyViolation):
    """An approver already has a full day's queue.

    Not raised into a run — the run gets `ApprovalPending` as usual. This is what the
    alert path raises internally so the deferral has a typed reason (edge case 31).
    """


class CredentialRotationError(RuntimeError_):
    """A rotation could not be applied atomically. Nothing was changed."""


class DecryptionFailed(CredentialRotationError):
    """Ciphertext would not open under any known key. Almost always a `key_id` that
    names a key this process was not given, which is a deployment fault and not a
    data fault — so it is loud rather than falling back to an empty credential."""


# --- M3: memory --------------------------------------------------------------------


class MemoryError_(RuntimeError_):
    """Base for the memory subsystem. Trailing underscore for the same reason
    `RuntimeError_` has one: `MemoryError` is a builtin, and shadowing it inside a
    module that also raises the builtin's cousins is how a bare `except MemoryError`
    stops catching what its author meant."""


class MemoryScopeViolation(MemoryError_, PolicyViolation):
    """A retrieval filter would have read something the run may not read.

    **A `PolicyViolation` as well as a memory error, and that dual inheritance is the
    point.** Cross-scope leakage is eval 5 and it is a zero-tolerance gate; a failure
    here must be caught by anything watching for policy failures, not only by code
    that happens to know memory exists. Raised at filter *construction*, before a store
    is touched, so the malformed filter never reaches a query planner at all (§3.4).
    """


class EmbeddingDimensionMismatch(MemoryError_):
    """A vector of the wrong width was offered to a collection.

    pgvector fixes the column width at first write (§3.3), so this is the error a
    silent embedder swap produces on the *next insert* — days later, in a worker, with
    nothing connecting it to the config change that caused it. Raising it by name from
    our own adapter, with both dimensions and the collection in the message, is the
    difference between a five-minute diagnosis and an afternoon. T45.
    """


class MemoryStoreUnavailable(MemoryError_, TransientFault):
    """The store could not be reached, or the adapter's dependency is not installed.

    Transient by inheritance so that a memory outage degrades a run rather than
    failing it: retrieval is an enrichment, and a department that stops working
    because its memory is down has traded a real capability for a hypothetical one.
    The write path is on a worker and retries there.
    """


class DelegationRefused(PolicyViolation):
    """Base for M5 §4's eight admission checks. Every one of them refuses.

    A `PolicyViolation` rather than a fault: a parent that asked for a child it may
    not have is a well-formed request the runtime declines, and the parent is expected
    to catch it and carry on. A delegation that failed is not a run that failed — §9
    risk 1 is that delegation multiplies existing problems, and a parent that dies
    because a child was refused would multiply this one immediately.

    Distinct from `DelegationWidening`, which is an `AuthorityError` raised when a
    *configuration* is compiled. These are raised at admission, against live counts and
    a live pool, about a delegation somebody actually attempted.
    """


class DelegationDisabled(DelegationRefused):
    """`RUNTIME_DELEGATION_ENABLED` is false. T68.

    Raised rather than returned as an empty result, and never caught into a silent
    no-op: a graph that quietly did the work itself when delegation was off would make
    the flag change *behaviour* rather than *capability*, and M5a's whole exit claim is
    that a checkout with the flag off is indistinguishable from M4.
    """


class DelegationCycle(DelegationRefused):
    """Check 1: the target is already in `agent_path`. A→B→A. Edge case 26."""


class DepthExceeded(DelegationRefused):
    """Check 2: `depth + 1` is past `max_delegation_depth`."""


class FanoutExceeded(DelegationRefused):
    """Checks 3 and 4: too many live children, or too many live descendants.

    One exception for two limits because the parent's response to both is the same —
    do it yourself, or wait — and the message names which one it was. Edge case 27.
    """


class PrivilegeEscalation(DelegationRefused):
    """Check 5: the child would hold authority its parent does not. Edge case 30.

    The runtime counterpart of `DelegationWidening`, and the one that matters for
    T70: an injected instruction that persuades a parent to delegate with a wider
    grant fails here, at admission, before the child exists.
    """


class ScopeEscalation(DelegationRefused, MemoryError_):
    """Check 6: the child asked for memory scopes its parent does not hold.

    A `MemoryError_` as well, for the same reason `MemoryScopeViolation` is a
    `PolicyViolation`: scope widening is a security property, and anything watching
    either category has to see it. Edge cases 30 and 81.
    """


class SubtreeBudgetExceeded(DelegationRefused, BudgetError):
    """Check 7: the run tree's spend ceiling. Edge case 22.

    Both a refusal and a budget error, because it is genuinely both: the caller may
    not have the child, *and* the reason is money. A `BudgetExceeded` handler that did
    not see this would report a healthy pool while the subtree was capped.
    """


class SubtreeCallsExceeded(DelegationRefused, BudgetError):
    """Check 8: the run tree's model-call ceiling, which binds separately from cost.

    Edge case 24. A tree of cheap calls can exhaust attention and wall clock without
    coming near the cost ceiling, and a count is the only limit that sees it.
    """


class PromotionNotReviewed(MemoryError_, PolicyViolation):
    """Something tried to widen a memory's scope without a `memory_promotions` row.

    §8: *"Automatic promotion means company memory accumulates whatever the extraction
    model happened to emit, with no one accountable for any of it."* T53 asserts that
    no path exists; this is what that path would raise if someone built one.
    """
