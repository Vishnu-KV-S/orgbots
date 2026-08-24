"""M2 §3 — the authority resolver, and the two checks that run when a spec is applied.

T32 and T36 live here, plus the resolution chain itself and the dormant delegation
check §3 says to write now.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from runtime.domain.authority import ActionAuthority, ResolvedAuthority, apply_floor
from runtime.domain.enums import AuthorityLevel, BlastRadius, OnExpiry
from runtime.domain.errors import DelegationWidening, EscalationCycle, GrantRevoked
from runtime.gateway.tools import ToolCall
from runtime.org.authority import (
    AuthorityResolver,
    ancestors,
    approver_chain,
    build_authority,
    check_delegation_subset,
    check_escalation_acyclicity,
)
from runtime.org.governance_seed import (
    ACTION_PUBLISH,
    DIRECTOR_ROLE,
    HEAD_ROLE,
    IC_ROLE,
    OPERATOR_ROLE,
)
from runtime.persistence.repositories.authority import PolicyRow, RoleRow
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.settings import Settings
from tests.conftest_m2 import build_harness, make_authority, make_ctx, new_governed_org

pytestmark = pytest.mark.integration

FLOORS = {ACTION_PUBLISH: BlastRadius.IRREVERSIBLE}


# --- the resolution chain ------------------------------------------------------------


async def test_the_resolution_chain_overlays_role_then_department_then_actor(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """§3: later scopes overlay earlier ones, and the actor override wins.

    Asserted by adding an actor-scoped policy on top of the shipped department one and
    checking which survives — rather than by reading the code, which would only prove
    the code says what it says.
    """
    governed_org = await new_governed_org(uow_factory)
    resolver = AuthorityResolver(uow_factory, action_floors=FLOORS)

    before = await resolver.resolve(governed_org, "marketing-head")
    assert before.for_action(ACTION_PUBLISH).level is AuthorityLevel.HUMAN
    assert before.for_action(ACTION_PUBLISH).source == "department:marketing"

    async with uow_factory.transaction() as uow:
        await uow.authority.upsert_policy(
            uuid.uuid4(),
            governed_org,
            scope_type="actor",
            scope_id="marketing-head",
            action=ACTION_PUBLISH,
            level=AuthorityLevel.DENIED,
            approver_role=None,
            max_escalations=0,
            on_expiry=OnExpiry.DENY,
            ttl_seconds=3600,
        )
    resolver.invalidate(governed_org)

    after = await resolver.resolve(governed_org, "marketing-head")
    assert after.for_action(ACTION_PUBLISH).level is AuthorityLevel.DENIED
    assert after.for_action(ACTION_PUBLISH).source == "actor:marketing-head"


async def test_an_unlisted_action_is_denied_not_allowed(uow_factory: UnitOfWorkFactory) -> None:
    """M1 defaulted to AUTO and said so was wrong for M2. This is the inversion."""
    governed_org = await new_governed_org(uow_factory)
    resolver = AuthorityResolver(uow_factory, action_floors=FLOORS)
    authority = await resolver.resolve(governed_org, "research")
    assert authority.for_action("wire_transfer").level is AuthorityLevel.DENIED


async def test_the_blast_radius_floor_tightens_a_policy_that_says_auto(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """I14 as authority: a policy row cannot waive an irreversible tool's gate.

    This is the check that stops governance from being self-service. Somebody with
    write access to `authority_policies` can say `auto`; the floor still says a human
    answers, because the tool registry — not a config row — is the authority on what a
    tool does to the world.
    """
    governed_org = await new_governed_org(uow_factory)
    async with uow_factory.transaction() as uow:
        await uow.authority.upsert_policy(
            uuid.uuid4(),
            governed_org,
            scope_type="actor",
            scope_id="marketing-head",
            action=ACTION_PUBLISH,
            level=AuthorityLevel.AUTO,
            approver_role=None,
            max_escalations=0,
            on_expiry=OnExpiry.DENY,
            ttl_seconds=3600,
        )
    resolver = AuthorityResolver(uow_factory, action_floors=FLOORS)
    resolved = await resolver.resolve(governed_org, "marketing-head")

    entry = resolved.for_action(ACTION_PUBLISH)
    assert entry.level is not AuthorityLevel.AUTO, "the floor wins over the policy"
    assert "blast_radius_floor" in entry.source
    # The policy said `auto`, so it named no approver. Raising it to `human` would
    # produce an approval nobody can answer, so the floor lands on `denied` instead —
    # stricter than asked for, and visible in the denial stream as a policy that needs
    # an approver rather than as a mystery.
    assert entry.level is AuthorityLevel.DENIED
    assert entry.source.endswith("no_approver")


def test_the_floor_never_loosens() -> None:
    """`apply_floor` is one-directional. A READ tool cannot downgrade a HUMAN policy."""
    human = ActionAuthority(action="a", level=AuthorityLevel.HUMAN, approver_chain=("operator",))
    assert apply_floor(human, AuthorityLevel.AUTO).level is AuthorityLevel.HUMAN

    # An `auto` entry carries no approver, so the floor degrades it to `denied` rather
    # than manufacturing one. Stricter than the floor asked for, which is allowed.
    auto = ActionAuthority(action="a", level=AuthorityLevel.AUTO)
    assert apply_floor(auto, AuthorityLevel.HUMAN).level is AuthorityLevel.DENIED

    # With an approver available, the floor does exactly what it says.
    auto_with_chain = ActionAuthority(
        action="a", level=AuthorityLevel.AUTO, approver_chain=("director",)
    )
    assert apply_floor(auto_with_chain, AuthorityLevel.HUMAN).level is AuthorityLevel.HUMAN


async def test_authority_is_frozen_into_the_spec_hash(uow_factory: UnitOfWorkFactory) -> None:
    """§3: the resolved authority is hashed into `spec_hash`.

    Two runs of the same actor under different policies must have different hashes, or
    "what was this run allowed to do" is not answerable from the spec — which is the
    only place it will still be answerable in six months.
    """
    from runtime.domain.ids import ActorId
    from runtime.domain.specs import ActorSpec, compile_actor_spec
    from runtime.org.department import RESEARCH_SPEC

    actor_id = ActorId(uuid.uuid4())
    spec: ActorSpec = RESEARCH_SPEC

    loose = make_authority("research", actions={})
    tight = make_authority(
        "research",
        actions={
            ACTION_PUBLISH: ActionAuthority(action=ACTION_PUBLISH, level=AuthorityLevel.DENIED)
        },
    )
    _, hash_loose = compile_actor_spec(spec, actor_id=actor_id, actor_version=1, authority=loose)
    _, hash_tight = compile_actor_spec(spec, actor_id=actor_id, actor_version=1, authority=tight)
    assert hash_loose != hash_tight


# --- T32: cyclic approval policy rejected at spec compile ---------------------------


def _roles() -> list[RoleRow]:
    ids = {name: uuid.uuid4() for name in (OPERATOR_ROLE, DIRECTOR_ROLE, HEAD_ROLE, IC_ROLE)}
    return [
        RoleRow(ids[OPERATOR_ROLE], OPERATOR_ROLE, None, None, 0, "operator", 10, {}),
        RoleRow(
            ids[DIRECTOR_ROLE],
            DIRECTOR_ROLE,
            "marketing",
            ids[OPERATOR_ROLE],
            5,
            "director",
            6,
            {},
        ),
        RoleRow(ids[HEAD_ROLE], HEAD_ROLE, "marketing", ids[DIRECTOR_ROLE], 10, None, 10, {}),
        RoleRow(ids[IC_ROLE], IC_ROLE, "marketing", ids[HEAD_ROLE], 20, None, 10, {}),
    ]


def _policy(scope_id: str, approver: str, scope_type: str = "role") -> PolicyRow:
    return PolicyRow(
        scope_type=scope_type,
        scope_id=scope_id,
        action=ACTION_PUBLISH,
        level=AuthorityLevel.HUMAN,
        approver_role=approver,
        max_escalations=0,
        on_expiry=OnExpiry.DENY,
        ttl_seconds=3600,
    )


def test_t32_the_shipped_policy_set_compiles() -> None:
    """The negative tests below are only meaningful if the real thing passes."""
    check_escalation_acyclicity(_roles(), [_policy(HEAD_ROLE, DIRECTOR_ROLE)])


def test_t32_a_role_approving_for_itself_is_rejected() -> None:
    with pytest.raises(EscalationCycle, match="approves for itself"):
        check_escalation_acyclicity(_roles(), [_policy(HEAD_ROLE, HEAD_ROLE)])


def test_t32_a_downward_approval_is_rejected() -> None:
    """`ic` approving for `head` is the direct half of edge case 30."""
    with pytest.raises(EscalationCycle, match="not up-hierarchy"):
        check_escalation_acyclicity(_roles(), [_policy(HEAD_ROLE, IC_ROLE)])


def test_t32_a_transitive_cycle_is_rejected() -> None:
    """A approves for B and B approves for A, with a role in between.

    Neither policy is obviously wrong on its own — which is the whole reason this is
    checked mechanically rather than in review.
    """
    with pytest.raises(EscalationCycle, match="not up-hierarchy"):
        check_escalation_acyclicity(
            _roles(),
            [
                _policy(IC_ROLE, DIRECTOR_ROLE),
                PolicyRow(
                    scope_type="role",
                    scope_id=DIRECTOR_ROLE,
                    action="something_else",
                    level=AuthorityLevel.HUMAN,
                    approver_role=IC_ROLE,
                    max_escalations=0,
                    on_expiry=OnExpiry.DENY,
                    ttl_seconds=3600,
                ),
            ],
        )


def test_t32_a_cycle_in_the_role_tree_itself_is_rejected() -> None:
    roles = _roles()
    by_name = {r.name: r for r in roles}
    # Point the operator at the ic: the tree becomes a ring.
    looped = [
        RoleRow(
            r.id,
            r.name,
            r.department,
            by_name[IC_ROLE].id if r.name == OPERATOR_ROLE else r.parent_role_id,
            r.rank,
            r.approver,
            r.approver_daily_budget,
            r.base_authority,
        )
        for r in roles
    ]
    with pytest.raises(EscalationCycle):
        check_escalation_acyclicity(looped, [])


def test_t32_ranks_that_contradict_the_tree_are_rejected() -> None:
    """Two columns that can disagree eventually do; the check is what makes them not."""
    roles = _roles()
    by_name = {r.name: r for r in roles}
    bad = [
        RoleRow(
            r.id,
            r.name,
            r.department,
            r.parent_role_id,
            99 if r.name == DIRECTOR_ROLE else r.rank,
            r.approver,
            r.approver_daily_budget,
            r.base_authority,
        )
        for r in roles
    ]
    assert by_name  # the fixture is what it is
    with pytest.raises(EscalationCycle, match="outrank"):
        check_escalation_acyclicity(bad, [])


def test_t32_an_approver_role_that_does_not_exist_is_rejected() -> None:
    with pytest.raises(EscalationCycle, match="does not exist"):
        check_escalation_acyclicity(_roles(), [_policy(HEAD_ROLE, "vp-of-nothing")])


def test_a_department_policy_does_not_govern_its_own_approver() -> None:
    """The director approving publishes "for marketing" does not include the director.

    Read the other way, the shipped configuration would fail T32 for a cycle it does
    not contain — and worse, `build_authority` would route a director's own approval to
    the director. The check and the resolver agree; this pins that they do.
    """
    roles = _roles()
    policy = _policy("marketing", DIRECTOR_ROLE, scope_type="department")
    check_escalation_acyclicity(roles, [policy])

    from runtime.persistence.repositories.authority import ActorIdentity

    as_director = build_authority(
        ActorIdentity(
            actor_name="someone",
            role=DIRECTOR_ROLE,
            department="marketing",
            roles=roles,
            policies=[policy],
            grants=[],
        ),
        action_floors=FLOORS,
    )
    assert as_director.for_action(ACTION_PUBLISH).level is AuthorityLevel.DENIED, (
        "not governed by the policy, so the floor denies — the org must name someone "
        "further up rather than the director self-approving"
    )

    as_head = build_authority(
        ActorIdentity(
            actor_name="marketing-head",
            role=HEAD_ROLE,
            department="marketing",
            roles=roles,
            policies=[policy],
            grants=[],
        ),
        action_floors=FLOORS,
    )
    assert as_head.for_action(ACTION_PUBLISH).approver == "director"


# --- the escalation chain ------------------------------------------------------------


def test_the_chain_walks_up_and_skips_roles_with_no_approver() -> None:
    roles = {r.name: r for r in _roles()}
    assert ancestors(IC_ROLE, roles) == [HEAD_ROLE, DIRECTOR_ROLE, OPERATOR_ROLE]
    # `head` has no approver of its own, so it is walked through rather than listed.
    assert approver_chain(HEAD_ROLE, roles) == ("director", "operator")


# --- delegation subset (dormant until M5) --------------------------------------------


def test_the_delegation_check_rejects_a_child_looser_than_its_parent() -> None:
    parent = make_authority(
        "marketing-head",
        actions={
            ACTION_PUBLISH: ActionAuthority(
                action=ACTION_PUBLISH,
                level=AuthorityLevel.HUMAN,
                approver_chain=("director",),
            )
        },
        tools=frozenset({"publish.external@1"}),
    )
    child = make_authority(
        "research",
        actions={ACTION_PUBLISH: ActionAuthority(action=ACTION_PUBLISH, level=AuthorityLevel.AUTO)},
        tools=frozenset({"publish.external@1"}),
    )
    with pytest.raises(DelegationWidening, match="may not be looser"):
        check_delegation_subset(child, parent)


def test_the_delegation_check_rejects_a_grant_the_parent_lacks() -> None:
    parent = make_authority("marketing-head", tools=frozenset({"web.fetch@1"}))
    child = make_authority("research", tools=frozenset({"web.fetch@1", "publish.external@1"}))
    with pytest.raises(DelegationWidening, match="tool grants"):
        check_delegation_subset(child, parent)


def test_the_delegation_check_accepts_a_child_that_is_a_subset() -> None:
    parent = make_authority(
        "marketing-head",
        actions={
            ACTION_PUBLISH: ActionAuthority(
                action=ACTION_PUBLISH, level=AuthorityLevel.HUMAN, approver_chain=("director",)
            )
        },
        tools=frozenset({"web.fetch@1", "publish.external@1"}),
    )
    child = make_authority(
        "research",
        actions={
            ACTION_PUBLISH: ActionAuthority(action=ACTION_PUBLISH, level=AuthorityLevel.DENIED)
        },
        tools=frozenset({"web.fetch@1"}),
    )
    check_delegation_subset(child, parent)


def test_an_approval_with_nobody_to_answer_it_cannot_be_constructed() -> None:
    """A HUMAN action with an empty chain is a deadlock with extra steps."""
    with pytest.raises(ValueError, match="names no approver"):
        ActionAuthority(action="a", level=AuthorityLevel.HUMAN)

    with pytest.raises(ValueError, match="nobody to escalate to"):
        ActionAuthority(
            action="a",
            level=AuthorityLevel.HUMAN,
            approver_chain=("operator",),
            max_escalations=1,
        )


# --- T36: a grant revoked mid-run is denied within the cache TTL ---------------------


async def test_t36_a_revoked_grant_is_denied_by_the_next_call(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """Edge case 64. The spec is frozen (I11) so revocation cannot mutate it; the
    ≤30s permission cache is what makes it take effect inside a running run.

    The TTL here is zero so the test does not sleep for thirty seconds. That is not
    cheating: what is under test is that the gateway *re-reads* rather than trusting
    the frozen spec, and the TTL is the bound on how stale the re-read may be.
    """
    governed_org = await new_governed_org(uow_factory)
    async with uow_factory.transaction() as uow:
        await uow.authority.grant_tool(
            uuid.uuid4(),
            governed_org,
            subject_type="actor",
            subject_id="research",
            tool="test.noop@1",
        )

    harness = build_harness(uow_factory, settings, permission_ttl=0.0)
    ctx = make_ctx(governed_org, authority=make_authority("research"))

    first = await harness.gateway.execute(ctx, ToolCall("test.noop@1", {"value": "before"}))
    assert first.ok

    async with uow_factory.transaction() as uow:
        revoked = await uow.authority.revoke_tool(
            governed_org, subject_type="actor", subject_id="research", tool="test.noop@1"
        )
    assert revoked

    with pytest.raises(GrantRevoked, match="no longer live"):
        await harness.gateway.execute(ctx, ToolCall("test.noop@1", {"value": "after"}))

    async with uow_factory() as uow:
        denials = [
            r for r in await uow.audit.decisions_for_run(ctx.run_id) if r["decision"] == "denied"
        ]
    assert [r["check"] for r in denials] == ["grant_revoked"], (
        "distinct from `grant_missing`, which would mean the spec and the grant table "
        "disagree — a seeding bug, not governance working"
    )


async def test_a_revocation_is_not_seen_before_the_cache_expires(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """The other half of edge case 64, and the honest one.

    A cache means a window, and the window is real: within the TTL, a revoked actor
    keeps working. Asserting it here rather than leaving it implied is the difference
    between a documented bound and a surprise — the guarantee M2 makes is "within
    thirty seconds", not "instantly".
    """
    governed_org = await new_governed_org(uow_factory)
    async with uow_factory.transaction() as uow:
        await uow.authority.grant_tool(
            uuid.uuid4(),
            governed_org,
            subject_type="actor",
            subject_id="research",
            tool="test.noop@1",
        )

    harness = build_harness(uow_factory, settings, permission_ttl=300.0)
    ctx = make_ctx(governed_org, authority=make_authority("research"))
    await harness.gateway.execute(ctx, ToolCall("test.noop@1", {"value": "warm the cache"}))

    async with uow_factory.transaction() as uow:
        await uow.authority.revoke_tool(
            governed_org, subject_type="actor", subject_id="research", tool="test.noop@1"
        )

    result = await harness.gateway.execute(ctx, ToolCall("test.noop@1", {"value": "still warm"}))
    assert result.ok, "within the TTL the grant is still believed — this is the documented bound"

    harness.permissions.invalidate(governed_org)
    with pytest.raises(GrantRevoked):
        await harness.gateway.execute(ctx, ToolCall("test.noop@1", {"value": "cold"}))


async def test_an_actor_with_no_grants_at_all_is_not_blocked_by_the_grant_check(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """The grant check applies to actors that hold grants, not to ones that hold none.

    `analytics` holds nothing by design — it is the deterministic control actor. If an
    empty grant set meant "denied everything", the one actor whose numbers the M1 gate
    is read off would stop working the moment M2 shipped, and the cause would look like
    a budget or a scheduler problem.
    """
    governed_org = await new_governed_org(uow_factory)
    harness = build_harness(uow_factory, settings, permission_ttl=0.0)
    ctx = make_ctx(
        governed_org,
        authority=make_authority("analytics", tools=frozenset()),
        actor_name="analytics",
    )
    result = await harness.gateway.execute(ctx, ToolCall("test.noop@1", {"value": "x"}))
    assert result.ok


def test_resolved_authority_is_immutable() -> None:
    """It is frozen into a spec hash; a mutable one would let a caller change what a
    run was allowed to do after the hash was taken."""
    authority = ResolvedAuthority(actor_name="research")
    with pytest.raises(ValidationError):
        authority.actor_name = "marketing-head"  # type: ignore[misc]
