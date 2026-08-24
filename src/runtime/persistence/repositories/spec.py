"""The config plane's tables, and the live-state read the differ works against.

Two halves.

**The tables** (migrations 031-032): `spec_documents`, `apply_plans`, `apply_events`.
Nothing subtle — an insert, an insert, an insert — except that `record_document` is
`ON CONFLICT DO NOTHING` on `(org, kind, name, spec_hash)`, so re-applying an unchanged
document is genuinely a no-op rather than a second row with a later timestamp.

**The live-state read.** `load_state` is one object holding what the organization
currently *is*, in the shape the differ compares against. It is one method rather than
eight because a diff assembled from eight separately-read snapshots is a diff against a
state that never existed — and `plan_hash` would then be a hash of a fiction. The reads
all happen on one session inside one transaction, so what comes back is one snapshot.

The advisory lock lives here too. `pg_advisory_xact_lock` rather than the session-level
variant: the lock is released by the transaction ending, however it ends, so a crashed
applier cannot leave an organization locked (edge case 75).
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.persistence.json import to_jsonb
from runtime.persistence.repositories.triggers import TriggerRepository

APPLY_LOCK_NAMESPACE = 0x4D34
"""M4's advisory-lock namespace. Distinct from the test suite's session lock so that a
test session holding the database does not look like a concurrent apply."""


def apply_lock_key(organization_id: uuid.UUID) -> int:
    """A signed 64-bit key for `pg_advisory_xact_lock`, derived from the org.

    Postgres advisory locks are keyed on a bigint, so the uuid is folded rather than
    stored. A collision between two organizations would serialise two applies that did
    not need serialising — a small cost, and the only failure mode this derivation has.
    """
    folded = (APPLY_LOCK_NAMESPACE << 48) ^ (organization_id.int & ((1 << 48) - 1))
    return folded - (1 << 63) if folded >= (1 << 63) else folded


@dataclass(frozen=True, slots=True)
class ActorState:
    id: uuid.UUID
    name: str
    kind: str
    role_name: str | None
    department: str | None
    reports_to: str | None
    uid: uuid.UUID | None
    active: bool
    spec_hash: str | None
    version: int | None
    run_count: int
    live_runs: int


@dataclass(frozen=True, slots=True)
class RoleState:
    name: str
    rank: int
    parent: str | None
    department: str | None
    approver: str | None
    approver_daily_budget: int
    base_authority: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ConnectionState:
    name: str
    provider: str
    scopes: tuple[str, ...]
    credential_name: str | None
    status: str


@dataclass(frozen=True, slots=True)
class GrantState:
    subject_type: str
    subject_id: str
    tool: str
    connection: str | None
    revoked: bool


@dataclass(frozen=True, slots=True)
class TriggerState:
    key: str
    actor_name: str
    cron: str
    timezone: str
    input: dict[str, Any]
    catchup: str
    active: bool


@dataclass(frozen=True, slots=True)
class DocumentState:
    kind: str
    name: str
    spec_hash: str
    applied_at: dt.datetime
    applied_by: str


@dataclass(frozen=True, slots=True)
class OrgState:
    """What the organization currently is. The differ's left-hand side."""

    exists: bool
    name: str | None = None
    actors: tuple[ActorState, ...] = ()
    roles: tuple[RoleState, ...] = ()
    connections: tuple[ConnectionState, ...] = ()
    grants: tuple[GrantState, ...] = ()
    triggers: tuple[TriggerState, ...] = ()
    documents: tuple[DocumentState, ...] = ()
    policies: tuple[dict[str, Any], ...] = ()
    pools: dict[str, tuple[int, int]] = field(default_factory=dict)
    """scope key → `(limit_cents, committed + reserved)` for the current period. What
    edge case 79 needs to refuse a limit lowered below what is already spent."""

    def actor(self, name: str) -> ActorState | None:
        return next((a for a in self.actors if a.name == name), None)

    def document(self, kind: str, name: str) -> DocumentState | None:
        return next((d for d in self.documents if d.kind == kind and d.name == name), None)


class SpecRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    # --- the advisory lock ------------------------------------------------------------

    async def lock_organization(self, organization_id: uuid.UUID, *, wait: bool = False) -> bool:
        """Serialise applies for one organization (edge case 75).

        `wait=False` is the default because an operator who runs `apply` while a
        colleague's apply is in flight wants to be told, not to block on a terminal for
        an unknown length of time and then apply a plan computed against pre-colleague
        state. The plan-hash recheck would catch that second part, but a clear refusal
        is a better answer than a confusing one.
        """
        key = apply_lock_key(organization_id)
        if wait:
            await self._s.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
            return True
        got = (
            await self._s.execute(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key})
        ).scalar_one()
        return bool(got)

    # --- writes the applier needs ------------------------------------------------------
    #
    # These live here rather than in `runtime.spec` for the reason every other statement
    # in this codebase does: SQL belongs to a repository, and an applier holding its own
    # `text()` calls would be a second place that knows the schema.

    async def ensure_organization(self, organization_id: uuid.UUID, name: str) -> None:
        await self._s.execute(
            text(
                """
                INSERT INTO organizations (id, name) VALUES (:id, :name)
                ON CONFLICT (id) DO NOTHING
                """
            ),
            {"id": organization_id, "name": name},
        )

    async def set_role_parent(
        self, organization_id: uuid.UUID, name: str, parent_role_id: uuid.UUID | None
    ) -> None:
        """Phase 2's reference write for roles (§7)."""
        await self._s.execute(
            text(
                """
                UPDATE roles SET parent_role_id = :parent
                 WHERE organization_id = :org AND name = :name
                """
            ),
            {"org": organization_id, "name": name, "parent": parent_role_id},
        )

    async def actor_ids(self, organization_id: uuid.UUID) -> dict[str, uuid.UUID]:
        rows = (
            await self._s.execute(
                text("SELECT id, name FROM actors WHERE organization_id = :org"),
                {"org": organization_id},
            )
        ).all()
        return {r.name: r.id for r in rows}

    async def active_spec_hashes(self, organization_id: uuid.UUID) -> dict[str, str]:
        """actor name → the hash of its *active* version.

        What `apply` compares against to decide whether to publish at all. §13 risk 2:
        if every apply churned a version for every actor, "nothing changed" and
        "everything changed" would look the same in `actor_versions`.
        """
        rows = (
            await self._s.execute(
                text(
                    """
                    SELECT a.name, av.spec_hash FROM actors a
                      JOIN actor_versions av ON av.id = a.active_version_id
                     WHERE a.organization_id = :org
                    """
                ),
                {"org": organization_id},
            )
        ).all()
        return {r.name: r.spec_hash for r in rows}

    async def next_actor_version(self, actor_id: uuid.UUID) -> int:
        return int(
            (
                await self._s.execute(
                    text(
                        "SELECT COALESCE(max(version), 0) + 1 FROM actor_versions "
                        "WHERE actor_id = :id"
                    ),
                    {"id": actor_id},
                )
            ).scalar_one()
        )

    async def rename_actor(self, organization_id: uuid.UUID, old: str, new: str) -> bool:
        """Move an actor to a new name, keeping its id, versions and history.

        That retention is the entire difference between `--rename` and the delete-plus-
        create §4 says a rename otherwise is. `reports_to` is a name, so every actor
        pointing at the old one is repointed in the same statement pair — a dangling
        `reports_to` would pass the CHECK constraint and fail the compiler's reporting
        walk on the *next* apply, which is a confusing place to find out.
        """
        moved = (
            await self._s.execute(
                text(
                    """
                    UPDATE actors SET name = :new
                     WHERE organization_id = :org AND name = :old
                    RETURNING id
                    """
                ),
                {"org": organization_id, "old": old, "new": new},
            )
        ).one_or_none()
        if moved is None:
            return False
        await self._s.execute(
            text(
                """
                UPDATE actors SET reports_to = :new
                 WHERE organization_id = :org AND reports_to = :old
                """
            ),
            {"org": organization_id, "old": old, "new": new},
        )
        return True

    # --- writes -----------------------------------------------------------------------

    async def record_document(
        self,
        organization_id: uuid.UUID,
        *,
        kind: str,
        name: str,
        source_yaml: str,
        compiled_spec: dict[str, Any],
        spec_hash: str,
        source_ref: str | None,
        applied_by: str,
    ) -> bool:
        """Store one applied document. Returns whether it was new."""
        row = (
            await self._s.execute(
                text(
                    """
                    INSERT INTO spec_documents (id, organization_id, kind, name, source_yaml,
                                                compiled_spec, spec_hash, source_ref, applied_by)
                    VALUES (:id, :org, :kind, :name, :yaml, CAST(:compiled AS jsonb),
                            :hash, :ref, :by)
                    ON CONFLICT ON CONSTRAINT uq_spec_document DO NOTHING
                    RETURNING id
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "org": organization_id,
                    "kind": kind,
                    "name": name,
                    "yaml": source_yaml,
                    "compiled": to_jsonb(compiled_spec),
                    "hash": spec_hash,
                    "ref": source_ref,
                    "by": applied_by,
                },
            )
        ).scalar_one_or_none()
        return row is not None

    async def save_plan(
        self,
        plan_id: uuid.UUID,
        organization_id: uuid.UUID,
        *,
        plan: dict[str, Any],
        plan_hash: str,
        created_by: str,
    ) -> None:
        await self._s.execute(
            text(
                """
                INSERT INTO apply_plans (id, organization_id, plan, plan_hash, created_by)
                VALUES (:id, :org, CAST(:plan AS jsonb), :hash, :by)
                """
            ),
            {
                "id": plan_id,
                "org": organization_id,
                "plan": to_jsonb(plan),
                "hash": plan_hash,
                "by": created_by,
            },
        )

    async def get_plan(self, plan_id: uuid.UUID) -> dict[str, Any] | None:
        row = (
            await self._s.execute(
                text(
                    """
                    SELECT id, organization_id, plan, plan_hash, created_at, applied_at, status
                      FROM apply_plans WHERE id = :id
                    """
                ),
                {"id": plan_id},
            )
        ).one_or_none()
        return dict(row._mapping) if row is not None else None

    async def mark_plan(self, plan_id: uuid.UUID, status: str) -> None:
        await self._s.execute(
            text(
                """
                UPDATE apply_plans
                   SET status = :status,
                       applied_at = CASE WHEN :status = 'APPLIED' THEN now() ELSE applied_at END
                 WHERE id = :id
                """
            ),
            {"id": plan_id, "status": status},
        )

    async def record_event(
        self,
        organization_id: uuid.UUID,
        *,
        plan_id: uuid.UUID | None,
        kind: str,
        name: str,
        action: str,
        detail: dict[str, Any],
        applied_by: str,
    ) -> None:
        await self._s.execute(
            text(
                """
                INSERT INTO apply_events (plan_id, organization_id, kind, name, action,
                                          detail, applied_by)
                VALUES (:plan, :org, :kind, :name, :action, CAST(:detail AS jsonb), :by)
                """
            ),
            {
                "plan": plan_id,
                "org": organization_id,
                "kind": kind,
                "name": name,
                "action": action,
                "detail": to_jsonb(detail),
                "by": applied_by,
            },
        )

    async def events(self, organization_id: uuid.UUID, limit: int = 50) -> list[dict[str, Any]]:
        rows = (
            await self._s.execute(
                text(
                    """
                    SELECT kind, name, action, detail, applied_by, created_at
                      FROM apply_events WHERE organization_id = :org
                     ORDER BY created_at DESC, id DESC LIMIT :limit
                    """
                ),
                {"org": organization_id, "limit": limit},
            )
        ).all()
        return [dict(r._mapping) for r in rows]

    async def set_actor_identity(
        self,
        organization_id: uuid.UUID,
        name: str,
        *,
        reports_to: str | None,
        uid: uuid.UUID | None,
    ) -> None:
        """Phase 2's reference write for actors (§7).

        `role_name` and `department` are set by `AuthorityRepository.set_actor_role`,
        which already exists and is already what governance uses. Two writers for one
        row in one transaction is fine; two *definitions* of what a placement means
        would not be, so this deliberately does not touch those columns.
        """
        await self._s.execute(
            text(
                """
                UPDATE actors SET reports_to = :reports_to,
                                  uid = COALESCE(:uid, uid),
                                  active = true
                 WHERE organization_id = :org AND name = :name
                """
            ),
            {"org": organization_id, "name": name, "reports_to": reports_to, "uid": uid},
        )

    async def deactivate_actor(self, organization_id: uuid.UUID, name: str) -> None:
        """Edge case 78. Deactivate, never delete.

        A hard delete breaks a resumed run's `resolve_active` and orphans every
        `actor_versions` row, and versions are never deleted (v3 §14). The active
        pointer is left exactly where it is, so a run already admitted still resolves
        its spec; what changes is that nothing new starts.
        """
        await self._s.execute(
            text("UPDATE actors SET active = false WHERE organization_id = :org AND name = :name"),
            {"org": organization_id, "name": name},
        )

    async def deactivate_trigger(self, organization_id: uuid.UUID, key: str) -> None:
        """The apply path's single-key pause, delegated so there is one statement.

        `TriggerRepository.set_active` is the operator's plural version. Two hand-
        written UPDATEs would be two places to remember when `triggers` grows a column
        that a pause has to touch, and the first one anybody would forget is this one.
        """
        await TriggerRepository(self._s).set_active(organization_id, [key], False)

    # --- the live-state read -----------------------------------------------------------

    async def load_state(self, organization_id: uuid.UUID) -> OrgState:
        """One snapshot of what the organization currently is. See the module docstring."""
        org = (
            await self._s.execute(
                text("SELECT name FROM organizations WHERE id = :org"),
                {"org": organization_id},
            )
        ).one_or_none()
        if org is None:
            return OrgState(exists=False)

        actor_rows = (
            await self._s.execute(
                text(
                    """
                    SELECT a.id, a.name, a.kind, a.role_name, a.department, a.reports_to,
                           a.uid, a.active, av.spec_hash, av.version,
                           (SELECT count(*) FROM runs r
                             WHERE r.organization_id = a.organization_id
                               AND r.actor_id = a.id) AS run_count,
                           (SELECT count(*) FROM runs r
                             WHERE r.actor_id = a.id
                               AND r.status IN ('QUEUED','RUNNING')) AS live_runs
                      FROM actors a
                      LEFT JOIN actor_versions av ON av.id = a.active_version_id
                     WHERE a.organization_id = :org
                     ORDER BY a.name
                    """
                ),
                {"org": organization_id},
            )
        ).all()

        role_rows = (
            await self._s.execute(
                text(
                    """
                    SELECT r.name, r.rank, r.department, r.approver, r.approver_daily_budget,
                           r.base_authority, p.name AS parent
                      FROM roles r LEFT JOIN roles p ON p.id = r.parent_role_id
                     WHERE r.organization_id = :org ORDER BY r.rank, r.name
                    """
                ),
                {"org": organization_id},
            )
        ).all()

        connection_rows = (
            await self._s.execute(
                text(
                    """
                    SELECT name, provider, scopes, credential_name, status
                      FROM connections WHERE organization_id = :org ORDER BY name
                    """
                ),
                {"org": organization_id},
            )
        ).all()

        grant_rows = (
            await self._s.execute(
                text(
                    """
                    SELECT g.subject_type, g.subject_id, g.tool, g.revoked_at, c.name AS connection
                      FROM tool_grants g LEFT JOIN connections c ON c.id = g.connection_id
                     WHERE g.organization_id = :org
                     ORDER BY g.subject_type, g.subject_id, g.tool
                    """
                ),
                {"org": organization_id},
            )
        ).all()

        trigger_rows = (
            await self._s.execute(
                text(
                    """
                    SELECT key, actor_name, cron, timezone, input, catchup_policy, active
                      FROM triggers WHERE organization_id = :org ORDER BY key
                    """
                ),
                {"org": organization_id},
            )
        ).all()

        policy_rows = (
            await self._s.execute(
                text(
                    """
                    SELECT scope_type, scope_id, action, level, approver_role,
                           max_escalations, on_expiry, ttl_seconds
                      FROM authority_policies WHERE organization_id = :org
                     ORDER BY scope_type, scope_id, action
                    """
                ),
                {"org": organization_id},
            )
        ).all()

        # The most recent document per (kind, name). `DISTINCT ON` rather than a
        # window function because the index in migration 031 is exactly this order.
        document_rows = (
            await self._s.execute(
                text(
                    """
                    SELECT DISTINCT ON (kind, name) kind, name, spec_hash, applied_at, applied_by
                      FROM spec_documents WHERE organization_id = :org
                     ORDER BY kind, name, applied_at DESC
                    """
                ),
                {"org": organization_id},
            )
        ).all()

        pool_rows = (
            await self._s.execute(
                text(
                    """
                    SELECT scope_type, scope_id, limit_cents,
                           committed_cents + reserved_cents AS held
                      FROM budget_pools
                     WHERE organization_id = :org
                       AND period_start = date_trunc('month', now())::date
                    """
                ),
                {"org": organization_id},
            )
        ).all()

        return OrgState(
            exists=True,
            name=org.name,
            actors=tuple(
                ActorState(
                    id=r.id,
                    name=r.name,
                    kind=r.kind,
                    role_name=r.role_name,
                    department=r.department,
                    reports_to=r.reports_to,
                    uid=r.uid,
                    active=bool(r.active),
                    spec_hash=r.spec_hash,
                    version=int(r.version) if r.version is not None else None,
                    run_count=int(r.run_count),
                    live_runs=int(r.live_runs),
                )
                for r in actor_rows
            ),
            roles=tuple(
                RoleState(
                    name=r.name,
                    rank=int(r.rank),
                    parent=r.parent,
                    department=r.department,
                    approver=r.approver,
                    approver_daily_budget=int(r.approver_daily_budget),
                    base_authority=dict(r.base_authority or {}),
                )
                for r in role_rows
            ),
            connections=tuple(
                ConnectionState(
                    name=r.name,
                    provider=r.provider,
                    scopes=tuple(r.scopes or ()),
                    credential_name=r.credential_name,
                    status=r.status,
                )
                for r in connection_rows
            ),
            grants=tuple(
                GrantState(
                    subject_type=r.subject_type,
                    subject_id=r.subject_id,
                    tool=r.tool,
                    connection=r.connection,
                    revoked=r.revoked_at is not None,
                )
                for r in grant_rows
            ),
            triggers=tuple(
                TriggerState(
                    key=r.key,
                    actor_name=r.actor_name,
                    cron=r.cron,
                    timezone=r.timezone,
                    input=dict(r.input or {}),
                    catchup=r.catchup_policy,
                    active=bool(r.active),
                )
                for r in trigger_rows
            ),
            documents=tuple(
                DocumentState(
                    kind=r.kind,
                    name=r.name,
                    spec_hash=r.spec_hash,
                    applied_at=r.applied_at,
                    applied_by=r.applied_by,
                )
                for r in document_rows
            ),
            policies=tuple(dict(r._mapping) for r in policy_rows),
            pools={
                f"{r.scope_type}:{r.scope_id}": (int(r.limit_cents), int(r.held)) for r in pool_rows
            },
        )


__all__ = [
    "APPLY_LOCK_NAMESPACE",
    "ActorState",
    "ConnectionState",
    "DocumentState",
    "GrantState",
    "OrgState",
    "RoleState",
    "SpecRepository",
    "TriggerState",
    "apply_lock_key",
]
