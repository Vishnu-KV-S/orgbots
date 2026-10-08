"""The organization's admin settings — `/v1/admin`.

Everyone signed in may read the policy (it is the rules their bots work under); only
owners and admins change anything, read secrets' names, provisioning, telemetry or the
audit trail. Without members the one person is the admin. The rules are in
`domain.policies` and `runtime.runtime.enterprise`; this module only translates.

No response carries a secret's value, a SCIM token after the one that made it, or a
telemetry header — only that one is set.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from runtime.api.bots import _organization
from runtime.api.identity import current_member
from runtime.domain.members import MemberError, NotAllowedError
from runtime.domain.policies import Policy, PolicyError
from runtime.gateway.vault import VaultUnavailableError
from runtime.persistence.repositories.enterprise import AuditEventRow, OtelRow
from runtime.runtime.enterprise import EnterpriseService
from runtime.settings import Settings

router = APIRouter(prefix="/v1/admin", tags=["admin"])


def _service(request: Request) -> EnterpriseService:
    service = getattr(request.app.state, "enterprise", None)
    if service is None:
        service = EnterpriseService(request.app.state.uow, request.app.state.settings)
        request.app.state.enterprise = service
    return service


def _errors(exc: Exception) -> HTTPException:
    if isinstance(exc, NotAllowedError):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    if isinstance(exc, VaultUnavailableError):
        return HTTPException(status_code=503, detail=str(exc).splitlines()[0])
    return HTTPException(status_code=422, detail=str(exc))


_FAILURES = (MemberError, PolicyError, VaultUnavailableError)


async def _admin(request: Request) -> None:
    member = await current_member(request)
    if member is not None and not member.is_admin:
        raise HTTPException(status_code=403, detail="this is for owners and admins")


def policy_view(policy: Policy) -> dict[str, Any]:
    return {
        "network": policy.network,
        "allowed_hosts": list(policy.allowed_hosts),
        "require_review": policy.require_review,
        "template_links": policy.template_links,
        "members_add_apps": policy.members_add_apps,
    }


def _event_view(row: AuditEventRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "occurred_at": row.occurred_at.isoformat(),
        "actor": row.actor or ("the person" if row.actor_member_id is None else ""),
        "action": row.action,
        "target": row.target,
        "detail": row.detail,
    }


def _otel_view(row: OtelRow | None) -> dict[str, Any]:
    if row is None:
        return {"configured": False}
    return {
        "configured": True,
        "endpoint": row.endpoint,
        "has_headers": row.headers_ciphertext is not None,
        "include_email": row.include_email,
        "include_actions": row.include_actions,
        "enabled": row.enabled,
        "last_error": row.last_error,
        "last_sent_at": row.last_sent_at.isoformat() if row.last_sent_at else None,
    }


class PolicyBody(BaseModel):
    network: Literal["open", "allowlist"] = "open"
    allowed_hosts: list[str] = Field(default_factory=list, max_length=200)
    require_review: bool = False
    template_links: bool = True
    members_add_apps: bool = False


class SecretBody(BaseModel):
    value: str = Field(min_length=1, max_length=40_000, repr=False)


class OtelBody(BaseModel):
    endpoint: str = Field(min_length=8, max_length=500)
    headers: dict[str, str] | None = Field(default=None, repr=False)
    """Omitted keeps the saved headers; `clear_headers` drops them."""
    clear_headers: bool = False
    include_email: bool = False
    include_actions: bool = True
    enabled: bool = True


# --- policy ------------------------------------------------------------------------------


@router.get("/policy")
async def get_policy(request: Request) -> dict[str, Any]:
    org = await _organization(request)
    return policy_view(await _service(request).policy(org))


@router.put("/policy")
async def put_policy(body: PolicyBody, request: Request) -> dict[str, Any]:
    org = await _organization(request)
    try:
        policy = await _service(request).save_policy(
            await current_member(request),
            org,
            network=body.network,
            hosts=body.allowed_hosts,
            require_review=body.require_review,
            template_links=body.template_links,
            members_add_apps=body.members_add_apps,
        )
    except _FAILURES as exc:
        raise _errors(exc) from exc
    return policy_view(policy)


# --- team secrets ------------------------------------------------------------------------


@router.get("/secrets")
async def list_secrets(request: Request) -> dict[str, Any]:
    await _admin(request)
    rows = await _service(request).secrets(await _organization(request))
    return {
        "secrets": [
            {"name": r.name, "bytes": r.bytes, "updated_at": r.updated_at.isoformat()} for r in rows
        ]
    }


@router.put("/secrets/{name}")
async def put_secret(name: str, body: SecretBody, request: Request) -> dict[str, Any]:
    org = await _organization(request)
    try:
        await _service(request).put_secret(await current_member(request), org, name, body.value)
    except _FAILURES as exc:
        raise _errors(exc) from exc
    return {"name": name, "saved": True}


@router.delete("/secrets/{name}")
async def delete_secret(name: str, request: Request) -> dict[str, Any]:
    org = await _organization(request)
    try:
        gone = await _service(request).delete_secret(await current_member(request), org, name)
    except _FAILURES as exc:
        raise _errors(exc) from exc
    if not gone:
        raise HTTPException(status_code=404, detail=f"no secret {name}")
    return {"deleted": name}


# --- provisioning (SCIM) ---------------------------------------------------------------------


def _scim_base(request: Request) -> str:
    settings: Settings = request.app.state.settings
    base = settings.public_url.rstrip("/") or str(request.base_url).rstrip("/")
    return base + "/scim/v2"


@router.get("/scim")
async def scim_status(request: Request) -> dict[str, Any]:
    await _admin(request)
    found = await _service(request).scim_status(await _organization(request))
    return {
        "base_url": _scim_base(request),
        "configured": found is not None,
        "hint": found["hint"] if found else None,
        "last_used_at": found["last_used_at"].isoformat()
        if found and found["last_used_at"]
        else None,
    }


@router.post("/scim/token", status_code=status.HTTP_201_CREATED)
async def scim_token(request: Request) -> dict[str, Any]:
    org = await _organization(request)
    try:
        token = await _service(request).new_scim_token(await current_member(request), org)
    except _FAILURES as exc:
        raise _errors(exc) from exc
    return {"token": token, "base_url": _scim_base(request)}


@router.delete("/scim")
async def scim_revoke(request: Request) -> dict[str, Any]:
    org = await _organization(request)
    try:
        await _service(request).revoke_scim(await current_member(request), org)
    except _FAILURES as exc:
        raise _errors(exc) from exc
    return {"configured": False}


# --- telemetry export --------------------------------------------------------------------


@router.get("/telemetry")
async def get_telemetry(request: Request) -> dict[str, Any]:
    await _admin(request)
    return _otel_view(await _service(request).otel(await _organization(request)))


@router.put("/telemetry")
async def put_telemetry(body: OtelBody, request: Request) -> dict[str, Any]:
    org = await _organization(request)
    try:
        row = await _service(request).save_otel(
            await current_member(request),
            org,
            endpoint=body.endpoint,
            headers=body.headers,
            clear_headers=body.clear_headers,
            include_email=body.include_email,
            include_actions=body.include_actions,
            enabled=body.enabled,
        )
    except _FAILURES as exc:
        raise _errors(exc) from exc
    return _otel_view(row)


@router.delete("/telemetry")
async def delete_telemetry(request: Request) -> dict[str, Any]:
    org = await _organization(request)
    try:
        await _service(request).delete_otel(await current_member(request), org)
    except _FAILURES as exc:
        raise _errors(exc) from exc
    return _otel_view(None)


# --- the audit trail -----------------------------------------------------------------------


@router.get("/audit")
async def audit_log(request: Request, action: str = "") -> dict[str, Any]:
    org = await _organization(request)
    try:
        rows = await _service(request).events(
            await current_member(request), org, action=action[:60]
        )
    except _FAILURES as exc:
        raise _errors(exc) from exc
    return {"events": [_event_view(r) for r in rows]}
