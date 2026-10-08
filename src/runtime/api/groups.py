"""`/v1/groups` — group chats between the person and their bots, and reactions.

A person's message in a group starts the bots it names (or the lead) at once, through
`BotManager.post_group`, the same `RunService.start_run()` door a message to one bot
uses; what a bot then passes to a teammate is queued and started by the worker's wake
runner when that teammate is free. Everything a group shows is read from the database:
the conversation, who is a member, and which members are working right now.

Reactions — the person's lightweight acknowledgements — live here too, for both a
group's messages and a bot's own conversation, kept server-side so they survive a
browser and a device.
"""

from __future__ import annotations

import uuid
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from runtime.api.bots import _bot_or_404, _manager, _organization, _run_status, _uow, _working
from runtime.api.identity import current_member
from runtime.domain.groups import MAX_MEMBERS, MIN_MEMBERS
from runtime.org.groups import GroupError, GroupService
from runtime.persistence.repositories.groups import GroupMessageRow, GroupRow

router = APIRouter(prefix="/v1/groups", tags=["groups"])
reactions_router = APIRouter(prefix="/v1/bots", tags=["groups"])

EMOJI = frozenset({"👍", "👎", "❤️", "🎉", "✅", "👀", "😂", "🙏"})


def _service(request: Request) -> GroupService:
    service = getattr(request.app.state, "groups", None)
    if service is None:
        service = GroupService(_uow(request))
        request.app.state.groups = service
    return service


def _message_view(m: GroupMessageRow, reactions: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": str(m.id),
        "seq": m.seq,
        "group_id": str(m.group_id),
        "author_kind": m.author_kind,
        "author_bot_id": str(m.author_bot_id) if m.author_bot_id else None,
        "author_name": m.author_name,
        "content": m.content,
        "payload": m.payload,
        "thread_root": str(m.thread_root) if m.thread_root else None,
        "run_id": str(m.run_id) if m.run_id else None,
        "created_at": m.created_at.isoformat(),
        "reactions": reactions or [],
    }


async def _view(
    request: Request, g: GroupRow, last: GroupMessageRow | None = None
) -> dict[str, Any]:
    roster = await _service(request).roster(g.id)
    members = []
    async with _uow(request)() as uow:
        for bot_id, name, label in roster:
            bot = await uow.bots.get(bot_id)
            members.append(
                {
                    "id": str(bot_id),
                    "name": name,
                    "label": label,
                    "working": False
                    if bot is None
                    else _working(await _run_status(_uow(request), bot.last_run_id)),
                }
            )
    return {
        "id": str(g.id),
        "name": g.name,
        "lead_bot_id": str(g.lead_bot_id) if g.lead_bot_id else None,
        "members": members,
        "unread": g.unread,
        "created_at": g.created_at.isoformat(),
        "updated_at": g.updated_at.isoformat(),
        "last_message": _message_view(last) if last else None,
    }


async def _group_or_404(request: Request, group_id: UUID) -> GroupRow:
    org = await _organization(request)
    group = await _service(request).get(group_id)
    member = await current_member(request)
    if (
        group is None
        or group.organization_id != org
        or (member is not None and group.owner_member_id not in (None, member.id))
    ):
        raise HTTPException(status_code=404, detail=f"no group {group_id}")
    return group


async def _check_bots(request: Request, bots: list[UUID]) -> None:
    """A group is its creator's, so only bots they can see may be in it."""
    for bot_id in bots:
        try:
            await _bot_or_404(request, bot_id)
        except HTTPException as exc:
            raise HTTPException(status_code=422, detail=f"no bot {bot_id} to add") from exc


class GroupBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    members: list[UUID] = Field(min_length=MIN_MEMBERS, max_length=MAX_MEMBERS)
    lead: UUID | None = None


class GroupPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    members: list[UUID] | None = Field(default=None, min_length=MIN_MEMBERS, max_length=MAX_MEMBERS)
    lead: UUID | None = None


class PostBody(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)
    thread_root: UUID | None = None


class ReactBody(BaseModel):
    emoji: str = Field(min_length=1, max_length=8)
    on: bool = True


@router.get("")
async def list_groups(request: Request) -> dict[str, Any]:
    org = await _organization(request)
    member = await current_member(request)
    async with _uow(request)() as uow:
        groups = [
            g
            for g in await uow.groups.list_for(org)
            if member is None or g.owner_member_id in (None, member.id)
        ]
        last = await uow.groups.last_message_per_group(org)
    return {"groups": [await _view(request, g, last.get(g.id)) for g in groups]}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_group(body: GroupBody, request: Request) -> dict[str, Any]:
    org = await _organization(request)
    member = await current_member(request)
    await _check_bots(request, list(body.members))
    try:
        group = await _service(request).create(
            org,
            body.name,
            list(body.members),
            body.lead,
            owner_member_id=member.id if member else None,
        )
    except GroupError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return await _view(request, group)


@router.get("/{group_id}")
async def get_group(group_id: UUID, request: Request) -> dict[str, Any]:
    return await _view(request, await _group_or_404(request, group_id))


@router.patch("/{group_id}")
async def update_group(group_id: UUID, body: GroupPatch, request: Request) -> dict[str, Any]:
    group = await _group_or_404(request, group_id)
    if body.members is not None:
        await _check_bots(request, list(body.members))
    try:
        updated = await _service(request).update(
            group,
            name=body.name,
            members=list(body.members) if body.members is not None else None,
            lead=body.lead,
        )
    except GroupError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return await _view(request, updated)


@router.delete("/{group_id}")
async def delete_group(group_id: UUID, request: Request) -> dict[str, Any]:
    await _group_or_404(request, group_id)
    async with _uow(request).transaction() as uow:
        await uow.groups.soft_delete(group_id)
    return {"deleted": str(group_id)}


@router.get("/{group_id}/messages")
async def group_messages(group_id: UUID, request: Request, after: int = 0) -> dict[str, Any]:
    group = await _group_or_404(request, group_id)
    async with _uow(request)() as uow:
        rows = await uow.groups.messages(group.id, after_seq=after)
        reactions = await uow.groups.reactions([r.id for r in rows])
    view = await _view(request, group)
    return {
        "messages": [_message_view(m, reactions.get(m.id)) for m in rows],
        "working": [m["id"] for m in view["members"] if m["working"]],
    }


@router.post("/{group_id}/messages", status_code=status.HTTP_202_ACCEPTED)
async def post_message(group_id: UUID, body: PostBody, request: Request) -> dict[str, Any]:
    group = await _group_or_404(request, group_id)
    if body.thread_root is not None:
        root = await _service(request).message(body.thread_root)
        if root is None or root.group_id != group.id:
            raise HTTPException(status_code=422, detail="that thread is not in this group")
        if root.thread_root is not None:
            body.thread_root = root.thread_root
    message_id, sent = await _manager(request).post_group(
        group, body.text.strip(), thread_root=body.thread_root
    )
    return {
        "message_id": str(message_id),
        "runs": [
            {
                "run_id": str(s.run_id) if s.run_id else None,
                "admitted": s.admitted,
                "refusal_reason": s.refusal_reason,
            }
            for s in sent
        ],
    }


@router.post("/{group_id}/read")
async def mark_group_read(group_id: UUID, request: Request) -> dict[str, Any]:
    await _group_or_404(request, group_id)
    async with _uow(request).transaction() as uow:
        await uow.groups.update(group_id, unread=False)
    return {"id": str(group_id), "unread": False}


@router.post("/{group_id}/messages/{message_id}/reactions")
async def react_in_group(
    group_id: UUID, message_id: UUID, body: ReactBody, request: Request
) -> dict[str, Any]:
    await _group_or_404(request, group_id)
    found = await _service(request).message(message_id)
    if found is None or found.group_id != group_id:
        raise HTTPException(status_code=404, detail=f"no message {message_id}")
    return await _react(request, message_id, "group", body)


@reactions_router.post("/{bot_id}/messages/{message_id}/reactions")
async def react_to_bot_message(
    bot_id: UUID, message_id: UUID, body: ReactBody, request: Request
) -> dict[str, Any]:
    await _bot_or_404(request, bot_id)
    async with _uow(request)() as uow:
        rows = await uow.bots.messages(bot_id, after_seq=0)
    if not any(m.id == message_id for m in rows):
        raise HTTPException(status_code=404, detail=f"no message {message_id}")
    return await _react(request, message_id, "bot", body)


async def _react(
    request: Request, message_id: uuid.UUID, scope: str, body: ReactBody
) -> dict[str, Any]:
    if body.emoji not in EMOJI:
        raise HTTPException(status_code=422, detail=f"reactions are {' '.join(sorted(EMOJI))}")
    async with _uow(request).transaction() as uow:
        await uow.groups.react(message_id, body.emoji, scope, body.on)
        current = await uow.groups.reactions([message_id])
    return {"message_id": str(message_id), "reactions": current.get(message_id, [])}
