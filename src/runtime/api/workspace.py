"""The person's side of the computer's terminal: the shared `/workspace`, and a shell.

A proxy to `runtime.computer`, like the screen endpoints in `api.bots`: list a folder,
download or upload a file, and run a command **in the sandbox** — the person's own
setup (installing a tool the bots will use, unpacking an archive). A person never runs
a "local" command through here: they have their own terminal on their own machine, and
an HTTP endpoint that ran host commands would be one more way in for anyone who can
reach this API, which has no authentication.

Uploads are base64 JSON for the same reason as the drive's: the UI's proxy passes
bodies through as text.

**Whose workspace.** A bot's commands and downloads use its browser profile's workspace
(`domain.members.computer_profile`), so `?bot_id=` picks that one — for anyone who can
see the bot; writing to a team bot's needs its owner or an admin. Without it, the
caller's own: theirs with members, the one shared workspace without.
"""

from __future__ import annotations

import base64
import binascii
from typing import Any
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from runtime.api.bots import _bot_or_404, _computer_call, _organization, _relay
from runtime.api.identity import current_member
from runtime.domain.members import can_edit, computer_profile

router = APIRouter(prefix="/v1/computer", tags=["bots"])

PERSON_SCREEN = "person"
"""The terminal's queue for the person's own commands — one at a time, like a bot's."""


class CommandBody(BaseModel):
    command: str = Field(min_length=1, max_length=20_000)
    timeout_s: float = Field(default=60.0, ge=1, le=300)


class PutFile(BaseModel):
    path: str = Field(min_length=1, max_length=400)
    data: str = Field(repr=False)


async def _profile(request: Request, bot_id: UUID | None, *, writing: bool = False) -> str:
    member = await current_member(request)
    if bot_id is None:
        return f"m-{member.id.hex}" if member is not None else ""
    bot = await _bot_or_404(request, bot_id)
    if writing and not can_edit(member, bot):
        raise HTTPException(
            status_code=403,
            detail=f"{bot.name} is shared with you; only its owner or an admin can change its "
            "workspace",
        )
    return computer_profile(bot)


@router.get("/workspace")
async def workspace(
    request: Request, path: str = "/workspace", bot_id: UUID | None = None
) -> dict[str, Any]:
    return _relay(
        await _computer_call(
            request,
            "GET",
            f"/workspace?path={_q(path)}",
            timeout_s=15.0,
            profile=await _profile(request, bot_id),
        )
    )


@router.get("/workspace/file")
async def workspace_file(request: Request, path: str, bot_id: UUID | None = None) -> Response:
    response = await _computer_call(
        request,
        "GET",
        f"/workspace/file?path={_q(path)}",
        profile=await _profile(request, bot_id),
    )
    if response is None or response.status_code >= 400:
        _relay(response)
    assert response is not None
    name = response.headers.get("x-path", path).rsplit("/", 1)[-1] or "file"
    return Response(
        content=response.content,
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{_q(name)}",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


@router.post("/workspace/file")
async def put_workspace_file(
    body: PutFile, request: Request, bot_id: UUID | None = None
) -> dict[str, Any]:
    try:
        base64.b64decode(body.data, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=422, detail="data is not base64") from exc
    return _relay(
        await _computer_call(
            request,
            "POST",
            "/workspace/file",
            json={"path": body.path, "data": body.data},
            profile=await _profile(request, bot_id, writing=True),
        )
    )


@router.post("/terminal")
async def run(body: CommandBody, request: Request, bot_id: UUID | None = None) -> dict[str, Any]:
    profile = await _profile(request, bot_id, writing=True)
    async with request.app.state.uow() as uow:
        policy = await uow.policies.get(await _organization(request))
    return _relay(
        await _computer_call(
            request,
            "POST",
            "/terminal/run",
            json={
                "screen_id": f"{PERSON_SCREEN}:{profile}" if profile else PERSON_SCREEN,
                "command": body.command,
                "timeout_s": body.timeout_s,
                "local": False,
                "profile": profile,
                # The organization's allowlist holds commands to no network; and a
                # person's own commands never get team secrets — `env` would show them.
                "network": policy.network == "open",
            },
            timeout_s=body.timeout_s + 15,
        )
    )


def _q(value: str) -> str:
    return quote(value, safe="/")
