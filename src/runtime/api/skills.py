"""Skills over HTTP: the library, the marketplace, and teaching by demonstration.

`/v1/skills` is the organization's library — every bot reads the same one — and the
person's whole say over it: write, edit, review a draft and mark it ready, delete.
`/v1/marketplace` lists the packaged skills the runtime ships and installs one.

`/v1/bots/{bot_id}/teach` is a demonstration. Starting one hands the bot's screen to
the person and tells the computer to record; the person does the task in the Computer
pane; stopping hands the screen back and sends the bot the recording as a message, so
the write-up is a turn like any other — admitted, budgeted and visible — that ends in a
`save_skill` the runtime files as a draft. Cancelling keeps nothing.
"""

from __future__ import annotations

import uuid
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from runtime.api.bots import _bot_or_404, _computer_call, _manager, _organization, _relay, _uow
from runtime.domain.skills import SkillBody, SkillError, render_recording, slug
from runtime.org.marketplace import SKILLS, catalog_skill
from runtime.org.skills import SkillService
from runtime.persistence.repositories.skills import RecordingRow, SkillRow

router = APIRouter(prefix="/v1/skills", tags=["skills"])
marketplace_router = APIRouter(prefix="/v1/marketplace", tags=["skills"])
teach_router = APIRouter(prefix="/v1/bots", tags=["skills"])


def _service(request: Request) -> SkillService:
    service = getattr(request.app.state, "skills", None)
    if service is None:
        service = SkillService(_uow(request))
        request.app.state.skills = service
    return service


def _view(s: SkillRow) -> dict[str, Any]:
    return {
        "id": str(s.id),
        "name": s.name,
        **s.body(),
        "status": s.status,
        "source": s.source,
        "source_bot_id": str(s.source_bot_id) if s.source_bot_id else None,
        "recording_id": str(s.recording_id) if s.recording_id else None,
        "version": s.version,
        "updated_by_kind": s.updated_by_kind,
        "updated_by_name": s.updated_by_name,
        "use_count": s.use_count,
        "last_used_at": s.last_used_at.isoformat() if s.last_used_at else None,
        "created_at": s.created_at.isoformat(),
        "updated_at": s.updated_at.isoformat(),
    }


def _recording_view(r: RecordingRow) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "bot_id": str(r.bot_id),
        "goal": r.goal,
        "status": r.status,
        "steps": r.steps,
        "skill_id": str(r.skill_id) if r.skill_id else None,
        "started_at": r.started_at.isoformat(),
        "stopped_at": r.stopped_at.isoformat() if r.stopped_at else None,
    }


def _bad(exc: Exception) -> HTTPException:
    return HTTPException(status_code=422, detail=str(exc).splitlines()[0])


class SkillCreate(SkillBody):
    name: str = Field(default="", max_length=64)
    """Defaults to the title, hyphenated."""
    status: str = Field(default="ready", pattern="^(draft|ready)$")


class SkillPatch(BaseModel):
    name: str | None = Field(default=None, max_length=64)
    title: str | None = Field(default=None, max_length=120)
    when: str | None = Field(default=None, max_length=1_500)
    inputs: str | None = Field(default=None, max_length=1_500)
    steps: list[str] | None = Field(default=None, max_length=30)
    checks: str | None = Field(default=None, max_length=1_500)
    output: str | None = Field(default=None, max_length=1_500)
    approvals: str | None = Field(default=None, max_length=1_500)
    status: str | None = Field(default=None, pattern="^(draft|ready)$")


async def _skill_or_404(request: Request, skill_id: UUID) -> SkillRow:
    org = await _organization(request)
    skill = await _service(request).get(skill_id)
    if skill is None or skill.organization_id != org:
        raise HTTPException(status_code=404, detail=f"no skill {skill_id}")
    return skill


# --- the library -----------------------------------------------------------------------


@router.get("")
async def library(request: Request) -> dict[str, Any]:
    org = await _organization(request)
    return {"skills": [_view(s) for s in await _service(request).library(org)]}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_skill(body: SkillCreate, request: Request) -> dict[str, Any]:
    org = await _organization(request)
    name = body.name or slug(body.title)
    content = SkillBody.model_validate(body.model_dump(exclude={"name", "status"}))
    try:
        skill = await _service(request).create(org, name, content, status=body.status)
    except SkillError as exc:
        raise _bad(exc) from exc
    return _view(skill)


@router.get("/{skill_id}")
async def get_skill(skill_id: UUID, request: Request) -> dict[str, Any]:
    return _view(await _skill_or_404(request, skill_id))


@router.patch("/{skill_id}")
async def update_skill(skill_id: UUID, body: SkillPatch, request: Request) -> dict[str, Any]:
    skill = await _skill_or_404(request, skill_id)
    content = body.model_dump(exclude_none=True, exclude={"name", "status"})
    try:
        updated = await _service(request).update(
            skill, name=body.name, body=content, status=body.status
        )
    except (SkillError, ValueError) as exc:
        raise _bad(exc) from exc
    return _view(updated)


@router.delete("/{skill_id}")
async def delete_skill(skill_id: UUID, request: Request) -> dict[str, Any]:
    await _skill_or_404(request, skill_id)
    await _service(request).delete(skill_id)
    return {"deleted": str(skill_id)}


# --- the marketplace -------------------------------------------------------------------


@marketplace_router.get("")
async def marketplace(request: Request) -> dict[str, Any]:
    org = await _organization(request)
    installed = {s.name for s in await _service(request).library(org)}
    return {
        "skills": [
            {
                "key": item.key,
                "name": item.name,
                "category": item.category,
                "blurb": item.blurb,
                **item.body.model_dump(),
                "installed": item.name in installed,
            }
            for item in SKILLS
        ]
    }


@marketplace_router.post("/skills/{key}", status_code=status.HTTP_201_CREATED)
async def install_skill(key: str, request: Request) -> dict[str, Any]:
    org = await _organization(request)
    item = catalog_skill(key)
    if item is None:
        raise HTTPException(status_code=404, detail=f"no skill {key!r} in the marketplace")
    try:
        skill = await _service(request).create(
            org, item.name, item.body, status="ready", source="marketplace"
        )
    except SkillError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _view(skill)


# --- teaching by demonstration ---------------------------------------------------------


class TeachBody(BaseModel):
    goal: str = Field(min_length=3, max_length=500)
    """What the task achieves, in the person's words — the recording says how."""


class StopBody(BaseModel):
    cancel: bool = False


@teach_router.get("/{bot_id}/teach")
async def teaching(bot_id: UUID, request: Request) -> dict[str, Any]:
    await _bot_or_404(request, bot_id)
    async with _uow(request)() as uow:
        live = await uow.skills.live_recording(bot_id)
    if live is None:
        return {"recording": None}
    progress = await _computer_call(
        request, "GET", f"/screens/{bot_id}/recording", quiet=True, timeout_s=5.0
    )
    status_ = progress.json() if progress is not None and progress.status_code < 400 else {}
    return {"recording": _recording_view(live), "computer": status_}


@teach_router.post("/{bot_id}/teach", status_code=status.HTTP_201_CREATED)
async def start_teaching(bot_id: UUID, body: TeachBody, request: Request) -> dict[str, Any]:
    bot = await _bot_or_404(request, bot_id)
    async with _uow(request)() as uow:
        if await uow.skills.live_recording(bot_id) is not None:
            raise HTTPException(status_code=409, detail="a demonstration is already recording")
    _relay(
        await _computer_call(
            request, "POST", f"/screens/{bot_id}/recording", json={"action": "start"}
        )
    )
    recording_id = uuid.uuid4()
    async with _uow(request).transaction() as uow:
        await uow.skills.start_recording(
            recording_id, bot.organization_id, bot_id, body.goal.strip()
        )
        await uow.bots.add_message(
            uuid.uuid4(),
            bot_id,
            role="system",
            content=(
                f"You're showing {bot.name} how to: {body.goal.strip()}. Do it on the "
                "Computer screen, then press Stop. Passwords you type are not recorded."
            ),
            payload={"recording_id": str(recording_id), "teach": "started"},
        )
    return {"recording_id": str(recording_id)}


@teach_router.post("/{bot_id}/teach/stop")
async def stop_teaching(bot_id: UUID, body: StopBody, request: Request) -> dict[str, Any]:
    bot = await _bot_or_404(request, bot_id)
    async with _uow(request)() as uow:
        live = await uow.skills.live_recording(bot_id)
    if live is None:
        raise HTTPException(status_code=409, detail="nothing is recording")
    stopped = await _computer_call(
        request,
        "POST",
        f"/screens/{bot_id}/recording",
        json={"action": "stop", "hand_back": True},
        quiet=True,
    )
    steps: list[dict[str, Any]] = []
    if stopped is not None and stopped.status_code < 400:
        steps = list(stopped.json().get("steps") or [])
    worked = [s for s in steps if s.get("kind") != "start"]
    cancel = body.cancel or not worked
    async with _uow(request).transaction() as uow:
        if not await uow.skills.finish_recording(
            live.id, status="cancelled" if cancel else "stopped", steps=steps
        ):
            raise HTTPException(status_code=409, detail="that demonstration already ended")
        if cancel:
            await uow.bots.add_message(
                uuid.uuid4(),
                bot_id,
                role="system",
                content=(
                    "Demonstration cancelled; nothing was saved."
                    if body.cancel
                    else "Demonstration stopped before anything was done; nothing was saved."
                ),
                payload={"recording_id": str(live.id), "teach": "cancelled"},
            )
    if cancel:
        return {"recording_id": str(live.id), "status": "cancelled", "steps": len(worked)}
    sent = await _manager(request).send(
        bot.id,
        render_recording(live.goal, steps),
        payload={"recording_id": str(live.id), "demonstration": live.goal, "steps": len(worked)},
        run_input={"recording_id": str(live.id)},
    )
    return {
        "recording_id": str(live.id),
        "status": "stopped",
        "steps": len(worked),
        "run_id": str(sent.run_id) if sent.run_id else None,
        "admitted": sent.admitted,
    }
