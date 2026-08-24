"""Specs.

An `ActorSpec` is authored configuration. A `RunSpec` is the *compiled, frozen*
snapshot a single run executes under. The distinction is load-bearing: a worker
loads the RunSpec from `run_specs`, never from live config, so editing an actor
mid-flight cannot change what an in-flight run is allowed to do.

Everything here is immutable and free of I/O.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from runtime.domain.authority import ResolvedAuthority
from runtime.domain.delegation import DelegationLimits
from runtime.domain.enums import ActorKind, MemoryScope, RunPriority, WorkClass
from runtime.domain.errors import SpecError
from runtime.domain.hashing import spec_hash as compute_spec_hash
from runtime.domain.ids import ActorId, OrganizationId, RunId, SessionId, TaskId

Cents = Annotated[int, Field(ge=0)]


class Frozen(BaseModel):
    """Base for every spec type: immutable, no extra keys, enums by value.

    `extra="forbid"` is deliberate. A typo in a spec field must fail compilation,
    not be silently carried along and ignored at the point it was supposed to
    constrain something.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", use_enum_values=False)


class Ceilings(Frozen):
    """Hard per-run limits. Enforced at gateway entry, not advisory.

    `max_llm_calls == 0` is how a deterministic worker is made deterministic: the
    ModelGateway refuses every call rather than trusting the handler not to make
    one.
    """

    max_llm_calls: int = Field(default=32, ge=0)
    max_tool_calls: int = Field(default=64, ge=0)
    max_wall_clock_s: float = Field(default=300.0, gt=0)
    max_cost_cents: Cents = 500
    max_depth: int = Field(default=4, ge=0)


class ModelProfile(Frozen):
    """One resolved model configuration.

    The last three fields are *how* to call the model rather than *which* model, and
    they live here for one reason: this object is already the thing a work class
    resolves to, is already frozen into the RunSpec, and is already hashed into
    `spec_hash`. Putting the expensive dials anywhere else would mean "what was this
    run allowed to spend" had two answers with different lifetimes.
    """

    provider: str
    model: str
    max_output_tokens: int = Field(default=4096, gt=0)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    input_cents_per_mtok: Cents = 0
    output_cents_per_mtok: Cents = 0

    thinking: bool = True
    """Extended thinking, where the provider has it. Off is a real choice for a class
    whose output is mechanical — reformatting, extraction — and it is the cheapest
    dial in this object. On is the default because a model that thinks by default and
    is told not to produces worse work quietly, which is the wrong direction for a
    default to fail in."""

    effort: Literal["low", "medium", "high", "max"] | None = None
    """Overrides the provider's work-class default (`EFFORT_BY_WORK_CLASS`).

    None means "use the table", and the table is still where the standing policy
    lives — this exists for the actor that is a genuine exception, not as the normal
    way to set effort. Note the vocabularies differ slightly by vendor: Anthropic
    reads low/medium/high, DeepSeek low/high/max."""

    web_search: bool = False
    """Ask the *provider* to search on its own server, for calls in this work class.

    Two keys open this door and this is only one of them: the actor must also hold
    `web.search@1`, because a provider-side search never reaches `ToolGateway` — no
    journal row, no per-connection rate limit, no approval. The grant is the
    permission and this is the request, and both are frozen into the same spec.

    Per work class rather than per actor on purpose: searching while compressing
    history is spend with no possible payoff."""


class ModelProfiles(Frozen):
    """Work class → model. A model call names its work class (I12) and the profile
    follows from that; a call site never names a model directly."""

    profiles: dict[WorkClass, ModelProfile] = Field(default_factory=dict)

    def for_work_class(self, work_class: WorkClass) -> ModelProfile:
        try:
            return self.profiles[work_class]
        except KeyError as exc:
            raise SpecError(f"no model profile for work class {work_class}") from exc


class ActorSpec(Frozen):
    """Authored actor configuration, as stored on an `actor_versions` row."""

    name: str = Field(min_length=1, max_length=128)
    kind: ActorKind
    graph_ref: str | None = None
    """`graphs/` registry key, e.g. "echo_agent@1". Required for LLM_AGENT."""
    handler_ref: str | None = None
    """`handlers/` registry key, e.g. "hasher@1". Required for DETERMINISTIC_WORKER."""
    allowed_tools: frozenset[str] = frozenset()
    ceilings: Ceilings = Ceilings()
    model_profiles: ModelProfiles = ModelProfiles()
    allowed_model_call_sites: frozenset[str] | None = None
    """HYBRID only, and mandatory there. `None` for every other kind."""

    @model_validator(mode="after")
    def _check_shape(self) -> Self:
        if self.kind is ActorKind.LLM_AGENT and not self.graph_ref:
            raise ValueError("LLM_AGENT requires graph_ref")
        if self.kind is ActorKind.DETERMINISTIC_WORKER:
            if not self.handler_ref:
                raise ValueError("DETERMINISTIC_WORKER requires handler_ref")
            if self.ceilings.max_llm_calls != 0:
                raise ValueError("DETERMINISTIC_WORKER requires ceilings.max_llm_calls == 0")
        if self.kind is not ActorKind.HYBRID and self.allowed_model_call_sites is not None:
            raise ValueError("allowed_model_call_sites is meaningful only for HYBRID")
        return self


class CompiledSpec(Frozen):
    """An ActorSpec resolved against a specific actor version, plus its hash.

    This is what gets frozen into `run_specs`. It carries no reference back to live
    configuration — everything a run needs is a value here.
    """

    actor_id: ActorId
    actor_name: str
    actor_version: int
    kind: ActorKind
    graph_ref: str | None
    handler_ref: str | None
    allowed_tools: frozenset[str]
    ceilings: Ceilings
    model_profiles: ModelProfiles
    allowed_model_call_sites: frozenset[str] | None
    authority: ResolvedAuthority | None = None
    """M2 §3. The resolution chain's answer, frozen here and therefore hashed into
    `spec_hash` — which is the property that makes "what was this run allowed to do"
    answerable from the spec alone, months later, without reconstructing the policy
    tables as they stood that morning.

    Optional because M0/M1 specs are still valid and still load: a spec with no
    authority resolves every gated action to the M1 default. `RunService` always sets
    it for new runs.
    """

    @property
    def entrypoint(self) -> str:
        ref = self.graph_ref if self.kind is ActorKind.LLM_AGENT else self.handler_ref
        if ref is None:  # unreachable: ActorSpec._check_shape guarantees it
            raise SpecError(f"actor {self.actor_name} has no entrypoint")
        return ref


def compile_actor_spec(
    spec: ActorSpec,
    *,
    actor_id: ActorId,
    actor_version: int,
    authority: ResolvedAuthority | None = None,
) -> tuple[CompiledSpec, str]:
    """Freeze an ActorSpec into a CompiledSpec and return it with its hash.

    `HYBRID` raises here rather than at first model call. Refusing to compile is a
    stronger guarantee than a runtime check that a future edit could route around.

    `authority` is resolved by the caller — `RunService`, from the authority tables —
    and is hashed in with everything else, so two runs admitted under different
    policies have different `spec_hash` values even when the actor spec is identical.
    That is how a policy change becomes visible in the run history rather than being
    something you have to remember happened.
    """
    if spec.kind is ActorKind.HYBRID:
        raise NotImplementedError(
            "ActorKind.HYBRID is not implemented in M0 or M1. A HYBRID actor must "
            "ship with a mandatory allowed_model_call_sites frozenset that the "
            "ModelGateway enforces per call site; until that exists, HYBRID actors "
            "cannot be compiled."
        )
    compiled = CompiledSpec(
        actor_id=actor_id,
        actor_name=spec.name,
        actor_version=actor_version,
        kind=spec.kind,
        graph_ref=spec.graph_ref,
        handler_ref=spec.handler_ref,
        allowed_tools=spec.allowed_tools,
        ceilings=spec.ceilings,
        model_profiles=spec.model_profiles,
        allowed_model_call_sites=spec.allowed_model_call_sites,
        authority=authority,
    )
    return compiled, compute_spec_hash(compiled)


class RunSpec(Frozen):
    """The immutable execution contract for one run.

    Persisted verbatim as JSON in `run_specs` at `start_run()` time and loaded from
    there by the worker. If this and live config disagree, this wins.
    """

    run_id: RunId
    organization_id: OrganizationId
    root_run_id: RunId
    parent_run_id: RunId | None = None
    session_id: SessionId | None = None
    task_id: TaskId | None = None
    """M1. The task this run is working on, if any.

    Two things depend on it. Every metric in §7 is attributed through
    `runs.task_id`, so a run that forgets it produces spend nobody can account to an
    outcome. And the approval gate keys on the *task*, not on the run — a run that
    resumes after a human decision is a different run with a different id, so
    keying on anything run-scoped would lose the approval that was just granted.
    """
    correlation_id: str | None = None
    """The chain this run belongs to. Copied, never regenerated."""
    thread_id: str
    depth: int = Field(default=0, ge=0)
    spec: CompiledSpec
    spec_hash: str
    input: dict[str, Any] = Field(default_factory=dict)
    budget_pool_id: str | None = None
    """M2: the **leaf** of the pool chain — the actor pool. Reservations resolve the
    chain up to the org root in one statement rather than carrying it here, because a
    chain frozen at admission would keep charging a pool that had since been
    re-parented."""
    allocation_id: str | None = None
    """The advisory allocation this run was admitted against. Closed when the run
    reaches a terminal state; §8's soft ceiling counts LIVE ones."""
    priority: RunPriority = RunPriority.NORMAL
    """What gets shed first when a pool runs short (§4). Frozen, because admission
    already made the decision — re-reading it later would let a run be re-prioritised
    after the check that used it."""
    durability: Literal["sync", "async", "exit"] = "sync"
    """LangGraph checkpoint durability. T7 runs against both `sync` and `async`;
    the effect journal is what makes the difference invisible."""

    memory_scopes: tuple[MemoryScope, ...] | None = None
    """M3 §7. Which memory scopes this run may read, frozen at admission.

    `None` means *every scope the run is entitled to* — its session, its actor's private
    store, its department and the company — resolved by `domain.memory.readable_scopes`
    from ids in the run's own context. A tuple narrows that; it can never widen it,
    because `readable_scopes` intersects rather than unions and the ids come from the
    context rather than from here.

    In the spec, and therefore in `spec_hash`, for the same reason authority is: *what
    this run was allowed to read* is part of what it was allowed to do, and a run that
    produced a strange answer six weeks ago should be explicable from its spec without
    reconstructing the configuration as it stood that morning.

    Optional, so every M0, M1 and M2 spec still loads and still runs — `extra="forbid"`
    rejects unknown keys, it does not require known ones. A pre-M3 spec resolves to the
    same default as one written today.
    """

    agent_path: tuple[str, ...] = ()
    """M5 §4, check 1. The actors from the root of this run tree down to and including
    this run's own actor.

    Carried in the spec rather than looked up, because the cycle check runs at
    admission and a run executes under its spec: a path read from `runs` at spawn time
    would be a second source of truth for something the frozen spec already knows, and
    the two would differ for exactly as long as a transaction is open. The column in
    migration 035 is the *audit* copy — written from this, never read into it.

    Empty for every pre-M5 spec and for every root run with delegation off, which is
    the value that makes T68's byte-identical claim hold: an empty tuple serialises to
    `[]` and changes no behaviour, because nothing consults it unless `delegate()` is
    called."""

    delegation: DelegationLimits | None = None
    """M5. The delegation limits this actor was admitted under, frozen.

    `None` means the actor has no `delegation:` block, which refuses everything —
    `DELEGATION_OFF` and `None` mean the same thing and the property below collapses
    them, so no call site has to remember which it got.

    Frozen for the same reason authority is: a limit re-read from live configuration
    could be raised mid-run, and "how many children was this run allowed" should be
    answerable from the run months later without reconstructing the config as it stood
    that morning."""

    @property
    def ceilings(self) -> Ceilings:
        return self.spec.ceilings

    @property
    def delegation_limits(self) -> DelegationLimits:
        """The frozen limits, or the refuse-everything default for a pre-M5 spec."""
        return self.delegation or DelegationLimits()

    @property
    def authority(self) -> ResolvedAuthority:
        """The frozen authority, or an empty one for a pre-M2 spec.

        Returning an empty `ResolvedAuthority` rather than `None` means every caller
        gets the same type and the default-deny path is the same code, so a spec
        written before 016 is governed rather than ungoverned.
        """
        return self.spec.authority or ResolvedAuthority(actor_name=self.spec.actor_name)


class StartRunRequest(Frozen):
    """The only way a run is created. `RunService.start_run()` is the only door."""

    organization_id: OrganizationId
    actor_name: str
    input: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str = Field(min_length=1, max_length=255)
    parent_run_id: RunId | None = None
    root_run_id: RunId | None = None
    session_id: SessionId | None = None
    task_id: TaskId | None = None
    correlation_id: str | None = None
    depth: int = Field(default=0, ge=0)
    priority: int = Field(default=50, ge=0, le=100)
    """Queue order. Distinct from `admission_priority`, which decides whether the run
    is admitted at all — two questions that a single number answers badly."""
    admission_priority: RunPriority = RunPriority.NORMAL
    deadline_s: float | None = Field(default=None, gt=0)
    durability: Literal["sync", "async", "exit"] = "sync"

    # --- M5: delegation --------------------------------------------------------------
    # Everything a child needs that a root run does not, on the *request* rather than
    # behind a second entry point, because I1 says there is one door and §2 says "no
    # second path".
    #
    # Note what is **not** here: `agent_path`. The child's path is *derived* inside
    # `start_run()` by appending the child's own actor name to the parent's, so a
    # caller cannot supply a path that omits the actor it is about to spawn — which
    # would defeat check 1 while looking entirely well-formed. Everything on this
    # request is either the parent's frozen state or the child's request; nothing is
    # the answer to a check.

    memory_scopes: tuple[MemoryScope, ...] | None = None
    """What the child asks to read. Narrows `RunSpec.memory_scopes`; `None` is the
    pre-M5 default and means "every scope this run is entitled to"."""

    parent_agent_path: tuple[str, ...] = ()
    """The parent's path, from its frozen spec. Check 1 runs against this."""

    parent_memory_scopes: tuple[MemoryScope, ...] | None = None
    """The parent's frozen scopes, for check 6. `None` means the parent was
    unrestricted, in which case any narrowing the child asks for is legal."""

    parent_authority: ResolvedAuthority | None = None
    """The parent's frozen authority, for check 5.

    Passed in rather than re-resolved, and the difference is not an optimisation: the
    parent is executing under an authority frozen at *its* admission, and re-resolving
    it here would compare the child against whatever policy exists now. A policy
    loosened this morning would then let a child through that its parent could not
    itself have performed — which is precisely the escalation check 5 exists to stop.
    """

    delegation_node: str = ""
    delegation_ordinal: int = Field(default=0, ge=0)
    """Where in the parent's graph the delegation was asked for. Recorded on the
    `delegations` row for diagnosis, and *not* used for idempotency — the child's own
    `idempotency_key` is already the deterministic function of these two plus the
    parent and the target, so `uq_run_idem` is the guard and this is the label."""

    parent_limits: DelegationLimits | None = None
    """The parent's frozen delegation limits — checks 2, 3, 4, 7 and 8.

    **The parent's, not the child's**, and that is the whole shape of the rule: a
    subtree ceiling belongs to whoever opened the subtree. A child's own limits govern
    what *it* may delegate, one level further down, and are resolved from its own actor
    row like any other run's.
    """

    @model_validator(mode="after")
    def _check_child_shape(self) -> Self:
        if self.parent_run_id is not None and not self.parent_agent_path:
            raise ValueError(
                "a run with a parent_run_id must carry its parent's agent_path; an "
                "empty path would make the cycle check vacuous for every descendant "
                "below this run"
            )
        if self.parent_run_id is None and self.parent_agent_path:
            raise ValueError("parent_agent_path was given without a parent_run_id")
        return self
