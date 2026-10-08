"""`/v1/push` — devices that want to hear when a bot needs the person.

The installed app (or any browser where the person turned push on) asks for the
server's VAPID public key, subscribes at its push service with it, and posts the
subscription here; from then on the worker's notifier pushes to it
(`runtime.runtime.notifier`). `test` puts one notification in the outbox, so a person
can see it arrive.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from runtime.api.bots import _organization, _settings, _uow
from runtime.api.identity import current_member
from runtime.gateway.push import PushSender
from runtime.gateway.vault import VaultUnavailableError

router = APIRouter(prefix="/v1/push", tags=["push"])


def _sender(request: Request) -> PushSender:
    sender = getattr(request.app.state, "push", None)
    if sender is None:
        sender = PushSender(_uow(request), _settings(request))
        request.app.state.push = sender
    return sender


class Keys(BaseModel):
    p256dh: str = Field(min_length=10, max_length=200)
    auth: str = Field(min_length=8, max_length=100)


class Subscription(BaseModel):
    endpoint: str = Field(min_length=10, max_length=1_000, pattern="^https://")
    keys: Keys
    device: str = Field(default="", max_length=300)
    """The browser's own user agent. The request's is the UI proxy's, not the device's."""


class Unsubscribe(BaseModel):
    endpoint: str = Field(min_length=10, max_length=1_000)


@router.get("")
async def push_key(request: Request) -> dict[str, Any]:
    await _organization(request)
    try:
        key = await _sender(request).public_key()
    except VaultUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc).splitlines()[0]) from exc
    return {"public_key": key}


@router.post("/subscribe", status_code=status.HTTP_201_CREATED)
async def subscribe(body: Subscription, request: Request) -> dict[str, Any]:
    org = await _organization(request)
    member = await current_member(request)
    async with _uow(request).transaction() as uow:
        await uow.push.subscribe(
            org,
            body.endpoint,
            body.keys.p256dh,
            body.keys.auth,
            body.device or request.headers.get("user-agent", ""),
            member_id=member.id if member else None,
        )
    return {"subscribed": True}


@router.post("/unsubscribe")
async def unsubscribe(body: Unsubscribe, request: Request) -> dict[str, Any]:
    org = await _organization(request)
    async with _uow(request).transaction() as uow:
        await uow.push.unsubscribe(org, body.endpoint)
    return {"subscribed": False}


@router.post("/test", status_code=status.HTTP_202_ACCEPTED)
async def test_push(request: Request) -> dict[str, Any]:
    org = await _organization(request)
    member = await current_member(request)
    async with _uow(request).transaction() as uow:
        await uow.push.notify(
            uuid.uuid4(),
            org,
            bot_id=None,
            kind="test",
            title="Notifications are on",
            body="Your bots will tell you here when they reply or need you.",
            url="/",
            member_id=member.id if member else None,
        )
    return {"queued": True}
