"""Routines over HTTP: a bot's routines, and the webhook that starts an event routine.

`/v1/bots/{bot_id}/routines` is the person's side — list, create, change, pause,
delete, test-run, and the last `RUNS_KEPT` firings — behind the same proxy as the rest
of `/v1/bots`. Saving goes through `RoutineService`, the same checks a bot's
`save_routine` gets; a test run goes through `BotManager.test_routine`, a turn like any
other.

`/v1/hooks/routines/{routine_id}/{token}` is the other side: GitHub, Slack, or anything
that can POST JSON. It is **not** behind the UI's proxy — a webhook comes from outside
— and it is authenticated by what it carries: the URL's token, compared in constant
time, and, when the person saved one, the sender's signature over the raw body
(GitHub's `X-Hub-Signature-256`, Slack's `v0` scheme with a five-minute window; a
generic sender signs like GitHub). A wrong token is a 404, not a 403, so the endpoint
does not confirm which routines exist. An event is only *queued* here: the runner
starts it when the bot is free, so a burst of deliveries cannot supersede the bot or
each other, and a retried delivery (same id) queues nothing new.

The signing secret is sealed with the credential cipher like a vault entry, and no
response returns it — only whether one is set.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field, SecretStr

from runtime.api.bots import _bot_or_404, _manager, _settings, _uow
from runtime.domain.routines import (
    RUNS_KEPT,
    EventFacts,
    EventMatch,
    RoutineError,
    RoutineSpec,
    fire_id,
    github_event,
    matches,
    slack_event,
    webhook_event,
)
from runtime.gateway.vault import VaultUnavailableError, load_cipher
from runtime.org.routines import RoutineService, describe
from runtime.persistence.repositories.routines import RoutineRow, RoutineRunRow
from runtime.runtime.bots import BotNotFoundError

router = APIRouter(prefix="/v1/bots", tags=["bots"])
hooks_router = APIRouter(prefix="/v1/hooks", tags=["hooks"])

MAX_BODY_BYTES = 512 * 1024
SLACK_WINDOW_S = 300


def _service(request: Request) -> RoutineService:
    service = getattr(request.app.state, "routines", None)
    if service is None:
        service = RoutineService(_uow(request))
        request.app.state.routines = service
    return service


def _hook_path(routine: RoutineRow) -> str | None:
    if routine.kind != "event" or not routine.token:
        return None
    return f"/v1/hooks/routines/{routine.id}/{routine.token}"


def _hook_url(request: Request, routine: RoutineRow) -> str | None:
    path = _hook_path(routine)
    if path is None:
        return None
    base = _settings(request).public_url.rstrip("/") or str(request.base_url).rstrip("/")
    return base + path


def _run_view(r: RoutineRunRow) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "trigger": r.trigger,
        "status": r.status,
        "scheduled_for": r.scheduled_for.isoformat() if r.scheduled_for else None,
        "run_id": str(r.run_id) if r.run_id else None,
        "detail": r.detail,
        "event": r.event,
        "created_at": r.created_at.isoformat(),
        "started_at": r.started_at.isoformat() if r.started_at else None,
    }


def _routine_view(
    request: Request, r: RoutineRow, last: RoutineRunRow | None = None
) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "bot_id": str(r.bot_id),
        "name": r.name,
        "instruction": r.instruction,
        "kind": r.kind,
        "cron": r.cron,
        "timezone": r.timezone,
        "schedule": describe(r.cron, r.timezone) if r.kind == "schedule" else "",
        "source": r.source,
        "match": r.match,
        "hook_url": _hook_url(request, r),
        "has_secret": r.has_secret,
        "inputs": r.inputs,
        "output": r.output,
        "approval": r.approval,
        "when_missing": r.when_missing,
        "active": r.active,
        "created_by_kind": r.created_by_kind,
        "next_fire_at": r.next_fire_at.isoformat() if r.next_fire_at else None,
        "last_fired_at": r.last_fired_at.isoformat() if r.last_fired_at else None,
        "fire_count": r.fire_count,
        "last_run": _run_view(last) if last else None,
        "created_at": r.created_at.isoformat(),
        "updated_at": r.updated_at.isoformat(),
    }


class RoutineBody(RoutineSpec):
    signing_secret: SecretStr | None = Field(default=None, max_length=256)
    """An event routine's signing secret (GitHub webhook secret, Slack signing secret).
    Empty string clears it."""


class RoutinePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    instruction: str | None = Field(default=None, min_length=1, max_length=4_000)
    cron: str | None = Field(default=None, max_length=120)
    timezone: str | None = Field(default=None, max_length=64)
    match: EventMatch | None = None
    inputs: str | None = Field(default=None, max_length=1_000)
    output: str | None = Field(default=None, max_length=1_000)
    approval: str | None = Field(default=None, pattern="^(default|drafts)$")
    when_missing: str | None = Field(default=None, max_length=500)
    active: bool | None = None
    signing_secret: SecretStr | None = Field(default=None, max_length=256)


async def _routine_or_404(request: Request, bot_id: UUID, routine_id: UUID) -> RoutineRow:
    routine = await _service(request).get(routine_id)
    if routine is None or routine.bot_id != bot_id:
        raise HTTPException(status_code=404, detail=f"no routine {routine_id}")
    return routine


async def _seal_secret(request: Request, routine: RoutineRow, secret: SecretStr) -> None:
    value = secret.get_secret_value()
    async with _uow(request).transaction() as uow:
        if not value:
            await uow.routines.update(
                routine.id,
                {"secret_key_id": None, "secret_nonce": None, "secret_ciphertext": None},
            )
            return
        try:
            cipher = load_cipher(_settings(request))
        except VaultUnavailableError as exc:
            raise HTTPException(status_code=503, detail=str(exc).splitlines()[0]) from exc
        key_id, nonce, ciphertext = cipher.encrypt(value, aad=_aad(routine))
        await uow.routines.update(
            routine.id,
            {"secret_key_id": key_id, "secret_nonce": nonce, "secret_ciphertext": ciphertext},
        )


def _aad(routine: RoutineRow) -> bytes:
    return f"routine:{routine.organization_id}:{routine.id}".encode()


def _bad(exc: Exception) -> HTTPException:
    return HTTPException(status_code=422, detail=str(exc).splitlines()[0])


# --- a bot's routines ------------------------------------------------------------------


@router.get("/{bot_id}/routines")
async def list_routines(bot_id: UUID, request: Request) -> dict[str, Any]:
    await _bot_or_404(request, bot_id)
    async with _uow(request)() as uow:
        rows = await uow.routines.for_bot(bot_id)
        last = await uow.routines.last_runs(bot_id)
    return {"routines": [_routine_view(request, r, last.get(r.id)) for r in rows]}


@router.post("/{bot_id}/routines", status_code=status.HTTP_201_CREATED)
async def create_routine(bot_id: UUID, body: RoutineBody, request: Request) -> dict[str, Any]:
    bot = await _bot_or_404(request, bot_id)
    spec = RoutineSpec.model_validate(body.model_dump(exclude={"signing_secret"}))
    try:
        routine = await _service(request).create(bot, spec)
    except (RoutineError, ValueError) as exc:
        raise _bad(exc) from exc
    if body.signing_secret is not None and routine.kind == "event":
        await _seal_secret(request, routine, body.signing_secret)
        routine = await _routine_or_404(request, bot_id, routine.id)
    return _routine_view(request, routine)


@router.patch("/{bot_id}/routines/{routine_id}")
async def update_routine(
    bot_id: UUID, routine_id: UUID, body: RoutinePatch, request: Request
) -> dict[str, Any]:
    current = await _routine_or_404(request, bot_id, routine_id)
    changes = body.model_dump(exclude_none=True, exclude={"signing_secret", "match"})
    merged = {
        "name": current.name,
        "instruction": current.instruction,
        "kind": current.kind,
        "cron": current.cron,
        "timezone": current.timezone,
        "source": current.source,
        "match": body.match.model_dump() if body.match is not None else current.match,
        "inputs": current.inputs,
        "output": current.output,
        "approval": current.approval,
        "when_missing": current.when_missing,
        "active": current.active,
        **changes,
    }
    try:
        routine = await _service(request).update(current, RoutineSpec.model_validate(merged))
    except (RoutineError, ValueError) as exc:
        raise _bad(exc) from exc
    if body.signing_secret is not None and routine.kind == "event":
        await _seal_secret(request, routine, body.signing_secret)
        routine = await _routine_or_404(request, bot_id, routine.id)
    return _routine_view(request, routine)


@router.delete("/{bot_id}/routines/{routine_id}")
async def delete_routine(bot_id: UUID, routine_id: UUID, request: Request) -> dict[str, Any]:
    await _routine_or_404(request, bot_id, routine_id)
    await _service(request).delete(routine_id)
    return {"deleted": str(routine_id)}


@router.post("/{bot_id}/routines/{routine_id}/test", status_code=status.HTTP_202_ACCEPTED)
async def test_routine(bot_id: UUID, routine_id: UUID, request: Request) -> dict[str, Any]:
    """Run it now: real work on safe inputs, drafts only. Like a message, it supersedes
    whatever the bot is doing — the person pressed it."""
    routine = await _routine_or_404(request, bot_id, routine_id)
    try:
        fire_row_id, sent = await _manager(request).test_routine(routine)
    except BotNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"no bot {bot_id}") from exc
    return {
        "fire_id": str(fire_row_id),
        "run_id": str(sent.run_id) if sent.run_id else None,
        "admitted": sent.admitted,
        "refusal_reason": sent.refusal_reason,
    }


@router.get("/{bot_id}/routines/{routine_id}/runs")
async def routine_runs(bot_id: UUID, routine_id: UUID, request: Request) -> dict[str, Any]:
    await _routine_or_404(request, bot_id, routine_id)
    async with _uow(request)() as uow:
        rows = await uow.routines.runs(routine_id, RUNS_KEPT)
    return {"runs": [_run_view(r) for r in rows]}


# --- the webhook -----------------------------------------------------------------------


def _signed(source: str, secret: str, headers: dict[str, str], raw: bytes) -> bool:
    if source == "slack":
        stamp = headers.get("x-slack-request-timestamp", "")
        given = headers.get("x-slack-signature", "")
        if not stamp.isdigit() or abs(time.time() - int(stamp)) > SLACK_WINDOW_S:
            return False
        base = b"v0:" + stamp.encode() + b":" + raw
        wanted = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
        return hmac.compare_digest(wanted, given)
    given = headers.get("x-hub-signature-256") or headers.get("x-signature-256") or ""
    wanted = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(wanted, given)


def _facts(source: str, headers: dict[str, str], body: dict[str, Any], raw: bytes) -> EventFacts:
    if source == "github":
        return github_event(headers, body)
    if source == "slack":
        return slack_event(headers, body)
    return webhook_event(headers, body, hashlib.sha256(raw).hexdigest()[:32])


@hooks_router.post("/routines/{routine_id}/{token}", status_code=status.HTTP_202_ACCEPTED)
async def routine_hook(routine_id: UUID, token: str, request: Request) -> dict[str, Any]:
    service = _service(request)
    routine = await service.get(routine_id)
    if (
        routine is None
        or routine.kind != "event"
        or not routine.token
        or not hmac.compare_digest(routine.token, token)
    ):
        raise HTTPException(status_code=404, detail="no such hook")
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="event body too large")
    headers = {k.lower(): v for k, v in request.headers.items()}
    source = routine.source or "webhook"

    if routine.secret_ciphertext is not None:
        try:
            cipher = load_cipher(_settings(request))
            secret = cipher.decrypt(
                routine.secret_key_id or "",
                routine.secret_nonce or b"",
                routine.secret_ciphertext,
                aad=_aad(routine),
            )
        except Exception as exc:
            raise HTTPException(status_code=503, detail="cannot check signatures now") from exc
        if not _signed(source, secret, headers, raw):
            raise HTTPException(status_code=401, detail="bad signature")

    try:
        body = json.loads(raw or b"{}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="expected a JSON body") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="expected a JSON object")

    # The handshakes: answered, never fired.
    if source == "slack" and body.get("type") == "url_verification":
        return {"challenge": body.get("challenge", "")}
    if source == "github" and headers.get("x-github-event") == "ping":
        return {"ok": True, "pong": True}
    # A bot's own message would start the routine that answers it, which would post
    # another: Slack marks messages from bots, and those never fire.
    if source == "slack" and (body.get("event") or {}).get("bot_id"):
        return {"fired": False, "reason": "messages from bots are ignored"}
    if not routine.active:
        return {"fired": False, "reason": "the routine is paused"}

    facts = _facts(source, headers, body, raw)
    if not matches(EventMatch.model_validate(routine.match or {}), facts):
        return {"fired": False, "reason": "the event did not match the routine"}
    delivery = facts.delivery or hashlib.sha256(raw).hexdigest()[:32]
    row_id = fire_id(routine.id, f"event:{delivery}")
    async with _uow(request).transaction() as uow:
        queued = await uow.routines.add_run(
            row_id,
            routine_id=routine.id,
            bot_id=routine.bot_id,
            trigger="event",
            event=facts.as_payload(),
        )
    return {"fired": True, "queued": queued, "fire_id": str(row_id)}
