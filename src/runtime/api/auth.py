"""Signing in and out — `/v1/auth`, reachable without a session (it is how one starts).

`GET /v1/auth/me` says whether the runtime has members and, if so, who this browser is.
A sign-in link (`/v1/auth/links/{token}`) is read, then spent by a POST that sets the
session cookie. Single sign-on starts with an email (`/sso/start` answers where to send
the browser) and comes back to `/sso/callback`, which sets the cookie and redirects into
the app — or to the sign-in page with the reason it failed.

The cookie is HttpOnly (no script reads it), SameSite=Lax (another site's form cannot
post with it) and Secure when the UI is served over https. Its value is never stored;
the database has its hash.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from runtime.api.identity import current_member, member_service, members_mode, session_token
from runtime.domain.members import SESSION_COOKIE, SESSION_TTL, Member, MemberError
from runtime.settings import Settings

router = APIRouter(prefix="/v1/auth", tags=["auth"])


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _need_members(request: Request) -> None:
    if not members_mode(request):
        raise HTTPException(status_code=404, detail="this runtime has no sign-in")


def member_view(member: Member) -> dict[str, Any]:
    return {
        "id": str(member.id),
        "email": member.email,
        "name": member.name,
        "role": member.role,
        "organization_id": str(member.organization_id),
    }


def _set_cookie(request: Request, response: Response, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(SESSION_TTL.total_seconds()),
        httponly=True,
        samesite="lax",
        secure=_settings(request).ui_url.startswith("https://"),
        path="/",
    )


class AcceptBody(BaseModel):
    name: str = Field(default="", max_length=80)


class SSOStartBody(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    return_to: str = Field(default="/", max_length=500)


@router.get("/me")
async def me(request: Request) -> dict[str, Any]:
    if not members_mode(request):
        return {"mode": "none", "member": None}
    try:
        member = await current_member(request)
    except HTTPException:
        member = None
    return {"mode": "members", "member": member_view(member) if member else None}


@router.get("/links/{token}")
async def link(token: str, request: Request) -> dict[str, Any]:
    _need_members(request)
    try:
        info = await member_service(request).link_info(token)
    except MemberError as exc:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail=str(exc)) from exc
    return {
        "kind": info.link.kind,
        "email": info.link.email,
        "role": info.link.role,
        "organization": info.organization,
        "joining": info.joining,
    }


@router.post("/links/{token}")
async def accept(
    token: str, body: AcceptBody, request: Request, response: Response
) -> dict[str, Any]:
    _need_members(request)
    try:
        signed = await member_service(request).accept(
            token, name=body.name, user_agent=request.headers.get("user-agent", "")
        )
    except MemberError as exc:
        raise HTTPException(status_code=status.HTTP_410_GONE, detail=str(exc)) from exc
    _set_cookie(request, response, signed.token)
    return {"member": member_view(signed.member)}


@router.post("/sso/start")
async def sso_start(body: SSOStartBody, request: Request) -> dict[str, Any]:
    _need_members(request)
    try:
        url = await member_service(request).sso_start(body.email, return_to=body.return_to)
    except MemberError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"redirect_url": url}


@router.get("/sso/callback")
async def sso_callback(
    request: Request, code: str = "", state: str = "", error: str = ""
) -> RedirectResponse:
    _need_members(request)

    def failed(reason: str) -> RedirectResponse:
        return RedirectResponse(f"/signin?error={quote(reason)}", status_code=303)

    if error:
        said = request.query_params.get("error_description") or error
        return failed(f"the identity provider said: {said[:200]}")
    if not code or not state:
        return failed("the identity provider sent the browser back without a sign-in")
    try:
        signed = await member_service(request).sso_finish(
            state, code, user_agent=request.headers.get("user-agent", "")
        )
    except MemberError as exc:
        return failed(str(exc))
    redirect = RedirectResponse(signed.return_to, status_code=303)
    _set_cookie(request, redirect, signed.token)
    return redirect


@router.post("/sign-out")
async def sign_out(request: Request, response: Response) -> dict[str, Any]:
    token = session_token(request)
    if token and members_mode(request):
        await member_service(request).sign_out(token)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"signed_out": True}
