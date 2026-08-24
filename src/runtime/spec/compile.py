"""Documents → a compiled organization.

The output of this module is the thing `apply` writes and the thing §8's round-trip
test hashes. It contains **no live state and no database handles**: documents in,
values out, so the compiler is testable without Postgres and the round-trip test is a
unit test rather than an integration one.

**The dependency graph, and the one cycle that is not a mistake.**

    Actor      → Department, Role, ModelProfile, MemoryProfile
    Department → Actor            (its head)
    Role       → Role             (its parent)
    ToolGrant  → Actor|Role, Connection
    Policy     → Role, Actor|Department|Role
    Trigger    → Actor
    Budget     → Department, Actor

A department names its head actor and that actor names its department (§7). That is
structural, not an error, so the `Department → Actor` edge is **deferred**: it is
removed before the topological sort and resolved afterwards, in phase 2. Any *other*
cycle is refused, with the cycle printed — a reporting loop or a role tree that eats
its own tail is a real configuration error and the message should say which documents
form it.

**`ActorSpec` is the compilation target, not a new type.** The whole claim of M4 is
that the config plane changed nothing, and the cheapest way to be sure of that is for
the compiler to construct exactly the object `runtime.org.department` constructs. Same
type, same fields, same `canonical_hash`. A parallel "compiled YAML spec" type would
have to be proved equivalent; this one is equal.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from runtime.domain.enums import (
    AuthorityLevel,
    CatchupPolicy,
    MemoryScope,
    OnExpiry,
    WorkClass,
)
from runtime.domain.hashing import canonical_hash
from runtime.domain.specs import ActorSpec, Ceilings, ModelProfile, ModelProfiles
from runtime.observability.logging import get_logger
from runtime.spec.documents import (
    ActorDoc,
    AuthorityPolicyDoc,
    BudgetPolicyDoc,
    ConnectionDoc,
    DelegationDoc,
    DepartmentDoc,
    Document,
    DocumentSet,
    Kind,
    MemoryProfileDoc,
    ModelProfileDoc,
    OrganizationDoc,
    RoleDoc,
    ToolGrantDoc,
    TriggerDoc,
    parse_scope,
)
from runtime.spec.errors import SpecValidationError

log = get_logger("spec.compile")


# --- the compiled values ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CompiledDepartment:
    name: str
    head: str | None
    parent: str | None
    description: str


@dataclass(frozen=True, slots=True)
class CompiledRole:
    name: str
    rank: int
    parent: str | None
    department: str | None
    approver: str | None
    approver_daily_budget: int
    base_authority: dict[str, Any]


@dataclass(frozen=True, slots=True)
class CompiledActor:
    """One actor, resolved. `spec` and `spec_hash` are the §8 comparison points."""

    name: str
    spec: ActorSpec
    spec_hash: str
    uid: UUID | None
    department: str | None
    role: str | None
    reports_to: str | None
    memory_scopes: tuple[MemoryScope, ...]
    """Resolved and narrowed. Empty means *every scope the actor is entitled to* — the
    same meaning `RunSpec.memory_scopes = None` carries, spelled as an empty tuple here
    because a dataclass field that is both "unset" and "explicitly nothing" is a bug
    waiting for a reader who assumes the wrong one."""
    memory_departments: tuple[str, ...]
    """The department names the scopes referred to, kept so validation can check the
    actor may hold them (edge case 81) without re-parsing the tokens."""
    delegation: DelegationDoc | None
    labels: dict[str, str]


@dataclass(frozen=True, slots=True)
class CompiledConnection:
    name: str
    provider: str
    scopes: tuple[str, ...]
    credential_name: str | None
    status: str


@dataclass(frozen=True, slots=True)
class CompiledGrant:
    subject_type: str
    subject_id: str
    tool: str
    connection: str | None


@dataclass(frozen=True, slots=True)
class CompiledPolicy:
    scope_type: str
    scope_id: str
    action: str
    level: AuthorityLevel
    approver_role: str | None
    max_escalations: int
    on_expiry: OnExpiry
    ttl_seconds: int
    source: str
    """Which document produced it — a standalone `AuthorityPolicy`, or an actor's
    inline block. Not written to the database; it is what makes the conflict message
    name both sides."""


@dataclass(frozen=True, slots=True)
class CompiledTrigger:
    key: str
    actor: str
    cron: str
    timezone: str
    input: dict[str, Any]
    catchup: CatchupPolicy


@dataclass(frozen=True, slots=True)
class CompiledBudget:
    period: str
    oversubscription: float
    organization: int
    departments: dict[str, int]
    actors: dict[str, int]


@dataclass(frozen=True, slots=True)
class CompiledDocument:
    """What lands in `spec_documents` — envelope, source text, and its hash."""

    kind: Kind
    name: str
    source_yaml: str
    compiled: dict[str, Any]
    spec_hash: str


@dataclass(frozen=True, slots=True)
class CompiledOrg:
    organization: str
    display_name: str | None
    departments: tuple[CompiledDepartment, ...] = ()
    roles: tuple[CompiledRole, ...] = ()
    actors: tuple[CompiledActor, ...] = ()
    connections: tuple[CompiledConnection, ...] = ()
    grants: tuple[CompiledGrant, ...] = ()
    policies: tuple[CompiledPolicy, ...] = ()
    triggers: tuple[CompiledTrigger, ...] = ()
    budget: CompiledBudget | None = None
    documents: tuple[CompiledDocument, ...] = ()
    order: tuple[tuple[Kind, str], ...] = ()
    """Topological order of phase 1. The apply walks it, so a create never precedes
    the thing it points at."""
    deferred: tuple[Edge, ...] = ()
    """`(source, field, target)` reference edges, resolved in phase 2 (§7)."""
    model_profiles: dict[str, ModelProfile] = field(default_factory=dict)
    memory_profiles: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def actor(self, name: str) -> CompiledActor | None:
        return next((a for a in self.actors if a.name == name), None)

    @property
    def actor_names(self) -> frozenset[str]:
        return frozenset(a.name for a in self.actors)

    def fingerprint(self) -> str:
        """One hash over the whole org. `spec drift` compares it against the stored
        documents, so a change anywhere — a grant, a cron string — moves it."""
        return canonical_hash([(d.kind.value, d.name, d.spec_hash) for d in self.documents])


# --- the dependency graph ---------------------------------------------------------------

Node = tuple[Kind, str]
Edge = tuple[str, str, str]
"""A deferred reference: `(source label, field, target label)`."""


def _deferred(doc: Document) -> list[Edge]:
    """The **reference columns** — everything phase 2 fills in (§7).

    These are excluded from the topological sort, and the reason is the one §7 gives:
    a department names its head actor and that actor names its department, so no
    ordering of the two exists. Rather than special-casing that one pair, *every*
    nullable reference column is deferred — `actors.role_name`, `actors.department`,
    `actors.reports_to`, `roles.parent_role_id`, a department's head and parent. Phase 1
    writes the rows with those columns empty and phase 2 fills them in, so the document
    set can be in any order and the database is never asked to accept a pointer to a row
    that does not exist yet.

    The consequence for errors is the point of doing it this way: a reporting cycle or
    a self-parenting role is no longer a generic "dependency cycle among 17 documents"
    out of the sort, it is `_check_reporting`'s or `_check_roles`' message naming the
    two documents involved.
    """
    spec = doc.spec
    label = f"{doc.kind.value}/{doc.name}"
    out: list[Edge] = []
    if isinstance(spec, ActorDoc):
        if spec.department:
            out.append((label, "department", f"{Kind.DEPARTMENT.value}/{spec.department}"))
        if spec.role:
            out.append((label, "role", f"{Kind.ROLE.value}/{spec.role}"))
        if spec.reports_to:
            out.append((label, "reportsTo", f"{Kind.ACTOR.value}/{spec.reports_to}"))
    elif isinstance(spec, DepartmentDoc):
        if spec.head:
            out.append((label, "head", f"{Kind.ACTOR.value}/{spec.head}"))
        if spec.parent:
            out.append((label, "parent", f"{Kind.DEPARTMENT.value}/{spec.parent}"))
    elif isinstance(spec, RoleDoc):
        if spec.parent:
            out.append((label, "parent", f"{Kind.ROLE.value}/{spec.parent}"))
    return out


def _dependencies(doc: Document) -> list[Node]:
    """What this document must **exist after**. Reference columns are not here.

    Only edges where the dependent row genuinely cannot be written first: a grant needs
    its subject and its connection to have rows, a trigger needs its actor, a policy
    needs the role it names, and an actor's `ActorSpec` cannot be compiled without the
    model profiles it resolves.
    """
    spec = doc.spec
    out: list[Node] = []
    if isinstance(spec, ActorDoc):
        if spec.memory_profile:
            out.append((Kind.MEMORY_PROFILE, spec.memory_profile))
        out.extend((Kind.MODEL_PROFILE, name) for name in sorted(set(spec.model_profiles.values())))
    elif isinstance(spec, ToolGrantDoc):
        out.append((Kind.ACTOR if spec.subject.actor else Kind.ROLE, spec.subject.subject_id))
        if spec.connection:
            out.append((Kind.CONNECTION, spec.connection))
    elif isinstance(spec, AuthorityPolicyDoc):
        target_kind = {
            "actor": Kind.ACTOR,
            "role": Kind.ROLE,
            "department": Kind.DEPARTMENT,
        }[spec.scope]
        out.append((target_kind, spec.target))
        if spec.approver_role:
            out.append((Kind.ROLE, spec.approver_role))
    elif isinstance(spec, TriggerDoc):
        out.append((Kind.ACTOR, spec.actor))
    elif isinstance(spec, BudgetPolicyDoc):
        out.extend((Kind.DEPARTMENT, name) for name in sorted(spec.departments))
        out.extend((Kind.ACTOR, name) for name in sorted(spec.actors))
    return out


def topological_order(
    documents: DocumentSet,
) -> tuple[tuple[Node, ...], tuple[Edge, ...]]:
    """Kahn's algorithm over the document graph, with the deferred edges removed.

    The ready set is drained in sorted order rather than as a queue, so the output is
    a *canonical* topological order rather than one of many valid ones. Two machines
    that load the same documents produce the same order, therefore the same plan,
    therefore the same `plan_hash` (edge case 76 depends on this).

    Dangling references are not this function's error — `validation` reports them with
    a message about the missing document. Here they are simply not edges, so a set with
    a dangling reference still sorts and the operator gets the better error.
    """
    present = set(documents.by_key)
    incoming: dict[Node, set[Node]] = {key: set() for key in present}
    outgoing: dict[Node, set[Node]] = defaultdict(set)
    for doc in documents:
        for dep in _dependencies(doc):
            if dep not in present:
                continue
            incoming[doc.key].add(dep)
            outgoing[dep].add(doc.key)

    ready = sorted(key for key, deps in incoming.items() if not deps)
    order: list[Node] = []
    remaining = dict(incoming)
    while ready:
        node = ready.pop(0)
        order.append(node)
        del remaining[node]
        newly: list[Node] = []
        for dependent in sorted(outgoing[node]):
            deps = remaining.get(dependent)
            if deps is None:
                continue
            deps.discard(node)
            if not deps:
                newly.append(dependent)
        ready = sorted([*ready, *newly])

    if remaining:
        raise SpecValidationError(
            "dependency cycle among "
            + ", ".join(f"{kind.value}/{name}" for kind, name in sorted(remaining))
            + ". Every reference column is already deferred to phase 2 (§7), so this is "
            "a cycle in what must *exist* first — a grant, a trigger or a policy chain."
        )

    deferred = tuple(edge for doc in documents for edge in _deferred(doc))
    return tuple(order), deferred


# --- compilation -------------------------------------------------------------------------


def compile_org(documents: DocumentSet, *, organization_name: str | None = None) -> CompiledOrg:
    """Turn a validated document set into the values `apply` writes.

    Call `validation.validate` first. This function assumes references resolve; it
    raises `SpecValidationError` where it cannot proceed at all, but it is not the
    place that produces good messages about a missing department.
    """
    order, deferred = topological_order(documents)

    org_docs = documents.of(Kind.ORGANIZATION)
    if len(org_docs) > 1:
        raise SpecValidationError(
            "more than one Organization document: "
            + ", ".join(d.name for d in org_docs)
            + ". One document set describes one organization; multi-tenancy activation "
            "is explicitly out of scope for M4 (§2)."
        )
    org_doc = org_docs[0] if org_docs else None
    if org_doc is None and organization_name is None:
        raise SpecValidationError(
            "no Organization document and no organization name given; one of the two "
            "has to say which organization this is"
        )
    org_name = org_doc.name if org_doc is not None else str(organization_name)
    display = (
        org_doc.spec.display_name
        if org_doc is not None and isinstance(org_doc.spec, OrganizationDoc)
        else None
    )

    model_profiles = {
        doc.name: _model_profile(doc.spec)
        for doc in documents.of(Kind.MODEL_PROFILE)
        if isinstance(doc.spec, ModelProfileDoc)
    }
    memory_profiles = {
        doc.name: doc.spec.scopes
        for doc in documents.of(Kind.MEMORY_PROFILE)
        if isinstance(doc.spec, MemoryProfileDoc)
    }

    departments = tuple(
        CompiledDepartment(
            name=doc.name,
            head=doc.spec.head,
            parent=doc.spec.parent,
            description=doc.spec.description,
        )
        for doc in documents.of(Kind.DEPARTMENT)
        if isinstance(doc.spec, DepartmentDoc)
    )

    roles = tuple(
        CompiledRole(
            name=doc.name,
            rank=doc.spec.rank,
            parent=doc.spec.parent,
            department=doc.spec.department,
            approver=doc.spec.approver,
            approver_daily_budget=doc.spec.approver_daily_budget,
            base_authority=dict(doc.spec.base_authority),
        )
        for doc in documents.of(Kind.ROLE)
        if isinstance(doc.spec, RoleDoc)
    )

    actors = tuple(
        _actor(doc, model_profiles=model_profiles, memory_profiles=memory_profiles)
        for doc in documents.of(Kind.ACTOR)
        if isinstance(doc.spec, ActorDoc)
    )

    connections = tuple(
        CompiledConnection(
            name=doc.name,
            provider=doc.spec.provider,
            scopes=doc.spec.scopes,
            credential_name=doc.spec.credentials.secret_ref if doc.spec.credentials else None,
            status=doc.spec.status,
        )
        for doc in documents.of(Kind.CONNECTION)
        if isinstance(doc.spec, ConnectionDoc)
    )

    grants = tuple(
        CompiledGrant(
            subject_type=doc.spec.subject.subject_type,
            subject_id=doc.spec.subject.subject_id,
            tool=doc.spec.tool,
            connection=doc.spec.connection,
        )
        for doc in documents.of(Kind.TOOL_GRANT)
        if isinstance(doc.spec, ToolGrantDoc)
    )

    policies = _policies(documents)

    triggers = tuple(
        CompiledTrigger(
            key=doc.name,
            actor=doc.spec.actor,
            cron=doc.spec.cron,
            timezone=doc.spec.timezone,
            # `mode` and `trigger` always win over anything in `input`. A trigger whose
            # payload disagreed with its own key would dispatch one graph while the
            # scheduler logged another, and nothing downstream could tell you which.
            input={**doc.spec.input, "mode": doc.spec.mode, "trigger": doc.name},
            catchup=doc.spec.catchup,
        )
        for doc in documents.of(Kind.TRIGGER)
        if isinstance(doc.spec, TriggerDoc)
    )

    budget_docs = documents.of(Kind.BUDGET_POLICY)
    if len(budget_docs) > 1:
        raise SpecValidationError(
            "more than one BudgetPolicy document: "
            + ", ".join(d.name for d in budget_docs)
            + ". The check that matters is children ≤ parent x (1 + oversubscription), "
            "and two documents make that sum something a reviewer assembles by hand."
        )
    budget = (
        _budget(budget_docs[0].spec)
        if budget_docs and isinstance(budget_docs[0].spec, BudgetPolicyDoc)
        else None
    )

    compiled_documents = tuple(
        CompiledDocument(
            kind=doc.kind,
            name=doc.name,
            source_yaml=doc.source_yaml,
            compiled=doc.envelope(),
            spec_hash=canonical_hash(doc.envelope()),
        )
        for doc in documents
    )

    org = CompiledOrg(
        organization=org_name,
        display_name=display,
        departments=departments,
        roles=roles,
        actors=actors,
        connections=connections,
        grants=grants,
        policies=policies,
        triggers=triggers,
        budget=budget,
        documents=compiled_documents,
        order=order,
        deferred=deferred,
        model_profiles=model_profiles,
        memory_profiles=memory_profiles,
    )
    log.info(
        "spec.compiled",
        organization=org_name,
        actors=len(actors),
        roles=len(roles),
        grants=len(grants),
        policies=len(policies),
        fingerprint=org.fingerprint(),
    )
    return org


def _model_profile(doc: ModelProfileDoc) -> ModelProfile:
    return ModelProfile(
        provider=doc.provider,
        model=doc.model,
        max_output_tokens=doc.max_output_tokens,
        temperature=doc.temperature,
        input_cents_per_mtok=doc.input_cents_per_mtok,
        output_cents_per_mtok=doc.output_cents_per_mtok,
        thinking=doc.thinking,
        effort=doc.effort,
        web_search=doc.web_search,
    )


def _actor(
    doc: Document,
    *,
    model_profiles: dict[str, ModelProfile],
    memory_profiles: dict[str, tuple[str, ...]],
) -> CompiledActor:
    spec = doc.spec
    assert isinstance(spec, ActorDoc)

    profiles: dict[WorkClass, ModelProfile] = {}
    for raw_class, profile_name in spec.model_profiles.items():
        try:
            work_class = WorkClass(str(raw_class).lower())
        except ValueError as exc:
            raise SpecValidationError(
                f"{doc.where}: {raw_class!r} is not a work class; expected one of "
                f"{[wc.value for wc in WorkClass]}"
            ) from exc
        profile = model_profiles.get(profile_name)
        if profile is None:
            raise SpecValidationError(
                f"{doc.where}: modelProfiles.{raw_class} names ModelProfile "
                f"{profile_name!r}, which no document defines; known profiles are "
                f"{sorted(model_profiles)}"
            )
        profiles[work_class] = profile

    scope_tokens = (
        spec.memory.scopes
        if spec.memory is not None
        else memory_profiles.get(spec.memory_profile or "", ())
    )
    scopes: list[MemoryScope] = []
    scope_departments: list[str] = []
    for token in scope_tokens:
        scope, qualifier = parse_scope(token, where=doc.where)
        if scope not in scopes:
            scopes.append(scope)
        if qualifier:
            scope_departments.append(qualifier)

    actor_spec = ActorSpec(
        name=doc.name,
        kind=spec.kind,
        graph_ref=spec.graph,
        handler_ref=spec.handler,
        allowed_tools=frozenset(spec.tools),
        ceilings=Ceilings(
            max_llm_calls=spec.ceilings.max_llm_calls,
            max_tool_calls=spec.ceilings.max_tool_calls,
            max_wall_clock_s=spec.ceilings.max_wall_clock_s,
            max_cost_cents=spec.ceilings.max_cost_cents,
            max_depth=spec.ceilings.max_depth,
        ),
        model_profiles=ModelProfiles(profiles=profiles),
        allowed_model_call_sites=(
            frozenset(spec.allowed_model_call_sites)
            if spec.allowed_model_call_sites is not None
            else None
        ),
    )
    return CompiledActor(
        name=doc.name,
        spec=actor_spec,
        # The hash `Registrar.publish_actor` writes, computed the same way. §8's whole
        # claim is that this number is unchanged by the config plane, so it is produced
        # by the same function on the same type rather than by a parallel path.
        spec_hash=canonical_hash(actor_spec),
        uid=doc.metadata.uid,
        department=spec.department,
        role=spec.role,
        reports_to=spec.reports_to,
        memory_scopes=tuple(sorted(scopes, key=lambda s: s.rank)),
        memory_departments=tuple(scope_departments),
        delegation=spec.delegation,
        labels=dict(doc.metadata.labels),
    )


def _policies(documents: DocumentSet) -> tuple[CompiledPolicy, ...]:
    """Standalone `AuthorityPolicy` documents plus actors' inline `authority` blocks.

    A conflict — both sources naming the same (scope, target, action) — is an error
    rather than an override. Two sources for one row is how a policy comes to depend on
    file ordering, and a policy that depends on file ordering is one nobody can review
    from a diff (edge case 85, and §13's first risk).
    """
    out: dict[tuple[str, str, str], CompiledPolicy] = {}

    def add(policy: CompiledPolicy) -> None:
        key = (policy.scope_type, policy.scope_id, policy.action)
        existing = out.get(key)
        if existing is not None:
            raise SpecValidationError(
                f"two documents define authority for {policy.scope_type}:"
                f"{policy.scope_id}/{policy.action} — {existing.source} and "
                f"{policy.source}. Inline actor authority is sugar for an actor-scoped "
                "policy, not an override of one."
            )
        out[key] = policy

    for doc in documents.of(Kind.AUTHORITY_POLICY):
        spec = doc.spec
        if not isinstance(spec, AuthorityPolicyDoc):
            continue
        add(
            CompiledPolicy(
                scope_type=spec.scope,
                scope_id=spec.target,
                action=spec.action,
                level=spec.level,
                approver_role=spec.approver_role,
                max_escalations=spec.max_escalations,
                on_expiry=spec.on_expiry,
                ttl_seconds=spec.ttl_seconds,
                source=doc.where,
            )
        )

    for doc in documents.of(Kind.ACTOR):
        spec = doc.spec
        if not isinstance(spec, ActorDoc) or spec.authority is None:
            continue
        auth = spec.authority
        for action in auth.allowed:
            add(
                CompiledPolicy(
                    scope_type="actor",
                    scope_id=doc.name,
                    action=action,
                    level=AuthorityLevel.AUTO,
                    approver_role=None,
                    max_escalations=0,
                    on_expiry=auth.on_expiry,
                    ttl_seconds=auth.ttl_seconds,
                    source=doc.where,
                )
            )
        for action in auth.denied:
            add(
                CompiledPolicy(
                    scope_type="actor",
                    scope_id=doc.name,
                    action=action,
                    level=AuthorityLevel.DENIED,
                    approver_role=None,
                    max_escalations=0,
                    on_expiry=auth.on_expiry,
                    ttl_seconds=auth.ttl_seconds,
                    source=doc.where,
                )
            )
        for action in auth.approval_required:
            add(
                CompiledPolicy(
                    scope_type="actor",
                    scope_id=doc.name,
                    action=action,
                    level=AuthorityLevel.HUMAN,
                    approver_role=auth.approver_role,
                    max_escalations=auth.max_escalations,
                    on_expiry=auth.on_expiry,
                    ttl_seconds=auth.ttl_seconds,
                    source=doc.where,
                )
            )

    return tuple(sorted(out.values(), key=lambda p: (p.scope_type, p.scope_id, p.action)))


def _budget(spec: Any) -> CompiledBudget:
    assert isinstance(spec, BudgetPolicyDoc)
    return CompiledBudget(
        period=spec.period,
        oversubscription=spec.oversubscription,
        organization=spec.organization,
        departments=dict(sorted(spec.departments.items())),
        actors=dict(sorted(spec.actors.items())),
    )


__all__ = [
    "CompiledActor",
    "CompiledBudget",
    "CompiledConnection",
    "CompiledDepartment",
    "CompiledDocument",
    "CompiledGrant",
    "CompiledOrg",
    "CompiledPolicy",
    "CompiledRole",
    "CompiledTrigger",
    "compile_org",
    "topological_order",
]
