"""The §6 semantic checks.

Structure is the loader's job; this module is about a document set being *coherent*.
Each row of §6's table is a function here and each function has a positive and a
negative test, because an exit criterion that says "every validation has a negative
test" is only meaningful if the validations are separable.

**Every check is cheap and every one of them replaces something expensive.** A
dangling department reference is a `KeyError` in a worker three days from now. A cyclic
escalation policy is two runs waiting on each other with no timeout that resolves it
correctly. A deterministic worker with a model profile is the M1 control actor quietly
becoming a fifth LLM agent, which invalidates the go/no-go numbers rather than failing
anything.

**The escalation check is M2's, imported rather than reimplemented.**
`check_escalation_acyclicity` runs here against a document set the same way it runs at
admission against rows. If the compile-time check and the runtime resolver disagreed
about who a policy governs, one of them would be wrong about something real (M2's
`_subject_roles` note), and the only way to keep them agreeing is for there to be one of
them. `_check_authority` adds the one case M2's row-level version structurally cannot
see: an *actor*-scoped policy, whose subject's role is not in the role rows.

`check_delegation_subset` is deliberately **not** called. It compares two
`ResolvedAuthority` values, and resolution needs live grants and the blast-radius floor
— which is M5's job, at child-spec construction. `_check_delegation` here is the part
answerable from configuration alone, over the reporting edge a delegation will use.

**Registries are a parameter, not a lookup.** Which graphs and tools exist is a fact
about the *process*, and a validator that reached for a global would be untestable and
would report a different answer from a CLI than from a worker. `default_registries()`
is the convenience; the tests pass their own.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from runtime.domain.enums import ActorKind, AuthorityLevel, MemoryScope
from runtime.org.authority import ancestors, check_escalation_acyclicity
from runtime.persistence.repositories.authority import PolicyRow, RoleRow
from runtime.spec.compile import CompiledActor, CompiledOrg
from runtime.spec.errors import SpecValidationError

MAX_DELEGATION_DEPTH = 2
"""M5 §2's *"Out: ... delegation depth > 2"*, as a number the compiler enforces.

Stated here rather than read from `Settings` on purpose. `spec validate` runs in CI's
**static** job, with no database and no environment — that is the whole argument for
the verb existing (M4's deviation table) — and a validation whose answer depended on a
worker's environment variable would pass in CI and fail on apply. `Settings.
delegation_max_depth` is the runtime's own ceiling and defaults to the same value;
`test_m5_delegation` asserts they agree, which is where a check like this belongs.
"""

_ROLE_NAMESPACE_SALT = 0
"""Roles are keyed by name in `RoleRow`, and M2's checks walk `parent_role_id`. The
compiler has no uuids yet, so ids are synthesised deterministically from the name —
see `_role_rows`. The salt exists so that the derivation is stated rather than
implied."""


@dataclass(frozen=True, slots=True)
class Registries:
    """What this process can actually run. See the module docstring."""

    tools: frozenset[str] = frozenset()
    graphs: frozenset[str] = frozenset()
    handlers: frozenset[str] = frozenset()
    tool_actions: Mapping[str, str] = field(default_factory=dict)
    """tool → the authority action it performs, from `ToolDef.authority_action`."""


def default_registries(settings: object = None) -> Registries:
    """The registries of a fully-imported runtime.

    Imports the graph and handler packages for their registration side effects, which
    is the same thing `department_boot.assert_registered` relies on. Deliberately done
    here rather than at module import: `runtime.spec` is imported by the CLI for
    commands that never need a registry, and importing every graph to run `spec show`
    would be a slow no-op.
    """
    import runtime.graphs.department
    import runtime.handlers  # noqa: F401  registers analytics@1, hasher@1
    from runtime.gateway.builtin import build_registry
    from runtime.graphs.registry import known_graphs
    from runtime.handlers.registry import known_handlers
    from runtime.persistence.uow import UnitOfWorkFactory
    from runtime.settings import Settings

    resolved = settings if isinstance(settings, Settings) else None
    registry = build_registry(UnitOfWorkFactory(resolved), resolved)
    return Registries(
        tools=registry.names(),
        graphs=known_graphs(),
        handlers=known_handlers(),
        tool_actions={
            name: action
            for name in registry.names()
            if (action := registry.get(name).definition.authority_action) is not None
        },
    )


# --- the entry point ------------------------------------------------------------------


def validate(org: CompiledOrg, *, registries: Registries | None = None) -> None:
    """Run every §6 check. Raises `SpecValidationError` on the first failure.

    First failure rather than a collected report, deliberately. The checks are ordered
    from most-local to most-global, so the first failure is almost always the *cause*
    and the rest would be its consequences — a dangling department produces a bad
    reporting graph, a bad budget sum and a scope the actor may not hold, and printing
    four errors for one typo is how an operator learns to skim them.
    """
    known = registries or Registries()

    _check_departments(org)
    _check_roles(org)
    _check_reporting(org)
    _check_entrypoints(org, known)
    _check_tools(org, known)
    _check_connections(org)
    _check_grants(org)
    _check_memory_scopes(org)
    _check_deterministic(org)
    _check_hybrid(org)
    _check_authority(org, known)
    _check_delegation(org)
    _check_budget(org)
    _check_triggers(org)


# --- structure ---------------------------------------------------------------------------


def _check_departments(org: CompiledOrg) -> None:
    """Referenced department exists; department heads and parents resolve."""
    known = {d.name for d in org.departments}
    for actor in org.actors:
        if actor.department and actor.department not in known:
            raise SpecValidationError(
                f"Actor/{actor.name}: department {actor.department!r} has no Department "
                f"document; known departments are {sorted(known) or '(none)'}"
            )
    for role in org.roles:
        if role.department and role.department not in known:
            raise SpecValidationError(
                f"Role/{role.name}: department {role.department!r} has no Department document"
            )
    actors = org.actor_names
    for dept in org.departments:
        if dept.head and dept.head not in actors:
            raise SpecValidationError(
                f"Department/{dept.name}: head {dept.head!r} is not an Actor in this document set"
            )
        if dept.parent and dept.parent not in known:
            raise SpecValidationError(
                f"Department/{dept.name}: parent {dept.parent!r} has no Department document"
            )
        if dept.parent == dept.name:
            raise SpecValidationError(f"Department/{dept.name} is its own parent")

    # The department tree, walked. `parent` is a phase-2 reference column and therefore
    # deferred out of the topological sort, so a cycle in it reaches here rather than
    # coming out of the sort as an unhelpful list of every document involved.
    by_name = {d.name: d for d in org.departments}
    for dept in org.departments:
        seen = [dept.name]
        current = dept
        while current.parent is not None:
            if current.parent in seen:
                raise SpecValidationError(
                    "department tree contains a cycle: " + " -> ".join([*seen, current.parent])
                )
            seen.append(current.parent)
            current = by_name[current.parent]


def _check_roles(org: CompiledOrg) -> None:
    """Every referenced role exists, and no role is its own parent."""
    known = {r.name for r in org.roles}
    for role in org.roles:
        if role.parent == role.name:
            raise SpecValidationError(f"Role/{role.name} is its own parent")
        if role.parent and role.parent not in known:
            raise SpecValidationError(
                f"Role/{role.name}: parent {role.parent!r} has no Role document"
            )
    for actor in org.actors:
        if actor.role and actor.role not in known:
            raise SpecValidationError(
                f"Actor/{actor.name}: role {actor.role!r} has no Role document; known "
                f"roles are {sorted(known) or '(none)'}"
            )


def _check_reporting(org: CompiledOrg) -> None:
    """`reportsTo` resolves, is not self, and the reporting graph is acyclic.

    A cycle here is an infinite escalation: A reports to B reports to A, and anything
    that walks the chain to find "who does this actor answer to" runs forever. It costs
    a walk to find at compile time.
    """
    known = org.actor_names
    by_name = {a.name: a for a in org.actors}
    for actor in org.actors:
        if actor.reports_to is None:
            continue
        if actor.reports_to == actor.name:
            raise SpecValidationError(f"Actor/{actor.name} reports to itself")
        if actor.reports_to not in known:
            raise SpecValidationError(
                f"Actor/{actor.name}: reportsTo {actor.reports_to!r} is not an Actor in "
                "this document set"
            )

    for actor in org.actors:
        seen = [actor.name]
        current: CompiledActor | None = actor
        while current is not None and current.reports_to is not None:
            if current.reports_to in seen:
                raise SpecValidationError(
                    "reporting graph contains a cycle: "
                    + " -> ".join([*seen, current.reports_to])
                    + ". Escalation walks this chain, so a cycle is a run that never "
                    "finds an approver."
                )
            seen.append(current.reports_to)
            current = by_name.get(current.reports_to)


def _check_entrypoints(org: CompiledOrg, known: Registries) -> None:
    """Graph and handler references exist in the registry.

    Moves a runtime failure to compile time. A worker that starts without a graph an
    actor's spec names does not find out until a cron fires days later, and by then it
    looks like a scheduling problem (`graphs.department.assert_registered`, said again
    from the other side).
    """
    if not known.graphs and not known.handlers:
        return  # No registry given: the caller is checking structure only.
    for actor in org.actors:
        spec = actor.spec
        if spec.graph_ref and spec.graph_ref not in known.graphs:
            raise SpecValidationError(
                f"Actor/{actor.name}: graph {spec.graph_ref!r} is not registered; known "
                f"graphs are {sorted(known.graphs)}"
            )
        if spec.handler_ref and spec.handler_ref not in known.handlers:
            raise SpecValidationError(
                f"Actor/{actor.name}: handler {spec.handler_ref!r} is not registered; "
                f"known handlers are {sorted(known.handlers)}"
            )


def _check_tools(org: CompiledOrg, known: Registries) -> None:
    """Tool references exist *at the pinned version*.

    The pinned version is the point. `web.search@1` and `web.search@2` are different
    tools with different argument models, and a reference that resolved by name would
    turn a tool upgrade into a silent behaviour change in every actor holding it.
    """
    if not known.tools:
        return
    for actor in org.actors:
        for tool in sorted(actor.spec.allowed_tools):
            if tool not in known.tools:
                raise SpecValidationError(
                    f"Actor/{actor.name}: tool {tool!r} is not registered at that "
                    f"version; registered tools are {sorted(known.tools)}"
                )
    for grant in org.grants:
        if grant.tool not in known.tools:
            raise SpecValidationError(
                f"ToolGrant for {grant.subject_type}:{grant.subject_id}: tool "
                f"{grant.tool!r} is not registered at that version"
            )


def _check_connections(org: CompiledOrg) -> None:
    known = {c.name for c in org.connections}
    for grant in org.grants:
        if grant.connection and grant.connection not in known:
            raise SpecValidationError(
                f"ToolGrant {grant.tool} for {grant.subject_type}:{grant.subject_id}: "
                f"connection {grant.connection!r} has no Connection document"
            )


def _check_grants(org: CompiledOrg) -> None:
    """Grant subjects exist, and an actor's `tools` agree with its grants.

    The second half is M2's rule said at compile time. `allowed_tools` and
    `tool_grants` answer different questions — admitted-to versus may-now — and
    `governance_seed` already says *"disagreement between them is a bug, so they are
    written from the same table"*. In YAML they are written in two documents, so the
    check that they agree has to be here or it is nowhere.

    Only one direction is an error. A grant for a tool the actor's spec does not list
    is dead but harmless: the spec check refuses the call first. A tool in the spec with
    no grant is a call that passes admission and is refused at the gateway, which reads
    in the denial stream as a mis-scoped actor and is exactly the confusing case §9's
    review is trying to eliminate.
    """
    actors = org.actor_names
    roles = {r.name for r in org.roles}
    for grant in org.grants:
        pool = actors if grant.subject_type == "actor" else roles
        if grant.subject_id not in pool:
            raise SpecValidationError(
                f"ToolGrant {grant.tool}: {grant.subject_type} {grant.subject_id!r} is "
                "not defined in this document set"
            )

    granted: dict[str, set[str]] = {a.name: set() for a in org.actors}
    role_of = {a.name: a.role for a in org.actors}
    for grant in org.grants:
        if grant.subject_type == "actor":
            granted.setdefault(grant.subject_id, set()).add(grant.tool)
        else:
            for actor_name, role in role_of.items():
                if role == grant.subject_id:
                    granted[actor_name].add(grant.tool)

    for actor in org.actors:
        missing = sorted(actor.spec.allowed_tools - granted.get(actor.name, set()))
        if missing:
            raise SpecValidationError(
                f"Actor/{actor.name}: tool(s) {missing} are in `tools` but no ToolGrant "
                "document grants them. The spec says what the actor was admitted to and "
                "the grant says what it may do now; a call allowed by one and refused by "
                "the other reads in the denial stream as a mis-scoped actor."
            )

    for actor in org.actors:
        wants_search = any(p.web_search for p in actor.spec.model_profiles.profiles.values())
        if wants_search and not any(
            tool.startswith("web.search@") for tool in granted.get(actor.name, set())
        ):
            raise SpecValidationError(
                f"Actor/{actor.name}: a model profile asks for provider-side webSearch, "
                "but the actor holds no web.search grant. Provider-side search never "
                "reaches the tool gateway — no journal row, no rate limit, no approval — "
                "so it may not reach further than the front door would."
            )


def _check_memory_scopes(org: CompiledOrg) -> None:
    """Memory scopes exist and the actor may hold them (edge case 81).

    Scope escalation by config edit is the failure this prevents: naming
    `dept:finance` from a marketing actor would, if it were honoured, widen a retrieval
    filter past the isolation rule `domain.memory.readable_scopes` enforces. The
    retrieval path already intersects rather than unions, so this could not actually
    widen anything — which is precisely why it must be refused *here*, loudly, rather
    than silently dropped at retrieval time where nobody would ever see it.
    """
    departments = {d.name for d in org.departments}
    for actor in org.actors:
        for named in actor.memory_departments:
            if named not in departments:
                raise SpecValidationError(
                    f"Actor/{actor.name}: memory scope `dept:{named}` names a department "
                    "with no Department document"
                )
            if named != actor.department:
                raise SpecValidationError(
                    f"Actor/{actor.name}: memory scope `dept:{named}` is not this "
                    f"actor's department ({actor.department or 'none'}). An actor reads "
                    "its own department and never sideways into a sibling."
                )
        if MemoryScope.DEPARTMENT in actor.memory_scopes and not actor.department:
            raise SpecValidationError(
                f"Actor/{actor.name}: asks for the department memory scope but belongs "
                "to no department"
            )


def _check_deterministic(org: CompiledOrg) -> None:
    """A deterministic worker has no model profiles and a zero LLM ceiling.

    `ActorDoc` already refuses both, and this says it again over the *compiled* spec.
    That is not redundancy for its own sake: the compiled spec is what gets hashed and
    published, and the `max_llm_calls = 0` guarantee is the one property the M1 control
    numbers rest on. It is worth two checks.
    """
    for actor in org.actors:
        if actor.spec.kind is not ActorKind.DETERMINISTIC_WORKER:
            continue
        if actor.spec.ceilings.max_llm_calls != 0:
            raise SpecValidationError(
                f"Actor/{actor.name}: a deterministic worker must have "
                "ceilings.maxLlmCalls == 0 — the gateway refusal is what makes it "
                "deterministic"
            )
        if actor.spec.model_profiles.profiles:
            raise SpecValidationError(
                f"Actor/{actor.name}: a deterministic worker may not hold model "
                "profiles; the gateway would refuse every call they describe"
            )


def _check_hybrid(org: CompiledOrg) -> None:
    """`HYBRID` declared → refuse (§6, and `compile_actor_spec`'s NotImplementedError).

    Refusing to compile is a stronger guarantee than a runtime check a future edit
    could route around, and the config plane must not become the route around it.
    """
    for actor in org.actors:
        if actor.spec.kind is ActorKind.HYBRID:
            raise SpecValidationError(
                f"Actor/{actor.name}: kind hybrid is not implemented. A HYBRID actor "
                "needs a mandatory allowedModelCallSites that the ModelGateway enforces "
                "per call site; until that exists it cannot be compiled."
            )


# --- authority --------------------------------------------------------------------------


def _role_rows(org: CompiledOrg) -> list[RoleRow]:
    """Compiled roles as the rows M2's checks read.

    Ids are derived from the name with `uuid5`, so the same document set produces the
    same rows every time and the parent pointers line up without a database.
    """
    import uuid

    def role_id(name: str) -> uuid.UUID:
        return uuid.uuid5(uuid.NAMESPACE_URL, f"spec-role:{_ROLE_NAMESPACE_SALT}:{name}")

    return [
        RoleRow(
            id=role_id(role.name),
            name=role.name,
            department=role.department,
            parent_role_id=role_id(role.parent) if role.parent else None,
            rank=role.rank,
            approver=role.approver,
            approver_daily_budget=role.approver_daily_budget,
            base_authority=dict(role.base_authority),
        )
        for role in org.roles
    ]


def _policy_rows(org: CompiledOrg) -> list[PolicyRow]:
    return [
        PolicyRow(
            scope_type=p.scope_type,
            scope_id=p.scope_id,
            action=p.action,
            level=p.level,
            approver_role=p.approver_role,
            max_escalations=p.max_escalations,
            on_expiry=p.on_expiry,
            ttl_seconds=p.ttl_seconds,
        )
        for p in org.policies
    ]


def _check_authority(org: CompiledOrg, known: Registries) -> None:
    """The escalation graph is acyclic and every approver is strictly up-hierarchy.

    M2 T32, re-run against the *new configuration* rather than only at runtime — which
    is §6's point about this row. At runtime the check protects the runs that already
    exist; here it protects the ones the apply is about to make possible, and the two
    are not the same set.

    An **actor**-scoped policy is checked here in a way M2's row-level check cannot:
    `_subject_roles` returns nothing for an actor scope because the actor's role is not
    in the role rows. The compiler knows it, so the check is done directly.
    """
    roles = _role_rows(org)
    policies = _policy_rows(org)
    check_escalation_acyclicity(roles, policies)

    by_name = {r.name: r for r in roles}
    role_of = {a.name: a.role for a in org.actors}
    for policy in org.policies:
        if policy.scope_type != "actor" or not policy.approver_role:
            continue
        subject_role = role_of.get(policy.scope_id)
        if subject_role is None:
            raise SpecValidationError(
                f"authority for actor:{policy.scope_id}/{policy.action} names approver "
                f"{policy.approver_role!r}, but the actor holds no role, so there is no "
                "hierarchy to escalate through"
            )
        if subject_role == policy.approver_role:
            raise SpecValidationError(
                f"authority for actor:{policy.scope_id}/{policy.action}: role "
                f"{subject_role!r} approves for itself"
            )
        if policy.approver_role not in ancestors(subject_role, by_name):
            raise SpecValidationError(
                f"authority for actor:{policy.scope_id}/{policy.action}: approver "
                f"{policy.approver_role!r} is not up-hierarchy from {subject_role!r}. "
                "Approval must always travel upwards; anything else admits a cycle."
            )

    if known.tool_actions:
        actions = set(known.tool_actions.values())
        for policy in org.policies:
            if policy.action not in actions:
                # Not an error: an action may be gated before the tool that performs it
                # is registered, and refusing that would make the safe ordering — policy
                # first, then tool — the one the validator rejects.
                continue
            if policy.level is AuthorityLevel.AUTO and policy.approver_role:
                raise SpecValidationError(
                    f"authority for {policy.scope_type}:{policy.scope_id}/{policy.action} "
                    "is `auto` but names an approver. One of the two is wrong, and the "
                    "blast-radius floor will overrule whichever it is."
                )


def _check_delegation(org: CompiledOrg) -> None:
    """Delegation child authority ⊆ parent authority, checked statically (v3 §10).

    Static means "over the reporting edge", which is where a delegation will run when
    M5 turns it on: an actor may delegate to somebody who reports to it, and a child
    that could do something its parent cannot is `DelegationWidening` waiting to
    happen. Actual `ResolvedAuthority` subset checking is M5's, at spec-construction
    time, against resolved values; this is the part answerable from config alone.
    """
    by_name = {a.name: a for a in org.actors}
    levels = {AuthorityLevel.DENIED: 0, AuthorityLevel.HUMAN: 1, AuthorityLevel.AUTO: 2}
    actor_actions: dict[str, dict[str, AuthorityLevel]] = {a.name: {} for a in org.actors}
    for policy in org.policies:
        if policy.scope_type == "actor" and policy.scope_id in actor_actions:
            actor_actions[policy.scope_id][policy.action] = policy.level

    for actor in org.actors:
        if actor.delegation is None or not actor.delegation.enabled:
            continue
        if actor.delegation.max_depth == 0 or actor.delegation.max_children == 0:
            raise SpecValidationError(
                f"Actor/{actor.name}: delegation is enabled with maxDepth "
                f"{actor.delegation.max_depth} and maxChildren "
                f"{actor.delegation.max_children}; one of them being zero means it can "
                "never delegate, which is what `enabled: false` says more clearly"
            )
        # M5. Two shape rules the runtime would otherwise discover one refused
        # delegation at a time, in production, weeks later.
        if actor.delegation.max_live_descendants < actor.delegation.max_children:
            raise SpecValidationError(
                f"Actor/{actor.name}: maxLiveDescendants "
                f"{actor.delegation.max_live_descendants} is below maxChildren "
                f"{actor.delegation.max_children}. The descendant limit counts direct "
                "children too, so this configuration refuses the last child it just "
                "said it would allow — and the refusal names the wrong limit."
            )
        if actor.delegation.max_depth > MAX_DELEGATION_DEPTH:
            raise SpecValidationError(
                f"Actor/{actor.name}: maxDepth {actor.delegation.max_depth} is past "
                f"the runtime ceiling of {MAX_DELEGATION_DEPTH}. M5 §2 puts delegation "
                "deeper than two generations out of scope; the runtime would clamp "
                "this at admission and the document would then describe something that "
                "does not happen."
            )
        children = [c for c in org.actors if c.reports_to == actor.name]
        for child in children:
            for action, level in actor_actions.get(child.name, {}).items():
                parent_level = actor_actions.get(actor.name, {}).get(action)
                if parent_level is None:
                    continue
                if levels[level] > levels[parent_level]:
                    raise SpecValidationError(
                        f"Actor/{child.name} would hold {action!r} at {level.value} "
                        f"while its parent {actor.name!r} holds it at "
                        f"{parent_level.value}. A child's authority must be a subset of "
                        "its parent's (v3 §10)."
                    )
            parent_tools = set(by_name[actor.name].spec.allowed_tools)
            extra = sorted(set(child.spec.allowed_tools) - parent_tools)
            if extra:
                raise SpecValidationError(
                    f"Actor/{child.name} holds tool(s) {extra} that its delegating "
                    f"parent {actor.name!r} does not. Delegation cannot widen."
                )


# --- budget and schedule ------------------------------------------------------------------


def _check_budget(org: CompiledOrg) -> None:
    """Children <= parent x (1 + oversubscription), at every level.

    Catches an impossible org before it half-runs. Without it the failure is a
    department that admits runs all month and stops in the third week because the org
    pool it hangs off was never large enough to cover its children — which looks
    exactly like a spend spike and is not one.
    """
    budget = org.budget
    if budget is None:
        return
    departments = {d.name for d in org.departments}
    actors = org.actor_names
    for name in budget.departments:
        if name not in departments:
            raise SpecValidationError(
                f"BudgetPolicy: departments.{name} has no Department document"
            )
    for name in budget.actors:
        if name not in actors:
            raise SpecValidationError(f"BudgetPolicy: actors.{name} has no Actor document")

    ceiling = budget.organization * (1 + budget.oversubscription)
    total_departments = sum(budget.departments.values())
    if total_departments > ceiling:
        raise SpecValidationError(
            f"BudgetPolicy: department limits sum to {total_departments}c against an "
            f"organization limit of {budget.organization}c "
            f"(x{1 + budget.oversubscription:.2f} = {ceiling:.0f}c). The organization "
            "would run out before its departments did."
        )

    department_of = {a.name: a.department for a in org.actors}
    per_department: dict[str | None, int] = {}
    for actor_name, cents in budget.actors.items():
        key = department_of.get(actor_name)
        per_department[key] = per_department.get(key, 0) + cents
    for department, total in sorted(per_department.items(), key=lambda kv: kv[0] or ""):
        parent = (
            budget.departments.get(department) if department is not None else budget.organization
        )
        if parent is None:
            raise SpecValidationError(
                f"BudgetPolicy: actors in department {department!r} have limits but the "
                "department has none"
            )
        allowed = parent * (1 + budget.oversubscription)
        if total > allowed:
            where = f"department {department}" if department else "the organization"
            raise SpecValidationError(
                f"BudgetPolicy: actor limits in {where} sum to {total}c against a parent "
                f"limit of {parent}c (x{1 + budget.oversubscription:.2f} = {allowed:.0f}c)"
            )


def _check_triggers(org: CompiledOrg) -> None:
    """Trigger actors exist and cron strings parse.

    Parsing here, before anything is written, for the reason `install_triggers` gives:
    a malformed schedule should be a refused apply rather than a trigger that silently
    never fires.
    """
    from runtime.runtime.scheduler import parse_cron

    actors = org.actor_names
    for trigger in org.triggers:
        if trigger.actor not in actors:
            raise SpecValidationError(
                f"Trigger/{trigger.key}: actor {trigger.actor!r} is not an Actor in this "
                "document set"
            )
        try:
            parse_cron(trigger.cron)
        except Exception as exc:
            raise SpecValidationError(
                f"Trigger/{trigger.key}: cron {trigger.cron!r} does not parse: {exc}"
            ) from exc


__all__ = ["Registries", "default_registries", "validate"]
