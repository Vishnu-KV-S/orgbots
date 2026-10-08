"""Tag @bot on X — `/v1/x`.

Everyone signed in sees whether the organization has an X account, links their own X
account (a one-time code they post from it) and chooses which of their bots takes
their tags. Owners and admins connect the account itself; its tokens are sealed and
never returned. The rules are `domain.x`; the poller that acts on tags is
`runtime.runtime.x_tags`.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from runtime.api.bots import _bot_or_404, _organization
from runtime.api.identity import current_member
from runtime.domain.members import NotAllowedError
from runtime.domain.x import XError
from runtime.gateway.vault import VaultUnavailableError
from runtime.runtime.x_tags import XService

router = APIRouter(prefix="/v1/x", tags=["x"])


def _service(request: Request) -> XService:
    service = getattr(request.app.state, "x", None)
    if service is None:
        service = XService(request.app.state.uow, request.app.state.settings)
        request.app.state.x = service
    return service


def _errors(exc: Exception) -> HTTPException:
    if isinstance(exc, NotAllowedError):
        return HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc))
    if isinstance(exc, VaultUnavailableError):
        return HTTPException(status_code=503, detail=str(exc).splitlines()[0])
    return HTTPException(status_code=422, detail=str(exc))


_FAILURES = (XError, NotAllowedError, VaultUnavailableError)


class AccountBody(BaseModel):
    handle: str = Field(min_length=1, max_length=16)
    read_token: str | None = Field(default=None, max_length=2_000, repr=False)
    post_token: str | None = Field(default=None, max_length=2_000, repr=False)
    clear_post: bool = False
    enabled: bool = True


class LinkBody(BaseModel):
    bot_id: UUID


@router.get("")
async def x_status(request: Request) -> dict[str, Any]:
    org = await _organization(request)
    member = await current_member(request)
    service = _service(request)
    status_ = await service.status(org, member)
    out: dict[str, Any] = {
        "account": status_.account,
        "link": {
            "handle": status_.link.handle,
            "bot_id": str(status_.link.bot_id) if status_.link.bot_id else None,
        }
        if status_.link
        else None,
    }
    if member is None or member.is_admin:
        account = await service.account(org)
        if account is not None:
            async with request.app.state.uow() as uow:
                recent = await uow.x.recent(org)
            out["admin"] = {
                "enabled": account.enabled,
                "can_reply": account.post_key_id is not None,
                "last_error": account.last_error,
                "last_polled_at": account.last_polled_at.isoformat()
                if account.last_polled_at
                else None,
                "recent": [
                    {
                        "post_id": r["post_id"],
                        "author": r["author_handle"],
                        "outcome": r["outcome"],
                        "note": r["note"],
                        "at": r["created_at"].isoformat(),
                    }
                    for r in recent
                ],
            }
    return out


@router.put("/account")
async def connect(body: AccountBody, request: Request) -> dict[str, Any]:
    org = await _organization(request)
    try:
        row = await _service(request).connect(
            await current_member(request),
            org,
            handle=body.handle,
            read_token=body.read_token or None,
            post_token=body.post_token or None,
            clear_post=body.clear_post,
            enabled=body.enabled,
        )
    except _FAILURES as exc:
        raise _errors(exc) from exc
    return {"account": row.handle, "can_reply": row.post_key_id is not None}


@router.delete("/account")
async def disconnect(request: Request) -> dict[str, Any]:
    org = await _organization(request)
    try:
        await _service(request).disconnect(await current_member(request), org)
    except _FAILURES as exc:
        raise _errors(exc) from exc
    return {"account": None}


@router.post("/link/code", status_code=status.HTTP_201_CREATED)
async def link_code(request: Request) -> dict[str, Any]:
    org = await _organization(request)
    service = _service(request)
    try:
        code = await service.new_code(org, await current_member(request))
    except _FAILURES as exc:
        raise _errors(exc) from exc
    account = await service.account(org)
    handle = account.handle if account else ""
    return {"code": code, "post": f"@{handle} link {code}", "account": handle}


@router.patch("/link")
async def choose_bot(body: LinkBody, request: Request) -> dict[str, Any]:
    org = await _organization(request)
    bot = await _bot_or_404(request, body.bot_id)
    try:
        await _service(request).choose_bot(org, await current_member(request), bot)
    except _FAILURES as exc:
        raise _errors(exc) from exc
    return {"bot_id": str(bot.id)}


@router.delete("/link")
async def unlink(request: Request) -> dict[str, Any]:
    org = await _organization(request)
    await _service(request).unlink(org, await current_member(request))
    return {"link": None}
