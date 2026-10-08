"""`/v1/bots` — the surface the bot UI talks to.

Writes go through `BotManager`, and every run it creates goes through
`RunService.start_run()`: a message typed into the chat box gets the same admission,
budget, kill switch and audit as any run in the company console. This router is a
screen in front of that door, not a second door.

The computer endpoints are a proxy to `runtime.computer`, for watching a bot's screen
and taking control of it. They never call `act` — a person's input goes through
`/input`, which the computer refuses unless the person holds the screen, and a bot's
actions go only through the gateway.

**No authentication**, like `/v1/control`. Fine on a local devstack; anything that
can reach this can drive a browser that may be signed in to real accounts.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field

from runtime.api.errors import http_errors
from runtime.domain.enums import LIVE_RUN_STATUSES, RunStatus
from runtime.domain.ids import OrganizationId, RunId
from runtime.persistence.repositories.bots import BotMessageRow, BotRow
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.runtime.bots import BotManager, BotNotFoundError, PendingNotFoundError
from runtime.settings import Settings

router = APIRouter(prefix="/v1/bots", tags=["bots"])
computer_router = APIRouter(prefix="/v1/computer", tags=["bots"])

ORG_HEADER = "X-Organization-Id"


# --- wiring ---------------------------------------------------------------------------


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _uow(request: Request) -> UnitOfWorkFactory:
    factory: UnitOfWorkFactory = request.app.state.uow
    return factory


def _manager(request: Request) -> BotManager:
    manager = getattr(request.app.state, "bots", None)
    if manager is None:
        manager = BotManager(_uow(request), request.app.state.service)
        request.app.state.bots = manager
    return manager


async def _organization(request: Request) -> OrganizationId:
    """The header if given, else the personal organization — created on first use."""
    raw = request.headers.get(ORG_HEADER)
    settings = _settings(request)
    try:
        org = OrganizationId(UUID(raw or settings.bots_organization_id))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"{ORG_HEADER} must be a UUID") from exc
    ready: set[str] = getattr(request.app.state, "bot_orgs_ready", set())
    if str(org) not in ready:
        await _manager(request).ensure_organization(org, settings.bots_organization_name)
        ready.add(str(org))
        request.app.state.bot_orgs_ready = ready
    return org


async def _bot_or_404(request: Request, bot_id: UUID) -> BotRow:
    try:
        return await _manager(request).get(bot_id)
    except BotNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"no bot {bot_id}") from exc


# --- views ----------------------------------------------------------------------------


async def _run_status(uow_factory: UnitOfWorkFactory, run_id: uuid.UUID | None) -> str | None:
    if run_id is None:
        return None
    async with uow_factory() as uow:
        run = await uow.runs.get(RunId(run_id))
    return None if run is None else str(run.status)


def _working(run_status: str | None) -> bool:
    if run_status is None:
        return False
    try:
        return RunStatus(run_status) in LIVE_RUN_STATUSES
    except ValueError:
        return False


def _bot_view(
    bot: BotRow, *, run_status: str | None = None, last: BotMessageRow | None = None
) -> dict[str, Any]:
    return {
        "id": str(bot.id),
        "actor_name": bot.actor_name,
        "name": bot.name,
        "label": bot.label,
        "description": bot.description,
        "instructions": bot.instructions,
        "avatar": bot.avatar,
        "memory": bot.memory,
        "pinned": bot.pinned,
        "hidden": bot.hidden,
        "unread": bot.unread,
        "needs_attention": bot.needs_attention,
        "working": _working(run_status),
        "run_status": run_status,
        "last_run_id": str(bot.last_run_id) if bot.last_run_id else None,
        "duplicated_from": str(bot.duplicated_from) if bot.duplicated_from else None,
        "created_at": bot.created_at.isoformat(),
        "updated_at": bot.updated_at.isoformat(),
        "last_message": _message_view(last) if last else None,
    }


def _message_view(m: BotMessageRow) -> dict[str, Any]:
    return {
        "id": str(m.id),
        "seq": m.seq,
        "bot_id": str(m.bot_id),
        "role": m.role,
        "content": m.content,
        "payload": m.payload,
        "run_id": str(m.run_id) if m.run_id else None,
        "reply_to": str(m.reply_to) if m.reply_to else None,
        "created_at": m.created_at.isoformat(),
    }


# --- bodies ---------------------------------------------------------------------------


class CreateBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    label: str = Field(default="", max_length=80)
    description: str = Field(default="", max_length=2_000)
    instructions: str = Field(default="", max_length=8_000)
    avatar: str = Field(default="", max_length=16)


class UpdateBody(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    label: str | None = Field(default=None, max_length=80)
    description: str | None = Field(default=None, max_length=2_000)
    instructions: str | None = Field(default=None, max_length=8_000)
    avatar: str | None = Field(default=None, max_length=16)
    memory: str | None = Field(default=None, max_length=4_000)
    pinned: bool | None = None
    hidden: bool | None = None


class MessageBody(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)
    reply_to: UUID | None = None


class DecisionBody(BaseModel):
    decision: Literal["once", "always", "deny"]


class RuleBody(BaseModel):
    action_type: str = Field(min_length=1, max_length=32)
    host: str = Field(default="", max_length=253)
    decision: Literal["ask", "allow"]


class ReadBody(BaseModel):
    unread: bool = False


class ControlBody(BaseModel):
    controller: Literal["bot", "human"]


class InputBody(BaseModel):
    kind: Literal["click", "type", "key", "scroll", "navigate", "back", "forward", "reload"]
    x: float | None = None
    y: float | None = None
    text: str | None = Field(default=None, max_length=4_000)
    key: str | None = Field(default=None, max_length=64)
    dy: float | None = None
    url: str | None = Field(default=None, max_length=2_048)


# --- bots -----------------------------------------------------------------------------


@router.get("")
async def list_bots(request: Request) -> dict[str, Any]:
    org = await _organization(request)
    uow_factory = _uow(request)
    async with uow_factory() as uow:
        bots = await uow.bots.list_for(org)
        last = await uow.bots.last_message_per_bot(org)
        statuses: dict[uuid.UUID, str] = {}
        for bot in bots:
            if bot.last_run_id is not None:
                run = await uow.runs.get(RunId(bot.last_run_id))
                if run is not None:
                    statuses[bot.id] = str(run.status)
    return {
        "organization_id": str(org),
        "bots": [_bot_view(b, run_status=statuses.get(b.id), last=last.get(b.id)) for b in bots],
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_bot(body: CreateBody, request: Request) -> dict[str, Any]:
    org = await _organization(request)
    with http_errors():
        bot = await _manager(request).create(org, **body.model_dump())
    return _bot_view(bot)


@router.get("/search")
async def search(request: Request, q: str = Query(min_length=1, max_length=200)) -> dict[str, Any]:
    org = await _organization(request)
    async with _uow(request)() as uow:
        bots = {b.id: b for b in await uow.bots.list_for(org)}
        hits = await uow.bots.search(org, q)
    needle = q.lower()
    matching_bots = [
        _bot_view(b)
        for b in bots.values()
        if needle in f"{b.name} {b.label} {b.description}".lower()
    ]
    return {
        "bots": matching_bots,
        "messages": [
            {**_message_view(m), "bot_name": bots[m.bot_id].name} for m in hits if m.bot_id in bots
        ],
    }


@router.get("/{bot_id}")
async def get_bot(bot_id: UUID, request: Request) -> dict[str, Any]:
    bot = await _bot_or_404(request, bot_id)
    return _bot_view(bot, run_status=await _run_status(_uow(request), bot.last_run_id))


@router.patch("/{bot_id}")
async def update_bot(bot_id: UUID, body: UpdateBody, request: Request) -> dict[str, Any]:
    await _bot_or_404(request, bot_id)
    fields = body.model_dump(exclude_none=True)
    bot = await _manager(request).update(bot_id, fields)
    return _bot_view(bot, run_status=await _run_status(_uow(request), bot.last_run_id))


@router.post("/{bot_id}/duplicate", status_code=status.HTTP_201_CREATED)
async def duplicate_bot(bot_id: UUID, request: Request) -> dict[str, Any]:
    await _bot_or_404(request, bot_id)
    with http_errors():
        bot = await _manager(request).duplicate(bot_id)
    return _bot_view(bot)


@router.delete("/{bot_id}")
async def delete_bot(bot_id: UUID, request: Request) -> dict[str, Any]:
    await _bot_or_404(request, bot_id)
    await _manager(request).delete(bot_id)
    await _computer_call(request, "DELETE", f"/screens/{bot_id}", quiet=True)
    return {"deleted": str(bot_id)}


@router.post("/{bot_id}/read")
async def mark_read(bot_id: UUID, body: ReadBody, request: Request) -> dict[str, Any]:
    await _bot_or_404(request, bot_id)
    await _manager(request).mark_read(bot_id, body.unread)
    return {"id": str(bot_id), "unread": body.unread}


@router.post("/{bot_id}/stop")
async def stop_bot(bot_id: UUID, request: Request) -> dict[str, Any]:
    await _bot_or_404(request, bot_id)
    await _manager(request).stop(bot_id)
    return {"id": str(bot_id), "stop_requested": True}


# --- conversation ---------------------------------------------------------------------


@router.get("/{bot_id}/messages")
async def messages(
    bot_id: UUID, request: Request, after: int = Query(default=0, ge=0)
) -> dict[str, Any]:
    bot = await _bot_or_404(request, bot_id)
    async with _uow(request)() as uow:
        rows = await uow.bots.messages(bot_id, after_seq=after)
        pending = await uow.bots.live_pending(bot_id)
    run_status = await _run_status(_uow(request), bot.last_run_id)
    return {
        "messages": [_message_view(m) for m in rows],
        "pending": [str(p.id) for p in pending],
        "working": _working(run_status),
        "run_status": run_status,
    }


@router.post("/{bot_id}/messages", status_code=status.HTTP_202_ACCEPTED)
async def send(bot_id: UUID, body: MessageBody, request: Request) -> dict[str, Any]:
    await _bot_or_404(request, bot_id)
    with http_errors():
        sent = await _manager(request).send(bot_id, body.text.strip(), reply_to=body.reply_to)
    return {
        "message_id": str(sent.message_id),
        "run_id": str(sent.run_id) if sent.run_id else None,
        "admitted": sent.admitted,
        "refusal_reason": sent.refusal_reason,
    }


@router.post("/{bot_id}/pending/{pending_id}", status_code=status.HTTP_202_ACCEPTED)
async def decide(
    bot_id: UUID, pending_id: UUID, body: DecisionBody, request: Request
) -> dict[str, Any]:
    await _bot_or_404(request, bot_id)
    try:
        with http_errors():
            sent = await _manager(request).decide(bot_id, pending_id, body.decision)
    except PendingNotFoundError as exc:
        raise HTTPException(status_code=409, detail=f"nothing pending: {exc}") from exc
    return {
        "run_id": str(sent.run_id) if sent.run_id else None,
        "admitted": sent.admitted,
        "refusal_reason": sent.refusal_reason,
    }


# --- rules ----------------------------------------------------------------------------


@router.get("/{bot_id}/rules")
async def rules(bot_id: UUID, request: Request) -> dict[str, Any]:
    await _bot_or_404(request, bot_id)
    async with _uow(request)() as uow:
        rows = await uow.bots.rules(bot_id)
    return {
        "rules": [
            {
                "id": str(r.id),
                "action_type": r.action_type,
                "host": r.host,
                "decision": r.decision,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]
    }


@router.post("/{bot_id}/rules", status_code=status.HTTP_201_CREATED)
async def put_rule(bot_id: UUID, body: RuleBody, request: Request) -> dict[str, Any]:
    await _bot_or_404(request, bot_id)
    async with _uow(request).transaction() as uow:
        rule_id = await uow.bots.put_rule(
            bot_id, body.action_type, body.host.strip().lower(), body.decision
        )
    return {"id": str(rule_id)}


@router.delete("/{bot_id}/rules/{rule_id}")
async def delete_rule(bot_id: UUID, rule_id: UUID, request: Request) -> dict[str, Any]:
    await _bot_or_404(request, bot_id)
    async with _uow(request).transaction() as uow:
        await uow.bots.delete_rule(bot_id, rule_id)
    return {"deleted": str(rule_id)}


# --- the computer ---------------------------------------------------------------------


async def _computer_call(
    request: Request,
    method: str,
    path: str,
    *,
    json: dict[str, Any] | None = None,
    quiet: bool = False,
    timeout_s: float = 30.0,
) -> httpx.Response | None:
    base = _settings(request).computer_url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            return await client.request(method, f"{base}{path}", json=json)
    except httpx.TransportError as exc:
        if quiet:
            return None
        raise HTTPException(
            status_code=503,
            detail=f"the computer at {base} is not reachable; start it with "
            "`python -m runtime.computer.main`",
        ) from exc


def _relay(response: httpx.Response | None) -> dict[str, Any]:
    if response is None:
        raise HTTPException(status_code=503, detail="the computer is not reachable")
    try:
        body = response.json()
    except ValueError:
        body = {"detail": response.text[:300]}
    if response.status_code >= 400:
        raise HTTPException(status_code=response.status_code, detail=body.get("detail", body))
    return dict(body)


@router.get("/{bot_id}/computer/screenshot")
async def screenshot(bot_id: UUID, request: Request, quality: int = 60) -> Response:
    await _bot_or_404(request, bot_id)
    response = await _computer_call(
        request, "GET", f"/screens/{bot_id}/screenshot?quality={quality}"
    )
    if response is None or response.status_code >= 400:
        raise HTTPException(status_code=503, detail="no screenshot available")
    return Response(
        content=response.content,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "no-store",
            "X-Controller": response.headers.get("x-controller", "bot"),
            "X-Url": response.headers.get("x-url", ""),
        },
    )


@router.post("/{bot_id}/computer/control")
async def control(bot_id: UUID, body: ControlBody, request: Request) -> dict[str, Any]:
    bot = await _bot_or_404(request, bot_id)
    result = _relay(
        await _computer_call(
            request, "POST", f"/screens/{bot_id}/control", json={"controller": body.controller}
        )
    )
    async with _uow(request).transaction() as uow:
        await uow.bots.add_message(
            uuid.uuid4(),
            bot.id,
            role="system",
            content=(
                "You took control of the screen."
                if body.controller == "human"
                else "You handed the screen back."
            ),
            payload={"controller": body.controller},
        )
    return result


@router.post("/{bot_id}/computer/input")
async def human_input(bot_id: UUID, body: InputBody, request: Request) -> dict[str, Any]:
    await _bot_or_404(request, bot_id)
    return _relay(
        await _computer_call(
            request,
            "POST",
            f"/screens/{bot_id}/input",
            json=body.model_dump(exclude_none=True),
        )
    )


@computer_router.get("")
async def computer_status(request: Request) -> dict[str, Any]:
    health = await _computer_call(request, "GET", "/healthz", quiet=True, timeout_s=3.0)
    screens = await _computer_call(request, "GET", "/screens", quiet=True, timeout_s=3.0)
    if health is None:
        return {"reachable": False, "url": _settings(request).computer_url}
    return {
        "reachable": True,
        "url": _settings(request).computer_url,
        **health.json(),
        "screens": (screens.json().get("screens", []) if screens is not None else []),
    }


@computer_router.post("/reset")
async def computer_reset(request: Request) -> dict[str, Any]:
    return _relay(await _computer_call(request, "POST", "/reset", timeout_s=60.0))
