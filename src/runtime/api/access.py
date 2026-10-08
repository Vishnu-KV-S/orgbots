"""Whose bot, and may they change it — one table, checked before any handler runs.

Every router under `/v1/bots` is mounted with `bot_access` (`api.app`). For a route
with a `{bot_id}` it loads the bot and answers 404 unless it is in the caller's
organization and theirs to see (`domain.members.can_see`); then, for a route that
changes what the bot *is*, 403 unless they may edit it (`can_edit`): its owner, or an
admin. Everything else — talking to it, approving a step once, answering a sign-in
card, watching or driving its screen, its team's files — is for anyone who can see it.

The table is by route template, so a new route under a listed prefix is covered by
the rule for its kind without anyone remembering to add a check. `group_access` does
the same for `/v1/groups/{group_id}`: a group is its creator's.

Without members every caller is the one person; the organization check still applies,
which also keeps a request naming one organization from reading another's bots.
"""

from __future__ import annotations

import re
import uuid

from fastapi import HTTPException, Request, status

from runtime.api.bots import _manager, _organization
from runtime.api.identity import current_member
from runtime.domain.members import can_edit, can_see
from runtime.runtime.bots import BotNotFoundError

# (methods, pattern on the route template after `/v1/bots/{bot_id}`)
SETUP_ROUTES: tuple[tuple[frozenset[str], re.Pattern[str]], ...] = tuple(
    (frozenset(methods.split()), re.compile(pattern))
    for methods, pattern in (
        # The profile, look, brief lock, sharing, Auto Review — and deleting it.
        ("PATCH DELETE", r"^$"),
        ("POST", r"^/brief/revisions/[^/]+/restore$"),
        # Its memory, rules and routines are what it is, for everyone who uses it.
        ("POST PATCH DELETE", r"^/memories(/.*)?$"),
        ("POST DELETE", r"^/rules(/.*)?$"),
        ("POST PATCH DELETE", r"^/routines(/.*)?$"),
        # Publishing it as a template.
        ("POST DELETE", r"^/template-links(/.*)?$"),
    )
)
_PREFIX = "/v1/bots/{bot_id}"


def is_setup(method: str, template: str) -> bool:
    if not template.startswith(_PREFIX):
        return False
    rest = template[len(_PREFIX) :]
    return any(method in methods and pattern.match(rest) for methods, pattern in SETUP_ROUTES)


def _template(request: Request) -> str:
    route = request.scope.get("route")
    return str(getattr(route, "path", ""))


async def bot_access(request: Request) -> None:
    raw = request.path_params.get("bot_id")
    if raw is None:
        return
    try:
        bot_id = uuid.UUID(str(raw))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"no bot {raw}") from exc
    try:
        bot = await _manager(request).get(bot_id)
    except BotNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"no bot {bot_id}") from exc
    member = await current_member(request)
    if bot.organization_id != await _organization(request) or not can_see(member, bot):
        raise HTTPException(status_code=404, detail=f"no bot {bot_id}")
    if is_setup(request.method, _template(request)) and not can_edit(member, bot):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"{bot.name} is shared with you; only its owner or an admin can change "
            "its setup",
        )


async def group_access(request: Request) -> None:
    raw = request.path_params.get("group_id")
    if raw is None:
        return
    try:
        group_id = uuid.UUID(str(raw))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"no group {raw}") from exc
    async with request.app.state.uow() as uow:
        group = await uow.groups.get(group_id)
    member = await current_member(request)
    if (
        group is None
        or group.organization_id != await _organization(request)
        or (member is not None and group.owner_member_id not in (None, member.id))
    ):
        raise HTTPException(status_code=404, detail=f"no group {group_id}")
