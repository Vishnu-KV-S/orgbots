"""The organization's people — `/v1/members`, for a runtime with members.

Everyone signed in sees who their teammates are (names show on what they said to team
bots). Owners and admins invite people, make sign-in links, change roles, suspend and
restore, and set up single sign-on; the rules for who may do which are
`domain.members`, enforced in `MemberService`, so this module only translates.

A link's token appears once, in the response that made it, as the full URL to send;
after that the database has only its hash. The SSO client secret is never returned —
only whether one is saved — and setting up SSO first reads the provider's
configuration, so an issuer that does not answer as itself is refused here, not
discovered by the next person trying to sign in.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from runtime.api.identity import current_member, member_service, members_mode
from runtime.domain.members import Member, MemberError, NotAllowedError, Role
from runtime.persistence.repositories.members import LinkRow, MemberRow, SSORow
from runtime.settings import Settings

router = APIRouter(prefix="/v1/members", tags=["members"])


async def _me(request: Request) -> Member:
    if not members_mode(request):
        raise HTTPException(status_code=404, detail="this runtime has no members")
    member = await current_member(request)
    assert member is not None
    return member


def _link_url(request: Request, token: str) -> str:
    settings: Settings = request.app.state.settings
    return f"{settings.ui_url.rstrip('/')}/signin?link={token}"


def _errors(exc: Exception) -> HTTPException:
    if isinstance(exc, NotAllowedError):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


def _member_view(row: MemberRow) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "email": row.email,
        "name": row.name,
        "role": row.role,
        "active": row.status == "active",
        "created_at": row.created_at.isoformat(),
        "last_seen_at": row.last_seen_at.isoformat() if row.last_seen_at else None,
    }


def _invite_view(row: LinkRow) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "email": row.email,
        "role": row.role,
        "created_at": row.created_at.isoformat(),
        "expires_at": row.expires_at.isoformat(),
    }


def _sso_view(request: Request, row: SSORow | None) -> dict[str, Any]:
    callback = member_service(request).callback_url
    if row is None:
        return {"configured": False, "callback_url": callback}
    return {
        "configured": True,
        "issuer": row.issuer,
        "client_id": row.client_id,
        "has_secret": row.secret_ciphertext is not None,
        "domains": row.domains,
        "auto_join": row.auto_join,
        "enabled": row.enabled,
        "callback_url": callback,
    }


class InviteBody(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    role: Role = "member"


class MemberBody(BaseModel):
    role: Role | None = None
    active: bool | None = None


class SSOBody(BaseModel):
    issuer: str = Field(min_length=8, max_length=500)
    client_id: str = Field(min_length=1, max_length=300)
    client_secret: str | None = Field(default=None, max_length=2_000)
    """Omitted keeps the saved one."""
    domains: list[str] = Field(min_length=1, max_length=20)
    auto_join: bool = False
    enabled: bool = True


@router.get("")
async def list_members(request: Request) -> dict[str, Any]:
    me = await _me(request)
    rows = await member_service(request).members(me.organization_id)
    return {"members": [_member_view(r) for r in rows], "me": str(me.id)}


@router.post("/invites", status_code=status.HTTP_201_CREATED)
async def invite(body: InviteBody, request: Request) -> dict[str, Any]:
    me = await _me(request)
    try:
        row, token = await member_service(request).invite(me, body.email, body.role)
    except MemberError as exc:
        raise _errors(exc) from exc
    return {**_invite_view(row), "link": _link_url(request, token)}


@router.get("/invites")
async def invites(request: Request) -> dict[str, Any]:
    me = await _me(request)
    try:
        rows = await member_service(request).open_invites(me)
    except MemberError as exc:
        raise _errors(exc) from exc
    return {"invites": [_invite_view(r) for r in rows]}


@router.delete("/invites/{link_id}")
async def revoke_invite(link_id: UUID, request: Request) -> dict[str, Any]:
    me = await _me(request)
    try:
        await member_service(request).revoke_invite(me, link_id)
    except MemberError as exc:
        raise _errors(exc) from exc
    return {"revoked": str(link_id)}


@router.patch("/{member_id}")
async def change(member_id: UUID, body: MemberBody, request: Request) -> dict[str, Any]:
    me = await _me(request)
    service = member_service(request)
    try:
        row = None
        if body.role is not None:
            row = await service.set_role(me, member_id, body.role)
        if body.active is not None:
            row = await service.set_active(me, member_id, body.active)
    except MemberError as exc:
        raise _errors(exc) from exc
    if row is None:
        raise HTTPException(status_code=422, detail="nothing to change")
    return _member_view(row)


@router.post("/{member_id}/sign-in-link", status_code=status.HTTP_201_CREATED)
async def sign_in_link(member_id: UUID, request: Request) -> dict[str, Any]:
    me = await _me(request)
    try:
        token = await member_service(request).sign_in_link(me, member_id)
    except MemberError as exc:
        raise _errors(exc) from exc
    return {"link": _link_url(request, token)}


@router.get("/sso")
async def get_sso(request: Request) -> dict[str, Any]:
    me = await _me(request)
    if not me.is_admin:
        raise HTTPException(status_code=403, detail="single sign-on is for owners and admins")
    return _sso_view(request, await member_service(request).sso(me.organization_id))


@router.put("/sso")
async def put_sso(body: SSOBody, request: Request) -> dict[str, Any]:
    me = await _me(request)
    try:
        row = await member_service(request).save_sso(
            me,
            issuer=body.issuer,
            client_id=body.client_id,
            client_secret=body.client_secret or None,
            domains=body.domains,
            auto_join=body.auto_join,
            enabled=body.enabled,
        )
    except MemberError as exc:
        raise _errors(exc) from exc
    return _sso_view(request, row)


@router.delete("/sso")
async def delete_sso(request: Request) -> dict[str, Any]:
    me = await _me(request)
    try:
        await member_service(request).delete_sso(me)
    except MemberError as exc:
        raise _errors(exc) from exc
    return _sso_view(request, None)
