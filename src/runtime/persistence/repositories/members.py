"""Members, their sessions and sign-in links, and single sign-on. See migration 052."""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True, slots=True)
class MemberRow:
    id: uuid.UUID
    organization_id: uuid.UUID
    email: str
    name: str
    role: str
    status: str
    external_id: str | None
    created_at: dt.datetime
    last_seen_at: dt.datetime | None


@dataclass(frozen=True, slots=True)
class LinkRow:
    id: uuid.UUID
    organization_id: uuid.UUID
    kind: str
    email: str
    role: str
    member_id: uuid.UUID | None
    made_by: uuid.UUID | None
    created_at: dt.datetime
    expires_at: dt.datetime
    used_at: dt.datetime | None
    revoked_at: dt.datetime | None


@dataclass(frozen=True, slots=True)
class SSORow:
    organization_id: uuid.UUID
    issuer: str
    client_id: str
    secret_key_id: str | None
    secret_nonce: bytes | None
    secret_ciphertext: bytes | None
    domains: list[str]
    auto_join: bool
    enabled: bool
    updated_at: dt.datetime


@dataclass(frozen=True, slots=True)
class StateRow:
    state: str
    organization_id: uuid.UUID
    nonce: str
    verifier: str
    return_to: str
    created_at: dt.datetime


_MEMBER = "id, organization_id, email, name, role, status, external_id, created_at, last_seen_at"
_LINK = (
    "id, organization_id, kind, email, role, member_id, made_by, created_at, expires_at, "
    "used_at, revoked_at"
)
_SSO = (
    "organization_id, issuer, client_id, secret_key_id, secret_nonce, secret_ciphertext, "
    "domains, auto_join, enabled, updated_at"
)


def _member(row: Any) -> MemberRow:
    return MemberRow(*row)


def _sso(row: Any) -> SSORow:
    return SSORow(
        organization_id=row.organization_id,
        issuer=row.issuer,
        client_id=row.client_id,
        secret_key_id=row.secret_key_id,
        secret_nonce=row.secret_nonce,
        secret_ciphertext=row.secret_ciphertext,
        domains=list(row.domains or []),
        auto_join=bool(row.auto_join),
        enabled=bool(row.enabled),
        updated_at=row.updated_at,
    )


class MemberRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    # --- members ---------------------------------------------------------------------

    async def add(
        self,
        member_id: uuid.UUID,
        organization_id: uuid.UUID,
        *,
        email: str,
        name: str,
        role: str,
        external_id: str | None = None,
    ) -> bool:
        """False when the organization already has a member with that email."""
        done = await self._s.execute(
            text(
                """
                INSERT INTO members (id, organization_id, email, name, role, external_id)
                VALUES (:id, :org, :email, :name, :role, :ext)
                ON CONFLICT DO NOTHING
                """
            ),
            {
                "id": member_id,
                "org": organization_id,
                "email": email,
                "name": name,
                "role": role,
                "ext": external_id,
            },
        )
        return bool(getattr(done, "rowcount", 0))

    async def get(self, member_id: uuid.UUID) -> MemberRow | None:
        row = (
            await self._s.execute(
                text(f"SELECT {_MEMBER} FROM members WHERE id = :id"), {"id": member_id}
            )
        ).first()
        return _member(row) if row else None

    async def by_email(self, organization_id: uuid.UUID, email: str) -> MemberRow | None:
        row = (
            await self._s.execute(
                text(
                    f"SELECT {_MEMBER} FROM members "
                    "WHERE organization_id = :org AND lower(email) = lower(:email)"
                ),
                {"org": organization_id, "email": email},
            )
        ).first()
        return _member(row) if row else None

    async def for_organization(self, organization_id: uuid.UUID) -> list[MemberRow]:
        rows = await self._s.execute(
            text(
                f"SELECT {_MEMBER} FROM members WHERE organization_id = :org "
                "ORDER BY status, lower(coalesce(nullif(name, ''), email))"
            ),
            {"org": organization_id},
        )
        return [_member(r) for r in rows]

    async def owners(self, organization_id: uuid.UUID) -> int:
        count = await self._s.scalar(
            text(
                "SELECT count(*) FROM members WHERE organization_id = :org "
                "AND role = 'owner' AND status = 'active'"
            ),
            {"org": organization_id},
        )
        return int(count or 0)

    async def organization_name(self, organization_id: uuid.UUID) -> str:
        name = await self._s.scalar(
            text("SELECT name FROM organizations WHERE id = :id"), {"id": organization_id}
        )
        return str(name or "")

    async def count(self) -> int:
        return int(await self._s.scalar(text("SELECT count(*) FROM members")) or 0)

    async def update(self, member_id: uuid.UUID, fields: dict[str, Any]) -> None:
        unknown = set(fields) - {"name", "role", "status", "email", "external_id"}
        if unknown:
            raise ValueError(f"not editable: {sorted(unknown)}")
        if not fields:
            return
        sets = ", ".join(f"{k} = :{k}" for k in fields)
        await self._s.execute(
            text(f"UPDATE members SET {sets} WHERE id = :id"), {**fields, "id": member_id}
        )

    # --- sessions ----------------------------------------------------------------------

    async def add_session(
        self,
        session_id: uuid.UUID,
        member_id: uuid.UUID,
        token_hash: bytes,
        *,
        expires_at: dt.datetime,
        user_agent: str,
    ) -> None:
        await self._s.execute(
            text(
                """
                INSERT INTO member_sessions (id, member_id, token_hash, expires_at, user_agent)
                VALUES (:id, :member, :hash, :expires, :ua)
                """
            ),
            {
                "id": session_id,
                "member": member_id,
                "hash": token_hash,
                "expires": expires_at,
                "ua": user_agent[:300],
            },
        )

    async def session_member(self, token_hash: bytes) -> MemberRow | None:
        """The active member a live session belongs to, and touch both."""
        row = (
            await self._s.execute(
                text(
                    f"""
                    UPDATE member_sessions s SET last_used_at = now()
                      FROM members m
                     WHERE s.token_hash = :hash AND s.expires_at > now()
                       AND m.id = s.member_id AND m.status = 'active'
                    RETURNING {", ".join("m." + c.strip() for c in _MEMBER.split(","))}
                    """
                ),
                {"hash": token_hash},
            )
        ).first()
        if row is None:
            return None
        await self._s.execute(
            text(
                "UPDATE members SET last_seen_at = now() WHERE id = :id "
                "AND (last_seen_at IS NULL OR last_seen_at < now() - interval '5 minutes')"
            ),
            {"id": row.id},
        )
        return _member(row)

    async def drop_session(self, token_hash: bytes) -> None:
        await self._s.execute(
            text("DELETE FROM member_sessions WHERE token_hash = :hash"), {"hash": token_hash}
        )

    async def drop_sessions(self, member_id: uuid.UUID) -> None:
        await self._s.execute(
            text("DELETE FROM member_sessions WHERE member_id = :m"), {"m": member_id}
        )

    # --- sign-in links -------------------------------------------------------------------

    async def add_link(
        self,
        link_id: uuid.UUID,
        organization_id: uuid.UUID,
        *,
        kind: str,
        email: str,
        role: str,
        member_id: uuid.UUID | None,
        made_by: uuid.UUID | None,
        token_hash: bytes,
        expires_at: dt.datetime,
    ) -> None:
        await self._s.execute(
            text(
                """
                INSERT INTO member_links (id, organization_id, kind, email, role, member_id,
                                          made_by, token_hash, expires_at)
                VALUES (:id, :org, :kind, :email, :role, :member, :by, :hash, :expires)
                """
            ),
            {
                "id": link_id,
                "org": organization_id,
                "kind": kind,
                "email": email,
                "role": role,
                "member": member_id,
                "by": made_by,
                "hash": token_hash,
                "expires": expires_at,
            },
        )

    async def link(self, token_hash: bytes) -> LinkRow | None:
        row = (
            await self._s.execute(
                text(f"SELECT {_LINK} FROM member_links WHERE token_hash = :hash"),
                {"hash": token_hash},
            )
        ).first()
        return LinkRow(*row) if row else None

    async def use_link(self, link_id: uuid.UUID) -> bool:
        """Spend a link. False when it was already used, revoked, or has expired."""
        done = await self._s.execute(
            text(
                "UPDATE member_links SET used_at = now() WHERE id = :id AND used_at IS NULL "
                "AND revoked_at IS NULL AND expires_at > now()"
            ),
            {"id": link_id},
        )
        return bool(getattr(done, "rowcount", 0))

    async def open_invites(self, organization_id: uuid.UUID) -> list[LinkRow]:
        rows = await self._s.execute(
            text(
                f"SELECT {_LINK} FROM member_links WHERE organization_id = :org "
                "AND kind = 'invite' AND used_at IS NULL AND revoked_at IS NULL "
                "AND expires_at > now() ORDER BY created_at DESC"
            ),
            {"org": organization_id},
        )
        return [LinkRow(*r) for r in rows]

    async def revoke_link(self, organization_id: uuid.UUID, link_id: uuid.UUID) -> bool:
        done = await self._s.execute(
            text(
                "UPDATE member_links SET revoked_at = now() WHERE id = :id "
                "AND organization_id = :org AND used_at IS NULL AND revoked_at IS NULL"
            ),
            {"id": link_id, "org": organization_id},
        )
        return bool(getattr(done, "rowcount", 0))

    async def revoke_links_for(self, organization_id: uuid.UUID, email: str) -> None:
        await self._s.execute(
            text(
                "UPDATE member_links SET revoked_at = now() WHERE organization_id = :org "
                "AND lower(email) = lower(:email) AND used_at IS NULL AND revoked_at IS NULL"
            ),
            {"org": organization_id, "email": email},
        )

    # --- single sign-on ------------------------------------------------------------------

    async def sso(self, organization_id: uuid.UUID) -> SSORow | None:
        row = (
            await self._s.execute(
                text(f"SELECT {_SSO} FROM sso_configs WHERE organization_id = :org"),
                {"org": organization_id},
            )
        ).first()
        return _sso(row) if row else None

    async def sso_for_domain(self, domain: str) -> SSORow | None:
        row = (
            await self._s.execute(
                text(
                    f"SELECT {_SSO} FROM sso_configs WHERE enabled "
                    "AND lower(:domain) = ANY(domains) ORDER BY updated_at DESC LIMIT 1"
                ),
                {"domain": domain},
            )
        ).first()
        return _sso(row) if row else None

    async def save_sso(
        self,
        organization_id: uuid.UUID,
        *,
        issuer: str,
        client_id: str,
        secret: tuple[str, bytes, bytes] | None,
        domains: list[str],
        auto_join: bool,
        enabled: bool,
    ) -> None:
        """`secret` None keeps the one already saved."""
        key_id, nonce, ciphertext = secret or (None, None, None)
        await self._s.execute(
            text(
                """
                INSERT INTO sso_configs (organization_id, issuer, client_id, secret_key_id,
                                         secret_nonce, secret_ciphertext, domains, auto_join,
                                         enabled)
                VALUES (:org, :issuer, :client, :kid, :nonce, :ct, :domains, :auto, :enabled)
                ON CONFLICT (organization_id) DO UPDATE SET
                    issuer = EXCLUDED.issuer, client_id = EXCLUDED.client_id,
                    secret_key_id = COALESCE(EXCLUDED.secret_key_id, sso_configs.secret_key_id),
                    secret_nonce = COALESCE(EXCLUDED.secret_nonce, sso_configs.secret_nonce),
                    secret_ciphertext = COALESCE(EXCLUDED.secret_ciphertext,
                                                 sso_configs.secret_ciphertext),
                    domains = EXCLUDED.domains, auto_join = EXCLUDED.auto_join,
                    enabled = EXCLUDED.enabled, updated_at = now()
                """
            ),
            {
                "org": organization_id,
                "issuer": issuer,
                "client": client_id,
                "kid": key_id,
                "nonce": nonce,
                "ct": ciphertext,
                "domains": domains,
                "auto": auto_join,
                "enabled": enabled,
            },
        )

    async def delete_sso(self, organization_id: uuid.UUID) -> None:
        await self._s.execute(
            text("DELETE FROM sso_configs WHERE organization_id = :org"), {"org": organization_id}
        )

    async def add_state(
        self, state: str, organization_id: uuid.UUID, *, nonce: str, verifier: str, return_to: str
    ) -> None:
        await self._s.execute(
            text(
                """
                INSERT INTO oidc_states (state, organization_id, nonce, verifier, return_to)
                VALUES (:state, :org, :nonce, :verifier, :return_to)
                """
            ),
            {
                "state": state,
                "org": organization_id,
                "nonce": nonce,
                "verifier": verifier,
                "return_to": return_to,
            },
        )
        # Sign-ins nobody finished.
        await self._s.execute(
            text("DELETE FROM oidc_states WHERE created_at < now() - interval '1 hour'")
        )

    async def take_state(self, state: str) -> StateRow | None:
        """A sign-in in flight, removed as it is read: a state is good once."""
        row = (
            await self._s.execute(
                text(
                    "DELETE FROM oidc_states WHERE state = :state RETURNING state, "
                    "organization_id, nonce, verifier, return_to, created_at"
                ),
                {"state": state},
            )
        ).first()
        return StateRow(*row) if row else None
