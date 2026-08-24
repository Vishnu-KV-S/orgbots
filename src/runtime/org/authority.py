"""Authority — a resolver, as of M2.

M1's version of this module was a frozen dict with one entry and a comment
explaining why building the resolver early would answer a question nobody had asked.
The question has now been asked: M2 points the runtime at things that matter, and one
gate on one action is not a model of who may do what.

**The resolution chain** (§3), evaluated once at `start_run()` and frozen into the
RunSpec:

    role.base_authority                    the role's own defaults
      ← policy(scope=role)                 overrides for everyone in the role
      ← policy(scope=department)           overrides for a department
      ← policy(scope=actor)                overrides for this actor
      ← blast-radius floor per tool        a tool may tighten, never loosen
      = ResolvedAuthority, hashed into spec_hash

Later steps overlay earlier ones, and the floor is last because I14 is not
negotiable by configuration: a policy row saying an irreversible publish is `auto`
loses to the floor, every time. Waiving it is a code change to the registry's
allow-list — a reviewed act with a diff — not a row somebody can insert.

**What the gateway reads.** The gateway reads the frozen `ResolvedAuthority` from the
RunSpec and never this module's live tables. A revoked grant is caught by the ≤30s
permission cache re-check (edge case 64), not by mutating a running spec — because a
spec that changes under a run makes "what was this allowed to do" unanswerable at
exactly the moment somebody needs the answer.

**Two compile-time checks**, run when a spec is applied. Both are graph walks and
both are free; the alternative for the first one is a runtime deadlock.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass

from runtime.domain.authority import (
    BLAST_RADIUS_FLOOR,
    ActionAuthority,
    ResolvedAuthority,
    apply_floor,
)
from runtime.domain.enums import AuthorityLevel, BlastRadius, OnExpiry
from runtime.domain.errors import DelegationWidening, EscalationCycle
from runtime.domain.ids import OrganizationId
from runtime.observability.logging import get_logger
from runtime.persistence.repositories.authority import ActorIdentity, PolicyRow, RoleRow
from runtime.persistence.uow import UnitOfWorkFactory

log = get_logger("org.authority")

DEFAULT_APPROVAL_TTL_SECONDS = 24 * 60 * 60
"""24h, carried over from M1. A policy row overrides it per action."""

AUTHORITY_CACHE_TTL_SECONDS = 30.0
"""How stale a *resolution* may be.

Bounded by the same 30s as the permission cache, and for the same reason (edge case
64): a policy change must take effect inside half a minute, and re-reading four
tables on every `start_run()` is a round trip the §9 latency budget would notice.
Runs already admitted keep their frozen authority regardless — this bounds how long a
policy change takes to reach *new* runs.
"""

_SCOPE_PRECEDENCE = ("role", "department", "actor")
"""Later scopes overlay earlier ones. Not alphabetical, not the table's order —
the resolution chain in §3, spelled out so nothing depends on a dict's iteration
order for a security decision."""


# --- building a ResolvedAuthority ---------------------------------------------------


def _role_index(roles: list[RoleRow]) -> dict[str, RoleRow]:
    return {r.name: r for r in roles}


def ancestors(role: str, roles: Mapping[str, RoleRow]) -> list[str]:
    """Role names strictly above `role`, nearest first. Cycle-safe.

    A cycle here would hang the escalation walk, so the visited set is not
    defensiveness — `check_escalation_acyclicity` is what *prevents* one, and this is
    what stops a database somebody edited by hand from taking a worker with it.
    """
    out: list[str] = []
    seen = {role}
    current = roles.get(role)
    while current is not None and current.parent_role_id is not None:
        parent = next((r for r in roles.values() if r.id == current.parent_role_id), None)
        if parent is None or parent.name in seen:
            break
        out.append(parent.name)
        seen.add(parent.name)
        current = parent
    return out


def approver_chain(approver_role: str, roles: Mapping[str, RoleRow]) -> tuple[str, ...]:
    """Who answers, in escalation order, starting at `approver_role`.

    A role with no `approver` is skipped rather than producing an empty slot: some
    roles exist to be escalated *through* — a department head who is out of scope for
    this action but sits between two people who are not.
    """
    chain: list[str] = []
    for name in [approver_role, *ancestors(approver_role, roles)]:
        role = roles.get(name)
        if role is not None and role.approver and role.approver not in chain:
            chain.append(role.approver)
    return tuple(chain)


def _from_policy(policy: PolicyRow, roles: Mapping[str, RoleRow], source: str) -> ActionAuthority:
    chain = approver_chain(policy.approver_role, roles) if policy.approver_role else ()
    # A policy may permit more escalations than its chain can absorb — an operator
    # typo, or a role that lost its approver. Clamp rather than refuse: the approval
    # still works, it just runs out of people sooner, and `ActionAuthority` would
    # otherwise reject the whole spec over a recoverable mismatch.
    max_esc = min(policy.max_escalations, max(0, len(chain) - 1))
    return ActionAuthority(
        action=policy.action,
        level=policy.level,
        approver_chain=chain,
        max_escalations=max_esc,
        ttl_seconds=policy.ttl_seconds,
        on_expiry=policy.on_expiry,
        source=source,
    )


def _from_base(
    action: str, spec: object, roles: Mapping[str, RoleRow], role_name: str
) -> ActionAuthority | None:
    """Parse one entry of a role's `base_authority` JSON.

    Two spellings are accepted, because the short one is what an operator writes and
    the long one is what a policy needs: `{"publish_external": "human"}` and
    `{"publish_external": {"level": "human", "approver_role": "director", …}}`.
    """
    if isinstance(spec, str):
        level = AuthorityLevel(spec)
        chain = approver_chain(role_name, roles) if level is AuthorityLevel.HUMAN else ()
        return ActionAuthority(
            action=action,
            level=level,
            approver_chain=chain,
            ttl_seconds=DEFAULT_APPROVAL_TTL_SECONDS,
            source=f"role:{role_name}",
        )
    if isinstance(spec, dict):
        level = AuthorityLevel(spec.get("level", "denied"))
        approver_role = spec.get("approver_role") or role_name
        chain = approver_chain(str(approver_role), roles) if level is AuthorityLevel.HUMAN else ()
        return ActionAuthority(
            action=action,
            level=level,
            approver_chain=chain,
            max_escalations=min(int(spec.get("max_escalations", 0)), max(0, len(chain) - 1)),
            ttl_seconds=int(spec.get("ttl_seconds", DEFAULT_APPROVAL_TTL_SECONDS)),
            on_expiry=OnExpiry(spec.get("on_expiry", "deny")),
            source=f"role:{role_name}",
        )
    return None


def _governs(policy: PolicyRow, role: str | None, roles: Mapping[str, RoleRow]) -> bool:
    """Does `policy` apply to an actor in `role`?

    The counterpart of `_subject_roles`, and the two must agree: approval travels
    upwards, so a policy naming an approver applies only to roles strictly below that
    approver. An actor in the approver's own role — or above it — is not somebody the
    approver approves for.

    A policy with no `approver_role` governs everyone in scope, because there is no
    approver for the actor to be at or above. That is the `auto` and `denied` case,
    where the question does not arise.
    """
    if not policy.approver_role:
        return True
    if role is None:
        return False
    return policy.approver_role in ancestors(role, roles)


def build_authority(
    identity: ActorIdentity,
    *,
    action_floors: Mapping[str, BlastRadius] | None = None,
    now: float | None = None,
) -> ResolvedAuthority:
    """Run the resolution chain. Pure: rows in, value out.

    `action_floors` maps an authority action to the widest blast radius of any
    registered tool that performs it, and comes from the tool registry — which is why
    it is a parameter rather than a lookup. The registry lives above this layer, and a
    resolver that reached up for it would invert the stack the same way a gateway
    reaching up for policy would.
    """
    roles = _role_index(identity.roles)
    actions: dict[str, ActionAuthority] = {}

    # 1. The role's own defaults.
    if identity.role and identity.role in roles:
        for action, spec in roles[identity.role].base_authority.items():
            entry = _from_base(action, spec, roles, identity.role)
            if entry is not None:
                actions[action] = entry

    # 2-4. Policy overlays, in precedence order. Each later scope wins outright for
    # the actions it names; it does not merge field-by-field, because a half-applied
    # policy is a rule nobody wrote.
    scope_values = {
        "role": identity.role,
        "department": identity.department,
        "actor": identity.actor_name,
    }
    for scope_type in _SCOPE_PRECEDENCE:
        wanted = scope_values.get(scope_type)
        if not wanted:
            continue
        for policy in identity.policies:
            if policy.scope_type != scope_type or policy.scope_id != wanted:
                continue
            if not _governs(policy, identity.role, roles):
                # This actor is at or above the policy's approver. The policy is about
                # people the approver approves *for*, and that is not this actor — see
                # `_subject_roles`. Skipping leaves no entry, so the blast-radius floor
                # denies rather than routing an approval to the requester.
                continue
            actions[policy.action] = _from_policy(policy, roles, f"{scope_type}:{wanted}")

    # 5. The blast-radius floor. Applied to every action a registered tool performs,
    # including ones no policy mentions — otherwise an irreversible tool whose action
    # nobody wrote a row for would resolve to the default and be denied for the wrong
    # reason, which reads in the denial stream as a mis-scoped actor rather than as a
    # missing policy.
    for action, radius in (action_floors or {}).items():
        floor = BLAST_RADIUS_FLOOR[radius]
        current = actions.get(action)
        if current is None:
            if floor is AuthorityLevel.AUTO:
                continue
            # There is a floor but no policy and therefore no approver. Deny: an
            # approval with nobody to answer it is a deadlock, and this is the one
            # place the default-deny is load-bearing rather than merely correct.
            actions[action] = ActionAuthority(
                action=action,
                level=AuthorityLevel.DENIED,
                source="blast_radius_floor:no_policy",
                ttl_seconds=DEFAULT_APPROVAL_TTL_SECONDS,
            )
            continue
        actions[action] = apply_floor(current, floor)

    moment = now if now is not None else time.time()
    live_grants = [
        g
        for g in identity.grants
        if g.revoked_at is None and (g.expires_at is None or g.expires_at.timestamp() > moment)
    ]
    # An actor-scoped grant beats a role-scoped one for the same tool. Sorting rather
    # than branching means the later assignment wins, and the sort key states which
    # one that is instead of leaving it to dict iteration order.
    live_grants.sort(key=lambda g: 0 if g.subject_type == "role" else 1)

    tool_connections = {g.tool: g.connection_name for g in live_grants if g.connection_name}
    connection_credentials = {
        g.connection_name: g.credential_name
        for g in live_grants
        if g.connection_name and g.credential_name
    }

    return ResolvedAuthority(
        actor_name=identity.actor_name,
        role=identity.role,
        department=identity.department,
        actions=actions,
        tool_grants=frozenset(g.tool for g in live_grants),
        connections=frozenset(tool_connections.values()),
        tool_connections=tool_connections,
        connection_credentials=connection_credentials,
        approver_budgets={
            r.approver: r.approver_daily_budget for r in identity.roles if r.approver
        },
    )


# --- the two compile-time checks (§3) -----------------------------------------------


def check_escalation_acyclicity(roles: list[RoleRow], policies: list[PolicyRow]) -> None:
    """Reject any policy set where approval could go round in a circle.

    v3 edge case 30. Three things are checked and the third is the one the edge case
    names:

    1. **The role tree is a tree.** A cycle in `parent_role_id` makes the escalation
       walk non-terminating, so it is caught before anything walks it.
    2. **Rank agrees with the tree.** `rank` is advisory — the parent pointer is
       authoritative — but two columns that can disagree will, and a dashboard sorted
       by rank that contradicts the escalation order is a way to be confidently wrong.
    3. **Every approver is strictly up-hierarchy from its requester.** This is the
       one that stops "A approves for B and B approves for A", directly or
       transitively, because "strictly above in a tree" is antisymmetric by
       construction. Catching it here is free; catching it at runtime is two runs
       waiting on each other with no timeout that resolves it correctly.
    """
    by_name = _role_index(roles)
    by_id = {r.id: r for r in roles}

    for role in roles:
        seen = {role.name}
        current = role
        while current.parent_role_id is not None:
            parent = by_id.get(current.parent_role_id)
            if parent is None:
                break
            if parent.name in seen:
                raise EscalationCycle(
                    f"role hierarchy contains a cycle through {parent.name!r}: "
                    f"{' -> '.join([*seen, parent.name])}"
                )
            if parent.rank >= current.rank:
                raise EscalationCycle(
                    f"role {current.name!r} (rank {current.rank}) has parent "
                    f"{parent.name!r} with rank {parent.rank}; a parent must outrank "
                    "its child or the tree and the ranks disagree about who is senior"
                )
            seen.add(parent.name)
            current = parent

    for policy in policies:
        if not policy.approver_role:
            continue
        if policy.approver_role not in by_name:
            raise EscalationCycle(
                f"policy {policy.scope_type}:{policy.scope_id}/{policy.action} names "
                f"approver role {policy.approver_role!r}, which does not exist"
            )
        for subject in _subject_roles(policy, roles):
            if subject == policy.approver_role:
                raise EscalationCycle(
                    f"policy {policy.scope_type}:{policy.scope_id}/{policy.action}: "
                    f"role {subject!r} approves for itself"
                )
            if policy.approver_role not in ancestors(subject, by_name):
                raise EscalationCycle(
                    f"policy {policy.scope_type}:{policy.scope_id}/{policy.action}: "
                    f"approver {policy.approver_role!r} is not up-hierarchy from "
                    f"{subject!r}. Approval must always travel upwards; anything else "
                    "admits a cycle."
                )


def _subject_roles(policy: PolicyRow, roles: list[RoleRow]) -> list[str]:
    """Which roles a policy governs.

    A **department**-scoped policy governs the roles in that department that sit
    *below* its approver, not every role that happens to carry the department label.
    That exclusion is not a loophole, it is what the sentence means: "the director
    approves publishes for marketing" does not say the director needs the director's
    approval, and reading it that way would make the natural org-chart configuration
    fail the acyclicity check for a cycle it does not contain.

    `build_authority` applies the same rule, which is the part that matters — if the
    check and the resolver disagreed about who a policy governs, one of them would be
    wrong about something real. An actor placed in the approver's own role therefore
    gets no entry from this policy, falls through to the blast-radius floor, and is
    **denied**. Safe, and it is the signal that the organization needs a policy naming
    somebody further up.

    An `actor`-scoped policy is not resolved to a role here — the actor's role is not
    in these rows — so it is checked at resolution time instead, where the identity is
    known. Returning nothing for it is correct rather than a gap: the same policy row's
    `approver_role` still has to exist, which is checked above.
    """
    if policy.scope_type == "role":
        return [policy.scope_id]
    if policy.scope_type == "department":
        by_name = _role_index(roles)
        approver = policy.approver_role
        return [
            r.name
            for r in roles
            if r.department == policy.scope_id
            and (approver is None or approver in ancestors(r.name, by_name))
        ]
    return []


def check_delegation_subset(child: ResolvedAuthority, parent: ResolvedAuthority) -> None:
    """A child's authority must be a subset of its parent's.

    **Dormant until M5** — nothing delegates yet — and written now because it is four
    lines here and an audit of every child-spec construction path later. M2 §3 says to
    write it now for exactly that reason.
    """
    ok, why = child.is_subset_of(parent)
    if not ok:
        raise DelegationWidening(
            f"actor {child.actor_name!r} would hold authority its parent "
            f"{parent.actor_name!r} does not: {why}"
        )


# --- the resolver service ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _CacheEntry:
    authority: ResolvedAuthority
    expires_at: float


class AuthorityResolver:
    """Resolves and caches authority for admission.

    The cache is per process and bounded by `AUTHORITY_CACHE_TTL_SECONDS`. It is a
    read cache over four tables that change by the week and are read once per run, so
    the alternative is four round trips on every admission for data that is almost
    always identical.
    """

    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        *,
        action_floors: Mapping[str, BlastRadius] | None = None,
        ttl_seconds: float = AUTHORITY_CACHE_TTL_SECONDS,
        clock: object = None,
    ) -> None:
        self._uow = uow_factory
        self._floors = dict(action_floors or {})
        self._ttl = ttl_seconds
        self._cache: dict[tuple[str, str], _CacheEntry] = {}
        self._clock = clock or time.monotonic

    def _now(self) -> float:
        return float(self._clock())  # type: ignore[operator]

    async def resolve(self, organization_id: OrganizationId, actor_name: str) -> ResolvedAuthority:
        key = (str(organization_id), actor_name)
        hit = self._cache.get(key)
        now = self._now()
        if hit is not None and hit.expires_at > now:
            return hit.authority

        async with self._uow() as uow:
            identity = await uow.authority.load_for_actor(organization_id, actor_name)
            # The acyclicity check runs here as well as at seed time. Seeding is not
            # the only way rows arrive — an operator CLI, a migration, a hand-written
            # INSERT — and a cyclic policy that reaches admission should refuse the
            # run rather than produce an approval nobody can answer.
            check_escalation_acyclicity(identity.roles, identity.policies)

        authority = build_authority(identity, action_floors=self._floors)
        self._cache[key] = _CacheEntry(authority, now + self._ttl)
        log.debug(
            "authority.resolved",
            actor=actor_name,
            role=authority.role,
            gated=sorted(authority.gated_actions()),
            grants=len(authority.tool_grants),
        )
        return authority

    def invalidate(self, organization_id: OrganizationId | None = None) -> None:
        """Drop cached resolutions. Called by the operator CLI after a policy edit,
        so an operator who just changed a policy does not have to wait out the TTL to
        see it — and by tests, which cannot wait 30s."""
        if organization_id is None:
            self._cache.clear()
            return
        wanted = str(organization_id)
        for key in [k for k in self._cache if k[0] == wanted]:
            del self._cache[key]
