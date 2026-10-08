"""Policies, team secrets, the control-plane audit trail, SCIM tokens and telemetry
export settings. See migration 053."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.domain.policies import Policy


@dataclass(frozen=True, slots=True)
class SecretRow:
    id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    key_id: str
    nonce: bytes
    ciphertext: bytes
    bytes: int
    updated_by: uuid.UUID | None
    created_at: dt.datetime
    updated_at: dt.datetime

    def __repr__(self) -> str:
        return f"SecretRow(name={self.name!r}, bytes={self.bytes})"


@dataclass(frozen=True, slots=True)
class AuditEventRow:
    id: int
    organization_id: uuid.UUID
    occurred_at: dt.datetime
    actor_member_id: uuid.UUID | None
    actor: str
    action: str
    target: str
    detail: dict[str, Any]
    exported_at: dt.datetime | None


@dataclass(frozen=True, slots=True)
class OtelRow:
    organization_id: uuid.UUID
    endpoint: str
    headers_key_id: str | None
    headers_nonce: bytes | None
    headers_ciphertext: bytes | None
    include_email: bool
    include_actions: bool
    enabled: bool
    last_error: str
    last_sent_at: dt.datetime | None
    updated_at: dt.datetime


_SEALED_VALUE_COLUMNS = (
    "id, organization_id, name, key_id, nonce, ciphertext, bytes, updated_by, created_at, "
    "updated_at"
)
_EVENT = (
    "id, organization_id, occurred_at, actor_member_id, actor, action, target, detail, exported_at"
)
_OTEL = (
    "organization_id, endpoint, headers_key_id, headers_nonce, headers_ciphertext, "
    "include_email, include_actions, enabled, last_error, last_sent_at, updated_at"
)


def _event(r: Any) -> AuditEventRow:
    return AuditEventRow(
        id=int(r.id),
        organization_id=r.organization_id,
        occurred_at=r.occurred_at,
        actor_member_id=r.actor_member_id,
        actor=r.actor,
        action=r.action,
        target=r.target,
        detail=dict(r.detail or {}),
        exported_at=r.exported_at,
    )


class PolicyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get(self, organization_id: uuid.UUID) -> Policy:
        row = (
            await self._s.execute(
                text(
                    "SELECT network, allowed_hosts, require_review, template_links, "
                    "members_add_apps FROM org_policies WHERE organization_id = :org"
                ),
                {"org": organization_id},
            )
        ).first()
        if row is None:
            return Policy()
        return Policy(
            network=row.network,
            allowed_hosts=tuple(row.allowed_hosts or ()),
            require_review=bool(row.require_review),
            template_links=bool(row.template_links),
            members_add_apps=bool(row.members_add_apps),
        )

    async def save(self, organization_id: uuid.UUID, policy: Policy) -> None:
        await self._s.execute(
            text(
                """
                INSERT INTO org_policies (organization_id, network, allowed_hosts,
                                          require_review, template_links, members_add_apps)
                VALUES (:org, :network, :hosts, :review, :links, :apps)
                ON CONFLICT (organization_id) DO UPDATE SET
                    network = EXCLUDED.network, allowed_hosts = EXCLUDED.allowed_hosts,
                    require_review = EXCLUDED.require_review,
                    template_links = EXCLUDED.template_links,
                    members_add_apps = EXCLUDED.members_add_apps, updated_at = now()
                """
            ),
            {
                "org": organization_id,
                "network": policy.network,
                "hosts": list(policy.allowed_hosts),
                "review": policy.require_review,
                "links": policy.template_links,
                "apps": policy.members_add_apps,
            },
        )


class TeamSecretRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def for_organization(self, organization_id: uuid.UUID) -> list[SecretRow]:
        rows = await self._s.execute(
            text(
                f"SELECT {_SEALED_VALUE_COLUMNS} FROM team_secrets "
                "WHERE organization_id = :org ORDER BY name"
            ),
            {"org": organization_id},
        )
        return [SecretRow(*r) for r in rows]

    async def put(
        self,
        organization_id: uuid.UUID,
        name: str,
        *,
        sealed: tuple[str, bytes, bytes],
        size: int,
        updated_by: uuid.UUID | None,
    ) -> None:
        key_id, nonce, ciphertext = sealed
        await self._s.execute(
            text(
                """
                INSERT INTO team_secrets (id, organization_id, name, key_id, nonce, ciphertext,
                                          bytes, updated_by)
                VALUES (:id, :org, :name, :kid, :nonce, :ct, :bytes, :by)
                ON CONFLICT ON CONSTRAINT uq_team_secret_name DO UPDATE SET
                    key_id = EXCLUDED.key_id, nonce = EXCLUDED.nonce,
                    ciphertext = EXCLUDED.ciphertext, bytes = EXCLUDED.bytes,
                    updated_by = EXCLUDED.updated_by, updated_at = now()
                """
            ),
            {
                "id": uuid.uuid4(),
                "org": organization_id,
                "name": name,
                "kid": key_id,
                "nonce": nonce,
                "ct": ciphertext,
                "bytes": size,
                "by": updated_by,
            },
        )

    async def delete(self, organization_id: uuid.UUID, name: str) -> bool:
        done = await self._s.execute(
            text("DELETE FROM team_secrets WHERE organization_id = :org AND name = :name"),
            {"org": organization_id, "name": name},
        )
        return bool(getattr(done, "rowcount", 0))


class OrgAuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def record(
        self,
        organization_id: uuid.UUID,
        action: str,
        *,
        actor_member_id: uuid.UUID | None = None,
        actor: str = "",
        target: str = "",
        detail: dict[str, Any] | None = None,
    ) -> None:
        await self._s.execute(
            text(
                """
                INSERT INTO org_audit_events (organization_id, actor_member_id, actor, action,
                                              target, detail)
                VALUES (:org, :member, :actor, :action, :target, CAST(:detail AS jsonb))
                """
            ),
            {
                "org": organization_id,
                "member": actor_member_id,
                "actor": actor[:254],
                "action": action,
                "target": target[:500],
                "detail": json.dumps(detail or {}, default=str),
            },
        )

    async def recent(
        self, organization_id: uuid.UUID, *, limit: int = 200, action: str = ""
    ) -> list[AuditEventRow]:
        rows = await self._s.execute(
            text(
                f"SELECT {_EVENT} FROM org_audit_events WHERE organization_id = :org "
                "AND (:action = '' OR action LIKE :prefix) ORDER BY id DESC LIMIT :n"
            ),
            {"org": organization_id, "action": action, "prefix": action + "%", "n": limit},
        )
        return [_event(r) for r in rows]

    async def unexported(self, organization_id: uuid.UUID, limit: int = 500) -> list[AuditEventRow]:
        rows = await self._s.execute(
            text(
                f"SELECT {_EVENT} FROM org_audit_events WHERE organization_id = :org "
                "AND exported_at IS NULL ORDER BY id LIMIT :n"
            ),
            {"org": organization_id, "n": limit},
        )
        return [_event(r) for r in rows]

    async def mark_exported(self, ids: list[int]) -> None:
        if ids:
            await self._s.execute(
                text("UPDATE org_audit_events SET exported_at = now() WHERE id = ANY(:ids)"),
                {"ids": ids},
            )

    async def decisions_after(
        self, organization_id: uuid.UUID, after_id: int, limit: int = 500
    ) -> list[dict[str, Any]]:
        """The gateway's tool-call decisions (`audit_logs`) — what was called and how it
        ended. Never arguments or results: those stay in the runtime."""
        rows = await self._s.execute(
            text(
                """
                SELECT id, occurred_at, run_id, actor_name, gateway, subject, decision,
                       check_name, severity, blast_radius
                  FROM audit_logs
                 WHERE organization_id = :org AND id > :after AND gateway = 'tool'
                 ORDER BY id LIMIT :n
                """
            ),
            {"org": organization_id, "after": after_id, "n": limit},
        )
        return [dict(r._mapping) for r in rows]


class ScimRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def set_token(self, organization_id: uuid.UUID, token_hash: bytes, hint: str) -> None:
        await self._s.execute(
            text(
                """
                INSERT INTO scim_tokens (organization_id, token_hash, hint)
                VALUES (:org, :hash, :hint)
                ON CONFLICT (organization_id) DO UPDATE SET token_hash = EXCLUDED.token_hash,
                    hint = EXCLUDED.hint, created_at = now(), last_used_at = NULL
                """
            ),
            {"org": organization_id, "hash": token_hash, "hint": hint},
        )

    async def organization_for(self, token_hash: bytes) -> uuid.UUID | None:
        found = await self._s.scalar(
            text(
                "UPDATE scim_tokens SET last_used_at = now() WHERE token_hash = :hash "
                "RETURNING organization_id"
            ),
            {"hash": token_hash},
        )
        return found if isinstance(found, uuid.UUID) else None

    async def status(self, organization_id: uuid.UUID) -> dict[str, Any] | None:
        row = (
            await self._s.execute(
                text(
                    "SELECT hint, created_at, last_used_at FROM scim_tokens "
                    "WHERE organization_id = :org"
                ),
                {"org": organization_id},
            )
        ).first()
        return dict(row._mapping) if row else None

    async def delete(self, organization_id: uuid.UUID) -> None:
        await self._s.execute(
            text("DELETE FROM scim_tokens WHERE organization_id = :org"), {"org": organization_id}
        )


class OtelRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def get(self, organization_id: uuid.UUID) -> OtelRow | None:
        row = (
            await self._s.execute(
                text(f"SELECT {_OTEL} FROM otel_configs WHERE organization_id = :org"),
                {"org": organization_id},
            )
        ).first()
        return OtelRow(*row) if row else None

    async def enabled(self) -> list[OtelRow]:
        rows = await self._s.execute(text(f"SELECT {_OTEL} FROM otel_configs WHERE enabled"))
        return [OtelRow(*r) for r in rows]

    async def save(
        self,
        organization_id: uuid.UUID,
        *,
        endpoint: str,
        headers: tuple[str, bytes, bytes] | None,
        clear_headers: bool,
        include_email: bool,
        include_actions: bool,
        enabled: bool,
    ) -> None:
        """`headers` None keeps the saved ones, unless `clear_headers`."""
        key_id, nonce, ct = headers or (None, None, None)
        await self._s.execute(
            text(
                """
                INSERT INTO otel_configs (organization_id, endpoint, headers_key_id,
                                          headers_nonce, headers_ciphertext, include_email,
                                          include_actions, enabled)
                VALUES (:org, :endpoint, :kid, :nonce, :ct, :email, :actions, :enabled)
                ON CONFLICT (organization_id) DO UPDATE SET
                    endpoint = EXCLUDED.endpoint,
                    headers_key_id = CASE WHEN :clear THEN NULL
                        ELSE COALESCE(EXCLUDED.headers_key_id, otel_configs.headers_key_id) END,
                    headers_nonce = CASE WHEN :clear THEN NULL
                        ELSE COALESCE(EXCLUDED.headers_nonce, otel_configs.headers_nonce) END,
                    headers_ciphertext = CASE WHEN :clear THEN NULL
                        ELSE COALESCE(EXCLUDED.headers_ciphertext,
                                      otel_configs.headers_ciphertext) END,
                    include_email = EXCLUDED.include_email,
                    include_actions = EXCLUDED.include_actions,
                    enabled = EXCLUDED.enabled, last_error = '', updated_at = now()
                """
            ),
            {
                "org": organization_id,
                "endpoint": endpoint,
                "kid": key_id,
                "nonce": nonce,
                "ct": ct,
                "clear": clear_headers,
                "email": include_email,
                "actions": include_actions,
                "enabled": enabled,
            },
        )

    async def delete(self, organization_id: uuid.UUID) -> None:
        await self._s.execute(
            text("DELETE FROM otel_configs WHERE organization_id = :org"), {"org": organization_id}
        )

    async def outcome(self, organization_id: uuid.UUID, *, error: str) -> None:
        await self._s.execute(
            text(
                "UPDATE otel_configs SET last_error = :error, "
                "last_sent_at = CASE WHEN :error = '' THEN now() ELSE last_sent_at END "
                "WHERE organization_id = :org"
            ),
            {"org": organization_id, "error": error[:500]},
        )

    async def cursor(self, organization_id: uuid.UUID) -> int:
        value = await self._s.scalar(
            text("SELECT audit_log_id FROM otel_cursors WHERE organization_id = :org"),
            {"org": organization_id},
        )
        if value is None:
            # Start from now: the trail before telemetry was set up is not news.
            latest = await self._s.scalar(
                text("SELECT coalesce(max(id), 0) FROM audit_logs WHERE organization_id = :org"),
                {"org": organization_id},
            )
            await self.set_cursor(organization_id, int(latest or 0))
            return int(latest or 0)
        return int(value)

    async def set_cursor(self, organization_id: uuid.UUID, audit_log_id: int) -> None:
        await self._s.execute(
            text(
                """
                INSERT INTO otel_cursors (organization_id, audit_log_id) VALUES (:org, :id)
                ON CONFLICT (organization_id) DO UPDATE SET audit_log_id = EXCLUDED.audit_log_id
                """
            ),
            {"org": organization_id, "id": audit_log_id},
        )
