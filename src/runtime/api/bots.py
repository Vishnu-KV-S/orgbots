"""`/v1/bots` — the surface the bot UI talks to.

Writes go through `BotManager`, and every run it creates goes through
`RunService.start_run()`: a message typed into the chat box gets the same admission,
budget, kill switch and audit as any run in the company console. This router is a
screen in front of that door, not a second door.

The computer endpoints are a proxy to `runtime.computer`, for watching a bot's screen
and taking control of it. They never call `act` — a person's input goes through
`/input`, which the computer refuses unless the person holds the screen, and a bot's
actions go only through the gateway.

**Sign-in details** come in through `/credentials/{request_id}` and nowhere else. The
values are `SecretStr` from the moment they are parsed, sealed into the vault by
`BotManager.submit_credentials`, and never returned by any endpoint: `/v1/vault` lists
saved logins by site and hint, and can switch automatic use off or delete one.

**Team files** are `/{bot_id}/files`: the drive of the team that bot is on, which the
person browses, edits, uploads to and restores from through the same `TeamDrive` the
bots write with. A person's save names the version it was made on, so saving over a
bot's newer change is a 409 rather than a silent undo.

**No authentication**, like `/v1/control`. Fine on a local devstack; anything that
can reach this can drive a browser that may be signed in to real accounts — and,
since the vault, submit to a credential card. Do not expose it on a network.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field, SecretStr, model_validator

from runtime.api.errors import http_errors
from runtime.api.identity import current_member
from runtime.domain.bot_memory import MEMORY_CHARS, BotBrief, MemoryKind, memory_handle
from runtime.domain.bots import BotAppearance
from runtime.domain.enums import LIVE_RUN_STATUSES, RunStatus
from runtime.domain.files import (
    MAX_ATTACHMENTS,
    MAX_FILE_CHARS,
    PERSON,
    FileError,
    FileLockedError,
    FileTakenError,
    NoSuchFileError,
    StaleFileError,
    Team,
    folder_of,
    kind_of,
    name_of,
    team_of,
)
from runtime.domain.ids import OrganizationId, RunId
from runtime.domain.members import can_edit, can_see, computer_profile
from runtime.gateway.vault import Vault, VaultUnavailableError
from runtime.org.files import Change, TeamDrive
from runtime.persistence.repositories.bots import (
    BotMemoryRow,
    BotMessageRow,
    BotRow,
    BriefRevisionRow,
)
from runtime.persistence.repositories.files import FileRevisionRow, TeamFileRow
from runtime.persistence.repositories.vault import VaultEntryRow
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.runtime.bots import (
    BotManager,
    BotNotFoundError,
    CredentialInputError,
    CredentialRequestNotFoundError,
    MemoryNotFoundError,
    PendingNotFoundError,
)
from runtime.settings import Settings

router = APIRouter(prefix="/v1/bots", tags=["bots"])
computer_router = APIRouter(prefix="/v1/computer", tags=["bots"])
vault_router = APIRouter(prefix="/v1/vault", tags=["bots"])

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


def _drive(request: Request) -> TeamDrive:
    drive = getattr(request.app.state, "team_drive", None)
    if drive is None:
        drive = TeamDrive(_uow(request))
        request.app.state.team_drive = drive
    return drive


def _vault(request: Request) -> Vault:
    vault = getattr(request.app.state, "vault", None)
    if vault is None:
        try:
            vault = Vault.from_settings(_uow(request), _settings(request))
        except VaultUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        request.app.state.vault = vault
    return vault


async def _organization(request: Request) -> OrganizationId:
    """The signed-in member's organization; without members, the header if given, else
    the personal organization — created on first use. A member's request never chooses
    its organization: the header is ignored."""
    member = await current_member(request)
    settings = _settings(request)
    if member is not None:
        org = OrganizationId(member.organization_id)
    else:
        raw = request.headers.get(ORG_HEADER)
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
    """The bot, if it is in the caller's organization and theirs to see. Another
    organization's bot, or a teammate's private one, is a 404 — not a 403 that would
    confirm it exists."""
    try:
        bot = await _manager(request).get(bot_id)
    except BotNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"no bot {bot_id}") from exc
    org = await _organization(request)
    if bot.organization_id != org or not can_see(await current_member(request), bot):
        raise HTTPException(status_code=404, detail=f"no bot {bot_id}")
    return bot


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
    bot: BotRow,
    *,
    run_status: str | None = None,
    last: BotMessageRow | None = None,
    memory_count: int | None = None,
) -> dict[str, Any]:
    return {
        "id": str(bot.id),
        "actor_name": bot.actor_name,
        "name": bot.name,
        "label": bot.label,
        "description": bot.description,
        "avatar": bot.avatar,
        "brief": bot.brief,
        "brief_locked": bot.brief_locked,
        "brief_rev": bot.brief_rev,
        "memory_count": memory_count,
        "pinned": bot.pinned,
        "hidden": bot.hidden,
        "unread": bot.unread,
        "needs_attention": bot.needs_attention,
        "working": _working(run_status),
        "run_status": run_status,
        "last_run_id": str(bot.last_run_id) if bot.last_run_id else None,
        "duplicated_from": str(bot.duplicated_from) if bot.duplicated_from else None,
        "parent_bot_id": str(bot.parent_bot_id) if bot.parent_bot_id else None,
        "team_id": str(bot.team_id),
        "created_by": bot.created_by,
        "appearance": bot.appearance,
        "auto_review": bot.auto_review,
        "owner_member_id": str(bot.owner_member_id) if bot.owner_member_id else None,
        "visibility": bot.visibility,
        "created_at": bot.created_at.isoformat(),
        "updated_at": bot.updated_at.isoformat(),
        "last_message": _message_view(last) if last else None,
    }


def _memory_view(m: BotMemoryRow) -> dict[str, Any]:
    return {
        "id": str(m.id),
        "handle": memory_handle(m.id),
        "kind": m.kind,
        "content": m.content,
        "importance": m.importance,
        "pinned": m.pinned,
        "source_kind": m.source_kind,
        "source_name": m.source_name,
        "recall_count": m.recall_count,
        "last_recalled_at": m.last_recalled_at.isoformat() if m.last_recalled_at else None,
        "created_at": m.created_at.isoformat(),
        "updated_at": m.updated_at.isoformat(),
    }


def _revision_view(r: BriefRevisionRow) -> dict[str, Any]:
    return {
        "rev": r.rev,
        "brief": r.brief,
        "editor_kind": r.editor_kind,
        "editor_bot_id": str(r.editor_bot_id) if r.editor_bot_id else None,
        "editor_name": r.editor_name,
        "reason": r.reason,
        "changed": r.changed,
        "created_at": r.created_at.isoformat(),
    }


def _entry_view(e: VaultEntryRow) -> dict[str, Any]:
    """A saved login as a list shows it: site, hint, what it holds. Never a value."""
    return {
        "id": str(e.id),
        "host": e.host,
        "label": e.label,
        "kinds": list(e.kinds),
        "auto_use": e.auto_use,
        "use_count": e.use_count,
        "last_used_at": e.last_used_at.isoformat() if e.last_used_at else None,
        "created_at": e.created_at.isoformat(),
        "updated_at": e.updated_at.isoformat(),
    }


def _file_view(f: TeamFileRow, *, content: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": str(f.id),
        "path": f.path,
        "name": name_of(f.path),
        "folder": folder_of(f.path),
        "chars": f.chars,
        "version": f.version,
        "locked": f.locked,
        "created_by_kind": f.created_by_kind,
        "created_by_name": f.created_by_name,
        "updated_by_kind": f.updated_by_kind,
        "updated_by_bot_id": str(f.updated_by_bot_id) if f.updated_by_bot_id else None,
        "updated_by_name": f.updated_by_name,
        "created_at": f.created_at.isoformat(),
        "updated_at": f.updated_at.isoformat(),
        "deleted_at": f.deleted_at.isoformat() if f.deleted_at else None,
        "media_type": f.media_type,
        "bytes": f.bytes,
        "binary": f.is_binary,
        "kind": kind_of(f.media_type) if f.is_binary else "text",
    }
    if content:
        out["content"] = f.content or ""
    return out


def _file_revision_view(r: FileRevisionRow) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "version": r.version,
        "op": r.op,
        "path": r.path,
        "content": r.content,
        "editor_kind": r.editor_kind,
        "editor_name": r.editor_name,
        "run_id": str(r.run_id) if r.run_id else None,
        "note": r.note,
        "created_at": r.created_at.isoformat(),
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
    avatar: str = Field(default="", max_length=16)
    brief: BotBrief | None = None
    appearance: BotAppearance | None = None


class UpdateBody(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    label: str | None = Field(default=None, max_length=80)
    description: str | None = Field(default=None, max_length=2_000)
    avatar: str | None = Field(default=None, max_length=16)
    brief: BotBrief | None = None
    brief_locked: bool | None = None
    brief_reason: str = Field(default="", max_length=400)
    pinned: bool | None = None
    hidden: bool | None = None
    appearance: BotAppearance | None = None
    auto_review: bool | None = None
    visibility: Literal["private", "team"] | None = None
    """Share the bot (and its helpers) with every member, or make it private again."""


class MemoryBody(BaseModel):
    content: str = Field(min_length=1, max_length=MEMORY_CHARS)
    kind: MemoryKind = "fact"
    importance: int = Field(default=3, ge=1, le=5)
    pinned: bool = False


class MemoryPatch(BaseModel):
    content: str | None = Field(default=None, min_length=1, max_length=MEMORY_CHARS)
    kind: MemoryKind | None = None
    importance: int | None = Field(default=None, ge=1, le=5)
    pinned: bool | None = None


class MessageBody(BaseModel):
    text: str = Field(default="", max_length=20_000)
    reply_to: UUID | None = None
    attachments: list[UUID] = Field(default_factory=list, max_length=MAX_ATTACHMENTS)
    """Files already in the team drive (`/files/upload`), sent with the message."""
    voice: bool = False
    """Spoken in a voice chat: the bot answers in a few spoken sentences, which the
    person's browser reads aloud."""

    @model_validator(mode="after")
    def _something(self) -> MessageBody:
        if not self.text.strip() and not self.attachments:
            raise ValueError("a message needs text or an attachment")
        return self


class DecisionBody(BaseModel):
    decision: Literal["once", "always", "deny"]


class CredentialsBody(BaseModel):
    """A credential card's answer: the values by field key, or a saved login's id."""

    values: dict[str, SecretStr] = Field(default_factory=dict, max_length=12)
    save: bool = True
    use_entry_id: UUID | None = None


class FileCreateBody(BaseModel):
    path: str = Field(min_length=1, max_length=400)
    content: str = Field(default="", max_length=MAX_FILE_CHARS)


class FilePatch(BaseModel):
    """One of: new content (with the version it was edited from), a new path, or the
    lock."""

    content: str | None = Field(default=None, max_length=MAX_FILE_CHARS)
    path: str | None = Field(default=None, min_length=1, max_length=400)
    locked: bool | None = None
    base_version: int | None = Field(default=None, ge=1)


class FileRestoreBody(BaseModel):
    version: int | None = Field(default=None, ge=1)


class VaultPatch(BaseModel):
    auto_use: bool


class RuleBody(BaseModel):
    action_type: str = Field(min_length=1, max_length=32)
    host: str = Field(default="", max_length=253)
    decision: Literal["ask", "allow", "deny"]


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
    member = await current_member(request)
    uow_factory = _uow(request)
    async with uow_factory() as uow:
        bots = [b for b in await uow.bots.list_for(org) if can_see(member, b)]
        last = await uow.bots.last_message_per_bot(org)
        counts = await uow.bots.memory_counts(org)
        statuses: dict[uuid.UUID, str] = {}
        for bot in bots:
            if bot.last_run_id is not None:
                run = await uow.runs.get(RunId(bot.last_run_id))
                if run is not None:
                    statuses[bot.id] = str(run.status)
    return {
        "organization_id": str(org),
        "bots": [
            _bot_view(
                b,
                run_status=statuses.get(b.id),
                last=last.get(b.id),
                memory_count=counts.get(b.id, 0),
            )
            for b in bots
        ],
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_bot(body: CreateBody, request: Request) -> dict[str, Any]:
    org = await _organization(request)
    with http_errors():
        fields = body.model_dump(exclude={"appearance", "brief"})
        appearance = body.appearance.model_dump() if body.appearance else None
        member = await current_member(request)
        bot = await _manager(request).create(
            org,
            **fields,
            brief=body.brief,
            appearance=appearance,
            owner_member_id=member.id if member else None,
        )
    return _bot_view(bot)


@router.get("/search")
async def search(request: Request, q: str = Query(min_length=1, max_length=200)) -> dict[str, Any]:
    org = await _organization(request)
    member = await current_member(request)
    async with _uow(request)() as uow:
        bots = {b.id: b for b in await uow.bots.list_for(org) if can_see(member, b)}
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
    """Profile fields are a plain update. The brief and its lock go through
    `set_brief`, so a person's edit is a revision like any bot's."""
    await _bot_or_404(request, bot_id)
    manager = _manager(request)
    fields = body.model_dump(
        exclude_none=True, exclude={"brief", "brief_locked", "brief_reason", "visibility"}
    )
    bot = await manager.update(bot_id, fields)
    if body.visibility is not None:
        try:
            bot = await manager.share(bot_id, body.visibility)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if body.brief is not None or body.brief_locked is not None:
        bot = await manager.set_brief(
            bot_id,
            body.brief if body.brief is not None else BotBrief.model_validate(bot.brief or {}),
            reason=body.brief_reason,
            locked=body.brief_locked,
        )
    return _bot_view(bot, run_status=await _run_status(_uow(request), bot.last_run_id))


# --- brief ----------------------------------------------------------------------------


@router.get("/{bot_id}/brief/revisions")
async def brief_revisions(bot_id: UUID, request: Request) -> dict[str, Any]:
    await _bot_or_404(request, bot_id)
    async with _uow(request)() as uow:
        rows = await uow.bots.brief_revisions(bot_id)
    return {"revisions": [_revision_view(r) for r in rows]}


@router.post("/{bot_id}/brief/revisions/{rev}/restore")
async def restore_brief(bot_id: UUID, rev: int, request: Request) -> dict[str, Any]:
    await _bot_or_404(request, bot_id)
    try:
        bot = await _manager(request).restore_brief(bot_id, rev)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _bot_view(bot)


# --- memory ---------------------------------------------------------------------------


@router.get("/{bot_id}/memories")
async def memories(bot_id: UUID, request: Request) -> dict[str, Any]:
    await _bot_or_404(request, bot_id)
    async with _uow(request)() as uow:
        rows = await uow.bots.memories(bot_id)
    rows.sort(key=lambda m: (not m.pinned, -m.created_at.timestamp()))
    return {"memories": [_memory_view(m) for m in rows]}


@router.post("/{bot_id}/memories", status_code=status.HTTP_201_CREATED)
async def add_memory(bot_id: UUID, body: MemoryBody, request: Request) -> dict[str, Any]:
    await _bot_or_404(request, bot_id)
    memory_id = await _manager(request).add_memory(bot_id, **body.model_dump())
    return {"id": str(memory_id)}


@router.patch("/{bot_id}/memories/{memory_id}")
async def edit_memory(
    bot_id: UUID, memory_id: UUID, body: MemoryPatch, request: Request
) -> dict[str, Any]:
    await _bot_or_404(request, bot_id)
    try:
        await _manager(request).edit_memory(bot_id, memory_id, body.model_dump(exclude_none=True))
    except MemoryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"no memory {memory_id}") from exc
    return {"id": str(memory_id)}


@router.delete("/{bot_id}/memories/{memory_id}")
async def delete_memory(bot_id: UUID, memory_id: UUID, request: Request) -> dict[str, Any]:
    await _bot_or_404(request, bot_id)
    try:
        await _manager(request).delete_memory(bot_id, memory_id)
    except MemoryNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"no memory {memory_id}") from exc
    return {"deleted": str(memory_id)}


@router.delete("/{bot_id}/memories")
async def clear_memories(bot_id: UUID, request: Request) -> dict[str, Any]:
    await _bot_or_404(request, bot_id)
    return {"deleted": await _manager(request).clear_memories(bot_id)}


@router.post("/{bot_id}/duplicate", status_code=status.HTTP_201_CREATED)
async def duplicate_bot(bot_id: UUID, request: Request) -> dict[str, Any]:
    await _bot_or_404(request, bot_id)
    member = await current_member(request)
    with http_errors():
        bot = await _manager(request).duplicate(
            bot_id, owner_member_id=member.id if member else None
        )
    return _bot_view(bot)


@router.delete("/{bot_id}")
async def delete_bot(
    bot_id: UUID, request: Request, with_helpers: bool = Query(default=False)
) -> dict[str, Any]:
    """Delete a bot. `with_helpers=true` deletes every helper under it too; the default
    keeps them and moves its direct helpers up a level. The UI asks which."""
    await _bot_or_404(request, bot_id)
    deleted = await _manager(request).delete(bot_id, with_helpers=with_helpers)
    for victim in deleted:
        await _computer_call(request, "DELETE", f"/screens/{victim}", quiet=True)
    return {"deleted": [str(d) for d in deleted]}


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
        asking = await uow.vault.live_requests(bot_id)
        reactions = await uow.groups.reactions([m.id for m in rows])
    run_status = await _run_status(_uow(request), bot.last_run_id)
    return {
        "messages": [{**_message_view(m), "reactions": reactions.get(m.id, [])} for m in rows],
        "pending": [str(p.id) for p in pending],
        "credential_requests": [str(r.id) for r in asking],
        "working": _working(run_status),
        "run_status": run_status,
    }


@router.post("/{bot_id}/messages", status_code=status.HTTP_202_ACCEPTED)
async def send(bot_id: UUID, body: MessageBody, request: Request) -> dict[str, Any]:
    bot = await _bot_or_404(request, bot_id)
    attached: list[dict[str, Any]] = []
    for file_id in body.attachments:
        found = await _drive(request).get(team_of(bot), file_id)
        if found is None or found.deleted_at is not None:
            raise HTTPException(status_code=422, detail=f"no file {file_id} in this team's drive")
        attached.append(
            {
                "id": str(found.id),
                "path": found.path,
                "name": name_of(found.path),
                "media_type": found.media_type,
                "bytes": found.bytes,
                "kind": kind_of(found.media_type) if found.is_binary else "text",
                "chars": found.chars,
            }
        )
    payload: dict[str, Any] = {}
    run_input: dict[str, Any] = {}
    if attached:
        payload["attachments"] = attached
    if body.voice:
        payload["voice"] = True
        run_input["voice"] = True
    member = await current_member(request)
    if member is not None:
        # Who said it: shown on a team bot's messages, and whose devices hear the answer.
        payload["from"] = {"member_id": str(member.id), "name": member.shown}
        run_input["member_id"] = str(member.id)
    with http_errors():
        sent = await _manager(request).send(
            bot_id,
            body.text.strip(),
            reply_to=body.reply_to,
            payload=payload or None,
            run_input=run_input or None,
        )
    return {
        "message_id": str(sent.message_id),
        "run_id": str(sent.run_id) if sent.run_id else None,
        "admitted": sent.admitted,
        "refusal_reason": sent.refusal_reason,
    }


class VoiceCallBody(BaseModel):
    seconds: int = Field(ge=0, le=6 * 3600)
    turns: int = Field(ge=0, le=1_000)


@router.post("/{bot_id}/voice-calls", status_code=status.HTTP_201_CREATED)
async def voice_call(bot_id: UUID, body: VoiceCallBody, request: Request) -> dict[str, Any]:
    """A voice chat ended: leave a card in the conversation saying it happened. The
    call's words are already there — every turn was a message — so this is the frame."""
    bot = await _bot_or_404(request, bot_id)
    minutes, seconds = divmod(body.seconds, 60)
    length = f"{minutes} min {seconds:02d} s" if minutes else f"{seconds} s"
    message_id = uuid.uuid4()
    async with _uow(request).transaction() as uow:
        await uow.bots.add_message(
            message_id,
            bot.id,
            role="system",
            content=f"Voice chat · {length} · {body.turns} turn{'s' if body.turns != 1 else ''}",
            payload={"voice_call": {"seconds": body.seconds, "turns": body.turns}},
        )
    return {"message_id": str(message_id)}


@router.post("/{bot_id}/pending/{pending_id}", status_code=status.HTTP_202_ACCEPTED)
async def decide(
    bot_id: UUID, pending_id: UUID, body: DecisionBody, request: Request
) -> dict[str, Any]:
    bot = await _bot_or_404(request, bot_id)
    if body.decision == "always" and not can_edit(await current_member(request), bot):
        # "Always" writes an allow rule — a change to what the bot is, for everyone who
        # uses it.
        raise HTTPException(
            status_code=403,
            detail=f"{bot.name} is shared with you: allow it once, or ask its owner to add a rule",
        )
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


# --- sign-in details -------------------------------------------------------------------


@router.post("/{bot_id}/credentials/{request_id}", status_code=status.HTTP_202_ACCEPTED)
async def answer_credentials(
    bot_id: UUID, request_id: UUID, body: CredentialsBody, request: Request
) -> dict[str, Any]:
    """Answer a credential card: typed values (sealed into the vault, and saved for
    next time when `save`), or a saved login picked from the card."""
    await _bot_or_404(request, bot_id)
    manager = _manager(request)
    try:
        with http_errors():
            if body.use_entry_id is not None:
                sent = await manager.use_saved_login(bot_id, request_id, body.use_entry_id)
            else:
                values = {k: v.get_secret_value() for k, v in body.values.items()}
                try:
                    sent = await manager.submit_credentials(
                        bot_id, request_id, values, save=body.save, vault=_vault(request)
                    )
                finally:
                    values.clear()
    except CredentialRequestNotFoundError as exc:
        raise HTTPException(status_code=409, detail=f"no longer waiting: {exc}") from exc
    except CredentialInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "run_id": str(sent.run_id) if sent.run_id else None,
        "admitted": sent.admitted,
        "refusal_reason": sent.refusal_reason,
    }


@router.post("/{bot_id}/credentials/{request_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
async def cancel_credentials(bot_id: UUID, request_id: UUID, request: Request) -> dict[str, Any]:
    await _bot_or_404(request, bot_id)
    try:
        with http_errors():
            sent = await _manager(request).cancel_credentials(bot_id, request_id)
    except CredentialRequestNotFoundError as exc:
        raise HTTPException(status_code=409, detail=f"no longer waiting: {exc}") from exc
    return {
        "run_id": str(sent.run_id) if sent.run_id else None,
        "admitted": sent.admitted,
        "refusal_reason": sent.refusal_reason,
    }


async def _vault_profile(request: Request, bot_id: UUID | None, *, editing: bool) -> str:
    """Whose saved logins: a bot's profile (`?bot_id=`, someone who can see it; changing
    a team bot's needs its owner or an admin), else the caller's own."""
    member = await current_member(request)
    if bot_id is not None:
        bot = await _bot_or_404(request, bot_id)
        if editing and not can_edit(member, bot):
            raise HTTPException(
                status_code=403, detail=f"only {bot.name}'s owner or an admin can change these"
            )
        return computer_profile(bot)
    return f"m-{member.id.hex}" if member is not None else ""


async def _own_entry(request: Request, entry_id: UUID, profile: str) -> None:
    org = await _organization(request)
    async with _uow(request)() as uow:
        row = await uow.vault.get_entry(entry_id)
    if row is None or row.organization_id != org or row.profile != profile:
        raise HTTPException(status_code=404, detail=f"no saved login {entry_id}")


@vault_router.get("")
async def saved_logins(request: Request, bot_id: UUID | None = None) -> dict[str, Any]:
    org = await _organization(request)
    profile = await _vault_profile(request, bot_id, editing=False)
    async with _uow(request)() as uow:
        rows = await uow.vault.saved(org, profile)
    return {"entries": [_entry_view(e) for e in rows]}


@vault_router.patch("/{entry_id}")
async def edit_saved_login(
    entry_id: UUID, body: VaultPatch, request: Request, bot_id: UUID | None = None
) -> dict[str, Any]:
    org = await _organization(request)
    await _own_entry(request, entry_id, await _vault_profile(request, bot_id, editing=True))
    async with _uow(request).transaction() as uow:
        found = await uow.vault.set_auto_use(org, entry_id, body.auto_use)
    if not found:
        raise HTTPException(status_code=404, detail=f"no saved login {entry_id}")
    return {"id": str(entry_id), "auto_use": body.auto_use}


@vault_router.delete("/{entry_id}")
async def delete_saved_login(
    entry_id: UUID, request: Request, bot_id: UUID | None = None
) -> dict[str, Any]:
    org = await _organization(request)
    await _own_entry(request, entry_id, await _vault_profile(request, bot_id, editing=True))
    async with _uow(request).transaction() as uow:
        found = await uow.vault.delete_entry(org, entry_id)
    if not found:
        raise HTTPException(status_code=404, detail=f"no saved login {entry_id}")
    return {"deleted": str(entry_id)}


# --- team files -----------------------------------------------------------------------


def _file_error(exc: FileError) -> HTTPException:
    if isinstance(exc, NoSuchFileError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, StaleFileError):
        return HTTPException(
            status_code=409,
            detail="This file changed since you opened it — reload it to see the newer "
            "version before saving.",
        )
    if isinstance(exc, FileTakenError | FileLockedError):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=422, detail=str(exc))


async def _team(request: Request, bot_id: UUID) -> Team:
    return team_of(await _bot_or_404(request, bot_id))


def _changed(change: Change) -> dict[str, Any]:
    return {"file": _file_view(change.file, content=True), "op": change.op}


@router.get("/{bot_id}/files")
async def team_files(
    bot_id: UUID, request: Request, q: str | None = Query(default=None, max_length=200)
) -> dict[str, Any]:
    """The drive of `bot_id`'s team: every live file (no content), the trash, and who
    is on the team. With `q`, the files matching it, best first, with a snippet."""
    bot = await _bot_or_404(request, bot_id)
    team = team_of(bot)
    drive = _drive(request)
    async with _uow(request)() as uow:
        members = await uow.bots.team(bot.team_id)
    out: dict[str, Any] = {
        "team": {
            "id": str(bot.team_id),
            "members": [
                {
                    "id": str(m.id),
                    "name": m.name,
                    "label": m.label,
                    "parent_bot_id": str(m.parent_bot_id) if m.parent_bot_id else None,
                }
                for m in members
            ],
        },
        "files": [_file_view(f) for f in await drive.listing(team)],
        "trash": [_file_view(f) for f in await drive.trash(team)],
    }
    if q and q.strip():
        out["matches"] = [
            {**_file_view(f), "snippet": text} for f, text in await drive.search(team, q)
        ]
    return out


@router.post("/{bot_id}/files", status_code=status.HTTP_201_CREATED)
async def create_file(bot_id: UUID, body: FileCreateBody, request: Request) -> dict[str, Any]:
    team = await _team(request, bot_id)
    try:
        change = await _drive(request).create(team, body.path, body.content, editor=PERSON)
    except FileError as exc:
        raise _file_error(exc) from exc
    return _changed(change)


@router.get("/{bot_id}/files/{file_id}")
async def get_file(bot_id: UUID, file_id: UUID, request: Request) -> dict[str, Any]:
    team = await _team(request, bot_id)
    found = await _drive(request).get(team, file_id)
    if found is None:
        raise HTTPException(status_code=404, detail=f"no file {file_id}")
    return _file_view(found, content=True)


@router.patch("/{bot_id}/files/{file_id}")
async def update_file(
    bot_id: UUID, file_id: UUID, body: FilePatch, request: Request
) -> dict[str, Any]:
    team = await _team(request, bot_id)
    drive = _drive(request)
    try:
        if body.locked is not None and body.content is None and body.path is None:
            return {"file": _file_view(await drive.set_locked(team, file_id, body.locked))}
        change = await drive.update(
            team,
            file_id,
            editor=PERSON,
            content=body.content,
            path=body.path,
            base_version=body.base_version,
        )
    except FileError as exc:
        raise _file_error(exc) from exc
    return _changed(change)


@router.delete("/{bot_id}/files/{file_id}")
async def delete_file(
    bot_id: UUID,
    file_id: UUID,
    request: Request,
    base_version: int | None = Query(default=None, ge=1),
) -> dict[str, Any]:
    team = await _team(request, bot_id)
    try:
        change = await _drive(request).remove(
            team, file_id, editor=PERSON, base_version=base_version
        )
    except FileError as exc:
        raise _file_error(exc) from exc
    return _changed(change)


@router.get("/{bot_id}/files/{file_id}/revisions")
async def file_revisions(bot_id: UUID, file_id: UUID, request: Request) -> dict[str, Any]:
    team = await _team(request, bot_id)
    try:
        rows = await _drive(request).revisions(team, file_id)
    except FileError as exc:
        raise _file_error(exc) from exc
    return {"revisions": [_file_revision_view(r) for r in rows]}


@router.post("/{bot_id}/files/{file_id}/restore")
async def restore_file(
    bot_id: UUID, file_id: UUID, body: FileRestoreBody, request: Request
) -> dict[str, Any]:
    """Out of the trash, or back to an earlier revision — as a new revision."""
    team = await _team(request, bot_id)
    try:
        change = await _drive(request).restore(team, file_id, editor=PERSON, version=body.version)
    except FileError as exc:
        raise _file_error(exc) from exc
    return _changed(change)


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
    profile: str | None = None,
) -> httpx.Response | None:
    """`profile` is the bot's browser profile (`domain.members.computer_profile`), for a
    call about a screen or a workspace."""
    base = _settings(request).computer_url.rstrip("/")
    params = {"profile": profile} if profile is not None else None
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            return await client.request(method, f"{base}{path}", json=json, params=params)
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
    bot = await _bot_or_404(request, bot_id)
    response = await _computer_call(
        request,
        "GET",
        f"/screens/{bot_id}/screenshot?quality={quality}",
        profile=computer_profile(bot),
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
            request,
            "POST",
            f"/screens/{bot_id}/control",
            json={"controller": body.controller},
            profile=computer_profile(bot),
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
    bot = await _bot_or_404(request, bot_id)
    return _relay(
        await _computer_call(
            request,
            "POST",
            f"/screens/{bot_id}/input",
            json=body.model_dump(exclude_none=True),
            profile=computer_profile(bot),
        )
    )


@computer_router.get("")
async def computer_status(request: Request) -> dict[str, Any]:
    health = await _computer_call(request, "GET", "/healthz", quiet=True, timeout_s=3.0)
    screens = await _computer_call(request, "GET", "/screens", quiet=True, timeout_s=3.0)
    if health is None:
        return {"reachable": False, "url": _settings(request).computer_url}
    listed = screens.json().get("screens", []) if screens is not None else []
    member = await current_member(request)
    if member is not None:
        # A screen shows where a bot is; only the bots this member can see.
        org = await _organization(request)
        async with _uow(request)() as uow:
            mine = {str(b.id) for b in await uow.bots.list_for(org) if can_see(member, b)}
        listed = [s for s in listed if str(s.get("screen_id")) in mine]
    return {
        "reachable": True,
        "url": _settings(request).computer_url,
        **health.json(),
        "screens": listed,
    }


@computer_router.post("/reset")
async def computer_reset(request: Request) -> dict[str, Any]:
    member = await current_member(request)
    if member is not None and not member.is_admin:
        raise HTTPException(
            status_code=403,
            detail="recovering the computer restarts everyone's browser; ask an admin",
        )
    return _relay(await _computer_call(request, "POST", "/reset", timeout_s=60.0))
