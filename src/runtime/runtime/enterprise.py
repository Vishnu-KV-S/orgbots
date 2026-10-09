"""An organization's admin settings: policies, team secrets, SCIM and telemetry export.

Every change here is an admin's (`actor` None is a runtime without members, where the
one person is everyone's admin), and every change is recorded in the organization's
audit trail in the same transaction (`org.audit`) — without the values: a secret's line
says its name, a telemetry line says the endpoint, never the headers.

Values are sealed with the credential cipher, with additional data binding each to its
organization (and a secret to its name), and no answer from here contains one.
"""

from __future__ import annotations

import json
import uuid
from typing import Any
from urllib.parse import urlparse

from runtime.domain.members import Member, NotAllowedError, new_token, token_hash
from runtime.domain.policies import Network, Policy, PolicyError, check_secret, clean_hosts
from runtime.gateway.team_secrets import seal_secret
from runtime.gateway.vault import load_cipher
from runtime.org.audit import record
from runtime.persistence.repositories.enterprise import AuditEventRow, OtelRow, SecretRow
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.settings import Settings

_LOCAL = frozenset({"localhost", "127.0.0.1", "::1"})


def otel_aad(organization_id: uuid.UUID) -> bytes:
    return b"otel-headers:" + organization_id.bytes


def _admin(actor: Member | None, what: str) -> None:
    if actor is not None and not actor.is_admin:
        raise NotAllowedError(f"only an owner or an admin can {what}")


class EnterpriseService:
    def __init__(self, uow_factory: UnitOfWorkFactory, settings: Settings) -> None:
        self._uow = uow_factory
        self._settings = settings

    # --- policies --------------------------------------------------------------------

    async def policy(self, organization_id: uuid.UUID) -> Policy:
        async with self._uow() as uow:
            return await uow.policies.get(organization_id)

    async def save_policy(
        self,
        actor: Member | None,
        organization_id: uuid.UUID,
        *,
        network: Network,
        hosts: list[str],
        require_review: bool,
        template_links: bool,
        members_add_apps: bool,
    ) -> Policy:
        _admin(actor, "change the organization's policies")
        allowed = clean_hosts(hosts)
        if network == "allowlist" and not allowed:
            raise PolicyError("an allowlist needs at least one host")
        policy = Policy(
            network=network,
            allowed_hosts=tuple(allowed),
            require_review=require_review,
            template_links=template_links,
            members_add_apps=members_add_apps,
        )
        async with self._uow.transaction() as uow:
            before = await uow.policies.get(organization_id)
            await uow.policies.save(organization_id, policy)
            changed = {
                key: {"from": _plain(getattr(before, key)), "to": _plain(getattr(policy, key))}
                for key in (
                    "network",
                    "allowed_hosts",
                    "require_review",
                    "template_links",
                    "members_add_apps",
                )
                if getattr(before, key) != getattr(policy, key)
            }
            if changed:
                await record(uow, organization_id, "policy.changed", actor=actor, detail=changed)
        return policy

    # --- team secrets ------------------------------------------------------------------

    async def secrets(self, organization_id: uuid.UUID) -> list[SecretRow]:
        async with self._uow() as uow:
            return await uow.team_secrets.for_organization(organization_id)

    async def put_secret(
        self, actor: Member | None, organization_id: uuid.UUID, name: str, value: str
    ) -> None:
        _admin(actor, "set team secrets")
        name = name.strip()
        async with self._uow.transaction() as uow:
            others = {
                s.name: s.bytes
                for s in await uow.team_secrets.for_organization(organization_id)
                if s.name != name
            }
            check_secret(name, value, others=others)
            sealed = seal_secret(self._settings, organization_id, name, value)
            await uow.team_secrets.put(
                organization_id,
                name,
                sealed=sealed,
                size=len(value.encode()),
                updated_by=actor.id if actor else None,
            )
            await record(uow, organization_id, "secret.set", actor=actor, target=name)

    async def delete_secret(
        self, actor: Member | None, organization_id: uuid.UUID, name: str
    ) -> bool:
        _admin(actor, "delete team secrets")
        async with self._uow.transaction() as uow:
            gone = await uow.team_secrets.delete(organization_id, name)
            if gone:
                await record(uow, organization_id, "secret.deleted", actor=actor, target=name)
        return gone

    # --- SCIM --------------------------------------------------------------------------

    async def scim_status(self, organization_id: uuid.UUID) -> dict[str, Any] | None:
        async with self._uow() as uow:
            return await uow.scim.status(organization_id)

    async def new_scim_token(self, actor: Member | None, organization_id: uuid.UUID) -> str:
        """A new provisioning token, replacing any other. Shown once."""
        _admin(actor, "set up provisioning")
        token = "scim_" + new_token()
        async with self._uow.transaction() as uow:
            await uow.scim.set_token(organization_id, token_hash(token), token[-4:])
            await record(uow, organization_id, "scim.token_made", actor=actor)
        return token

    async def revoke_scim(self, actor: Member | None, organization_id: uuid.UUID) -> None:
        _admin(actor, "turn off provisioning")
        async with self._uow.transaction() as uow:
            await uow.scim.delete(organization_id)
            await record(uow, organization_id, "scim.token_revoked", actor=actor)

    # --- telemetry export ----------------------------------------------------------------

    async def otel(self, organization_id: uuid.UUID) -> OtelRow | None:
        async with self._uow() as uow:
            return await uow.otel.get(organization_id)

    async def save_otel(
        self,
        actor: Member | None,
        organization_id: uuid.UUID,
        *,
        endpoint: str,
        headers: dict[str, str] | None,
        clear_headers: bool,
        include_email: bool,
        include_actions: bool,
        enabled: bool,
    ) -> OtelRow:
        """`headers` None keeps the saved ones (or `clear_headers` drops them)."""
        _admin(actor, "set up telemetry export")
        parsed = urlparse(endpoint.strip())
        if (
            not (
                parsed.scheme == "https" or (parsed.scheme == "http" and parsed.hostname in _LOCAL)
            )
            or not parsed.hostname
        ):
            raise PolicyError(
                "the collector must be an https:// address (or http:// on this machine)"
            )
        sealed = None
        if headers:
            for key in headers:
                if not key or any(c in key for c in " :\r\n"):
                    raise PolicyError(f"{key!r} is not a header name")
            sealed = load_cipher(self._settings).encrypt(
                json.dumps(headers), aad=otel_aad(organization_id)
            )
        async with self._uow.transaction() as uow:
            await uow.otel.save(
                organization_id,
                endpoint=endpoint.strip().rstrip("/"),
                headers=sealed,
                clear_headers=clear_headers,
                include_email=include_email,
                include_actions=include_actions,
                enabled=enabled,
            )
            await record(
                uow,
                organization_id,
                "telemetry.changed",
                actor=actor,
                target=endpoint.strip(),
                detail={"enabled": enabled, "include_email": include_email},
            )
            row = await uow.otel.get(organization_id)
        assert row is not None
        return row

    async def delete_otel(self, actor: Member | None, organization_id: uuid.UUID) -> None:
        _admin(actor, "turn off telemetry export")
        async with self._uow.transaction() as uow:
            await uow.otel.delete(organization_id)
            await record(uow, organization_id, "telemetry.removed", actor=actor)

    # --- the trail -----------------------------------------------------------------------

    async def events(
        self, actor: Member | None, organization_id: uuid.UUID, *, action: str = ""
    ) -> list[AuditEventRow]:
        _admin(actor, "read the audit log")
        async with self._uow() as uow:
            return await uow.org_audit.recent(organization_id, action=action)


def _plain(value: object) -> object:
    return list(value) if isinstance(value, tuple) else value
