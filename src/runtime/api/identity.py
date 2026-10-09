"""Who is calling — the signed-in member, when the runtime has members.

`current_member` is None with `RUNTIME_AUTH_MODE=none`: one person, no sign-in, as the
runtime always was. With `members` it is the member whose session cookie came with the
request, or a 401. `authenticate` is the dependency every bot-surface router is mounted
with (`api.app`), so no route there is reachable without a session — a route that
forgets to ask is still behind it. `require_admin` guards the operator console.
"""

from __future__ import annotations

from fastapi import HTTPException, Request, status

from runtime.domain.members import SESSION_COOKIE, Member
from runtime.runtime.members import MemberService
from runtime.settings import Settings


def members_mode(request: Request) -> bool:
    settings: Settings = request.app.state.settings
    return settings.auth_mode == "members"


def member_service(request: Request) -> MemberService:
    service = getattr(request.app.state, "members", None)
    if service is None:
        service = MemberService(request.app.state.uow, request.app.state.settings)
        request.app.state.members = service
    return service


def session_token(request: Request) -> str:
    return request.cookies.get(SESSION_COOKIE, "")


async def current_member(request: Request) -> Member | None:
    if not members_mode(request):
        return None
    cached = getattr(request.state, "member", None)
    if isinstance(cached, Member):
        return cached
    member = await member_service(request).member_for(session_token(request))
    if member is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="sign in first")
    request.state.member = member
    return member


async def authenticate(request: Request) -> None:
    await current_member(request)


async def require_admin(request: Request) -> None:
    member = await current_member(request)
    if member is not None and not member.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="the operator console is for owners and admins",
        )
