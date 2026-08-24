"""Roles, authority policies, tool grants, connections.

Reads, mostly. The one write path that matters is `apply_*`, used by seeding and by
the operator CLI, and it is idempotent for the same reason everything else in this
codebase is: a boot that runs twice must not produce two of anything.

`load_for_actor` is the hot one — it runs once per `start_run()` — and it is a single
round trip on purpose. Four queries here would be four round trips on the admission
path, which §9's latency budget notices.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.domain.enums import AuthorityLevel, OnExpiry
from runtime.persistence.json import to_jsonb


@dataclass(frozen=True, slots=True)
class RoleRow:
    id: uuid.UUID
    name: str
    department: str | None
    parent_role_id: uuid.UUID | None
    rank: int
    approver: str | None
    approver_daily_budget: int
    base_authority: dict[str, Any]


@dataclass(frozen=True, slots=True)
class PolicyRow:
    scope_type: str
    scope_id: str
    action: str
    level: AuthorityLevel
    approver_role: str | None
    max_escalations: int
    on_expiry: OnExpiry
    ttl_seconds: int


@dataclass(frozen=True, slots=True)
class GrantRow:
    subject_type: str
    subject_id: str
    tool: str
    connection_name: str | None
    credential_name: str | None
    expires_at: dt.datetime | None
    revoked_at: dt.datetime | None

    def live_at(self, now: dt.datetime) -> bool:
        if self.revoked_at is not None:
            return False
        return self.expires_at is None or self.expires_at > now


@dataclass(frozen=True, slots=True)
class ActorIdentity:
    """Who an actor is, for authority purposes. Everything needed to resolve."""

    actor_name: str
    role: str | None
    department: str | None
    roles: list[RoleRow] = field(default_factory=list)
    """Every role in the org, not just this actor's — the escalation chain is a walk
    up the tree, and fetching one role then its parent then its parent is a round trip
    per level on the admission path."""
    policies: list[PolicyRow] = field(default_factory=list)
    grants: list[GrantRow] = field(default_factory=list)


class AuthorityRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    # --- writes ---------------------------------------------------------------------

    async def upsert_role(
        self,
        role_id: uuid.UUID,
        organization_id: uuid.UUID,
        *,
        name: str,
        department: str | None,
        parent_role_id: uuid.UUID | None,
        rank: int,
        approver: str | None,
        approver_daily_budget: int,
        base_authority: dict[str, Any],
    ) -> uuid.UUID:
        row = (
            await self._s.execute(
                text(
                    """
                    INSERT INTO roles (id, organization_id, name, department, parent_role_id,
                                       rank, approver, approver_daily_budget, base_authority)
                    VALUES (:id, :org, :name, :dept, :parent, :rank, :approver, :budget,
                            CAST(:base AS jsonb))
                    ON CONFLICT ON CONSTRAINT uq_role_name DO UPDATE
                        SET department = EXCLUDED.department,
                            parent_role_id = EXCLUDED.parent_role_id,
                            rank = EXCLUDED.rank,
                            approver = EXCLUDED.approver,
                            approver_daily_budget = EXCLUDED.approver_daily_budget,
                            base_authority = EXCLUDED.base_authority
                    RETURNING id
                    """
                ),
                {
                    "id": role_id,
                    "org": organization_id,
                    "name": name,
                    "dept": department,
                    "parent": parent_role_id,
                    "rank": rank,
                    "approver": approver,
                    "budget": approver_daily_budget,
                    "base": to_jsonb(base_authority),
                },
            )
        ).scalar_one()
        return uuid.UUID(str(row))

    async def upsert_policy(
        self,
        policy_id: uuid.UUID,
        organization_id: uuid.UUID,
        *,
        scope_type: str,
        scope_id: str,
        action: str,
        level: AuthorityLevel,
        approver_role: str | None,
        max_escalations: int,
        on_expiry: OnExpiry,
        ttl_seconds: int,
    ) -> None:
        await self._s.execute(
            text(
                """
                INSERT INTO authority_policies (id, organization_id, scope_type, scope_id,
                                                action, level, approver_role, max_escalations,
                                                on_expiry, ttl_seconds)
                VALUES (:id, :org, :st, :sid, :action, :level, :ar, :esc, :oe, :ttl)
                ON CONFLICT ON CONSTRAINT uq_authority_scope DO UPDATE
                    SET level = EXCLUDED.level,
                        approver_role = EXCLUDED.approver_role,
                        max_escalations = EXCLUDED.max_escalations,
                        on_expiry = EXCLUDED.on_expiry,
                        ttl_seconds = EXCLUDED.ttl_seconds
                """
            ),
            {
                "id": policy_id,
                "org": organization_id,
                "st": scope_type,
                "sid": scope_id,
                "action": action,
                "level": level.value,
                "ar": approver_role,
                "esc": max_escalations,
                "oe": on_expiry.value,
                "ttl": ttl_seconds,
            },
        )

    async def upsert_connection(
        self,
        connection_id: uuid.UUID,
        organization_id: uuid.UUID,
        *,
        name: str,
        provider: str,
        scopes: list[str],
        credential_name: str | None,
        status: str = "ACTIVE",
    ) -> uuid.UUID:
        row = (
            await self._s.execute(
                text(
                    """
                    INSERT INTO connections (id, organization_id, name, provider, scopes,
                                             status, credential_name)
                    VALUES (:id, :org, :name, :provider, CAST(:scopes AS jsonb), :status, :cred)
                    ON CONFLICT ON CONSTRAINT uq_connection_name DO UPDATE
                        SET provider = EXCLUDED.provider,
                            scopes = EXCLUDED.scopes,
                            status = EXCLUDED.status,
                            credential_name = EXCLUDED.credential_name
                    RETURNING id
                    """
                ),
                {
                    "id": connection_id,
                    "org": organization_id,
                    "name": name,
                    "provider": provider,
                    "scopes": to_jsonb(scopes),
                    "status": status,
                    "cred": credential_name,
                },
            )
        ).scalar_one()
        return uuid.UUID(str(row))

    async def grant_tool(
        self,
        grant_id: uuid.UUID,
        organization_id: uuid.UUID,
        *,
        subject_type: str,
        subject_id: str,
        tool: str,
        connection_id: uuid.UUID | None = None,
        granted_by: str = "system",
        expires_at: dt.datetime | None = None,
    ) -> None:
        """Grant, or un-revoke an existing grant.

        The `DO UPDATE ... revoked_at = NULL` matters: re-granting after a revocation
        must produce a live grant, and `DO NOTHING` would silently leave the tool
        revoked while the operator's command reported success.
        """
        await self._s.execute(
            text(
                """
                INSERT INTO tool_grants (id, organization_id, subject_type, subject_id, tool,
                                         connection_id, granted_by, expires_at)
                VALUES (:id, :org, :st, :sid, :tool, :conn, :by, :exp)
                ON CONFLICT ON CONSTRAINT uq_tool_grant DO UPDATE
                    SET revoked_at = NULL,
                        revoked_by = NULL,
                        granted_at = now(),
                        granted_by = EXCLUDED.granted_by,
                        connection_id = EXCLUDED.connection_id,
                        expires_at = EXCLUDED.expires_at
                """
            ),
            {
                "id": grant_id,
                "org": organization_id,
                "st": subject_type,
                "sid": subject_id,
                "tool": tool,
                "conn": connection_id,
                "by": granted_by,
                "exp": expires_at,
            },
        )

    async def revoke_tool(
        self,
        organization_id: uuid.UUID,
        *,
        subject_type: str,
        subject_id: str,
        tool: str,
        revoked_by: str = "operator",
    ) -> bool:
        row = (
            await self._s.execute(
                text(
                    """
                    UPDATE tool_grants
                       SET revoked_at = now(), revoked_by = :by
                     WHERE organization_id = :org AND subject_type = :st
                       AND subject_id = :sid AND tool = :tool AND revoked_at IS NULL
                    RETURNING id
                    """
                ),
                {
                    "org": organization_id,
                    "st": subject_type,
                    "sid": subject_id,
                    "tool": tool,
                    "by": revoked_by,
                },
            )
        ).one_or_none()
        return row is not None

    async def set_actor_role(
        self,
        organization_id: uuid.UUID,
        actor_name: str,
        *,
        role_name: str | None,
        department: str | None,
    ) -> None:
        await self._s.execute(
            text(
                """
                UPDATE actors SET role_name = :role, department = :dept
                 WHERE organization_id = :org AND name = :name
                """
            ),
            {"org": organization_id, "name": actor_name, "role": role_name, "dept": department},
        )

    # --- reads ----------------------------------------------------------------------

    async def load_for_actor(self, organization_id: uuid.UUID, actor_name: str) -> ActorIdentity:
        """Everything the resolver needs, in one round trip.

        The grants query pulls both the actor's own grants and its role's, because a
        grant is held by whichever of the two the operator chose to attach it to and
        the resolver should not care which.
        """
        identity = (
            await self._s.execute(
                text(
                    """
                    SELECT role_name, department FROM actors
                     WHERE organization_id = :org AND name = :name
                    """
                ),
                {"org": organization_id, "name": actor_name},
            )
        ).one_or_none()
        role_name = identity.role_name if identity is not None else None
        department = identity.department if identity is not None else None

        roles = await self.roles(organization_id)
        policies = await self.policies(organization_id)

        subjects: list[str] = [actor_name]
        if role_name:
            subjects.append(role_name)
        grant_rows = (
            await self._s.execute(
                text(
                    """
                    SELECT g.subject_type, g.subject_id, g.tool, g.expires_at, g.revoked_at,
                           c.name AS connection_name, c.credential_name
                      FROM tool_grants g
                      LEFT JOIN connections c ON c.id = g.connection_id
                                             AND c.status = 'ACTIVE'
                     WHERE g.organization_id = :org
                       AND ((g.subject_type = 'actor' AND g.subject_id = :actor)
                         OR (g.subject_type = 'role'  AND g.subject_id = ANY(:roles)))
                    """
                ),
                {
                    "org": organization_id,
                    "actor": actor_name,
                    "roles": [role_name] if role_name else [],
                },
            )
        ).all()

        return ActorIdentity(
            actor_name=actor_name,
            role=role_name,
            department=department,
            roles=roles,
            policies=policies,
            grants=[
                GrantRow(
                    subject_type=r.subject_type,
                    subject_id=r.subject_id,
                    tool=r.tool,
                    connection_name=r.connection_name,
                    credential_name=r.credential_name,
                    expires_at=r.expires_at,
                    revoked_at=r.revoked_at,
                )
                for r in grant_rows
            ],
        )

    async def roles(self, organization_id: uuid.UUID) -> list[RoleRow]:
        rows = (
            await self._s.execute(
                text(
                    """
                    SELECT id, name, department, parent_role_id, rank, approver,
                           approver_daily_budget, base_authority
                      FROM roles WHERE organization_id = :org ORDER BY rank, name
                    """
                ),
                {"org": organization_id},
            )
        ).all()
        return [
            RoleRow(
                id=r.id,
                name=r.name,
                department=r.department,
                parent_role_id=r.parent_role_id,
                rank=int(r.rank),
                approver=r.approver,
                approver_daily_budget=int(r.approver_daily_budget),
                base_authority=dict(r.base_authority or {}),
            )
            for r in rows
        ]

    async def policies(self, organization_id: uuid.UUID) -> list[PolicyRow]:
        rows = (
            await self._s.execute(
                text(
                    """
                    SELECT scope_type, scope_id, action, level, approver_role,
                           max_escalations, on_expiry, ttl_seconds
                      FROM authority_policies WHERE organization_id = :org
                    """
                ),
                {"org": organization_id},
            )
        ).all()
        return [
            PolicyRow(
                scope_type=r.scope_type,
                scope_id=r.scope_id,
                action=r.action,
                level=AuthorityLevel(r.level),
                approver_role=r.approver_role,
                max_escalations=int(r.max_escalations),
                on_expiry=OnExpiry(r.on_expiry),
                ttl_seconds=int(r.ttl_seconds),
            )
            for r in rows
        ]

    async def live_tools(
        self, organization_id: uuid.UUID, actor_name: str, role_name: str | None
    ) -> frozenset[str]:
        """Which tools this actor holds a live grant for, *right now*.

        This is the query behind the ≤30s permission cache (edge case 64). It is
        deliberately narrow — one column, one index — because it runs on the hot path
        and the cache in front of it is what keeps it off the per-call round trip
        count that §9 warns about.
        """
        rows = (
            await self._s.execute(
                text(
                    """
                    SELECT tool FROM tool_grants
                     WHERE organization_id = :org
                       AND revoked_at IS NULL
                       AND (expires_at IS NULL OR expires_at > now())
                       AND ((subject_type = 'actor' AND subject_id = :actor)
                         OR (subject_type = 'role'  AND subject_id = ANY(:roles)))
                    """
                ),
                {
                    "org": organization_id,
                    "actor": actor_name,
                    "roles": [role_name] if role_name else [],
                },
            )
        ).all()
        return frozenset(r.tool for r in rows)

    async def connection_for_tool(
        self, organization_id: uuid.UUID, actor_name: str, role_name: str | None, tool: str
    ) -> tuple[str, str] | None:
        """`(connection_name, credential_name)` for this actor's grant on this tool."""
        row = (
            await self._s.execute(
                text(
                    """
                    SELECT c.name, c.credential_name
                      FROM tool_grants g
                      JOIN connections c ON c.id = g.connection_id
                     WHERE g.organization_id = :org AND g.tool = :tool
                       AND g.revoked_at IS NULL AND c.status = 'ACTIVE'
                       AND ((g.subject_type = 'actor' AND g.subject_id = :actor)
                         OR (g.subject_type = 'role'  AND g.subject_id = ANY(:roles)))
                     ORDER BY g.subject_type = 'actor' DESC
                     LIMIT 1
                    """
                ),
                {
                    "org": organization_id,
                    "tool": tool,
                    "actor": actor_name,
                    "roles": [role_name] if role_name else [],
                },
            )
        ).one_or_none()
        if row is None or row.credential_name is None:
            return None
        return row.name, row.credential_name
