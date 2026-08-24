"""Applying the governance configuration.

One function, idempotent, called by `seed_department`. Same contract as everything
else that boots: running it twice produces one of each.

**The order matters and it is not arbitrary.** Roles first, because policies and
placements reference them by name. Connections before grants, because a grant points
at a connection. Placements before the acyclicity check, because an actor-scoped
policy can only be validated once the actor's role is known.

**The checks run here and they raise.** §3 calls them compile-time checks and this is
compile time: a cyclic escalation policy stops the boot rather than producing an
approval nobody can answer, three weeks later, at 2am. That is the whole argument for
doing this at spec-apply rather than at runtime — the same defect costs a failed
deploy here and a deadlock there.
"""

from __future__ import annotations

from dataclasses import dataclass

from runtime.domain.enums import BlastRadius
from runtime.domain.ids import OrganizationId
from runtime.observability.logging import get_logger
from runtime.org.authority import build_authority, check_escalation_acyclicity
from runtime.org.governance_seed import (
    CONNECTIONS,
    GRANTS,
    PLACEMENTS,
    POLICIES,
    RATE_LIMITS,
    ROLES,
    connection_id_for,
    grant_id_for,
    policy_id_for,
    rate_limit_id_for,
    role_id_for,
)
from runtime.persistence.uow import UnitOfWorkFactory

log = get_logger("runtime.governance")


@dataclass(frozen=True, slots=True)
class GovernanceSeeded:
    roles: int
    policies: int
    connections: int
    grants: int
    rate_limits: int
    placements: int
    gated_actions: dict[str, list[str]]
    """actor → the actions that will need a human. Returned rather than logged only,
    because a seeding function that silently produced *no* gates would look exactly
    like a successful one, and this is the value a test can assert on."""


async def seed_governance(
    uow_factory: UnitOfWorkFactory,
    organization_id: OrganizationId,
    *,
    action_floors: dict[str, BlastRadius] | None = None,
) -> GovernanceSeeded:
    """Install roles, policies, connections, grants and rate limits. Idempotent."""
    async with uow_factory.transaction() as uow:
        for role in ROLES:
            await uow.authority.upsert_role(
                role_id_for(organization_id, role.name),
                organization_id,
                name=role.name,
                department=role.department,
                parent_role_id=(role_id_for(organization_id, role.parent) if role.parent else None),
                rank=role.rank,
                approver=role.approver,
                approver_daily_budget=role.approver_daily_budget,
                base_authority=dict(role.base_authority),
            )

        for policy in POLICIES:
            await uow.authority.upsert_policy(
                policy_id_for(organization_id, policy.scope_type, policy.scope_id, policy.action),
                organization_id,
                scope_type=policy.scope_type,
                scope_id=policy.scope_id,
                action=policy.action,
                level=policy.level,
                approver_role=policy.approver_role,
                max_escalations=policy.max_escalations,
                on_expiry=policy.on_expiry,
                ttl_seconds=policy.ttl_seconds,
            )

        for connection in CONNECTIONS:
            await uow.authority.upsert_connection(
                connection_id_for(organization_id, connection.name),
                organization_id,
                name=connection.name,
                provider=connection.provider,
                scopes=list(connection.scopes),
                credential_name=connection.credential_name,
            )

        for placement in PLACEMENTS:
            await uow.authority.set_actor_role(
                organization_id,
                placement.actor,
                role_name=placement.role,
                department=placement.department,
            )

        for grant in GRANTS:
            await uow.authority.grant_tool(
                grant_id_for(organization_id, grant.subject_type, grant.subject_id, grant.tool),
                organization_id,
                subject_type=grant.subject_type,
                subject_id=grant.subject_id,
                tool=grant.tool,
                connection_id=(
                    connection_id_for(organization_id, grant.connection)
                    if grant.connection
                    else None
                ),
                granted_by="seed",
            )

        for limit in RATE_LIMITS:
            await uow.rate_limits.upsert(
                rate_limit_id_for(organization_id, limit.scope_type, limit.scope_id),
                organization_id,
                scope_type=limit.scope_type,
                scope_id=limit.scope_id,
                limit_per_window=limit.limit_per_window,
                window_seconds=limit.window_seconds,
                fail_open=limit.fail_open,
            )

        # --- §3's compile-time checks -----------------------------------------------
        # Inside the transaction, so a policy set that fails them is not merely
        # reported — it is not written. A half-applied cyclic policy is worse than
        # none, because the half that applied looks deliberate.
        roles = await uow.authority.roles(organization_id)
        policies = await uow.authority.policies(organization_id)
        check_escalation_acyclicity(roles, policies)

        gated: dict[str, list[str]] = {}
        for placement in PLACEMENTS:
            identity = await uow.authority.load_for_actor(organization_id, placement.actor)
            authority = build_authority(identity, action_floors=action_floors)
            gated[placement.actor] = sorted(authority.gated_actions())

    log.info(
        "governance.seeded",
        organization_id=str(organization_id),
        roles=len(ROLES),
        policies=len(POLICIES),
        grants=len(GRANTS),
        gated=gated,
    )
    return GovernanceSeeded(
        roles=len(ROLES),
        policies=len(POLICIES),
        connections=len(CONNECTIONS),
        grants=len(GRANTS),
        rate_limits=len(RATE_LIMITS),
        placements=len(PLACEMENTS),
        gated_actions=gated,
    )
