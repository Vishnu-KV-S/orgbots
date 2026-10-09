"""HTTP surface of the computer.

Two audiences and they get different verbs:

- **Bots**, through the gateway tools: `observe`, `act`, and `fill` — the vault's
  sign-in values, which `browser.act@1` opens and sends here so no run holds them.
  Refused with 409 while a person holds the screen.
- **People**, through the API proxy: `screenshot`, `control` and `input`. `input` is
  refused unless the person holds the screen, so watching can never become driving by
  accident. `recording` starts and stops a demonstration: the person's inputs on the
  screen, written down as steps for a bot to learn a skill from.

Binds to loopback by default. Like `/v1/control`, it has no authentication — anything
that can reach it can drive a browser that may be signed in to real accounts.
"""

from __future__ import annotations

import base64
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, Field, SecretStr

from runtime.computer.browser import (
    ACTION_TYPES,
    Computer,
    ComputerError,
    HumanInControlError,
)
from runtime.computer.snapshot import render


class ActBody(BaseModel):
    action: dict[str, Any]
    label: str = ""


class ObserveBody(BaseModel):
    label: str = ""
    screenshot: bool = False
    """Also return a masked JPEG of the viewport, base64, for the bot's vision step."""


class FillField(BaseModel):
    elements: list[int] = Field(max_length=12)
    value: SecretStr
    password: bool = False


class FillBody(BaseModel):
    """The values arrive as `SecretStr` so a validation error, a repr or a log line
    shows asterisks. Only the gateway's `browser.act@1` sends this."""

    expect_host: str = Field(min_length=1, max_length=253)
    fields: list[FillField] = Field(min_length=1, max_length=12)
    submit: bool = True
    label: str = ""


class ControlBody(BaseModel):
    controller: str = Field(pattern="^(bot|human)$")


class RecordingBody(BaseModel):
    action: str = Field(pattern="^(start|stop)$")
    hand_back: bool = True
    """On stop: give the screen back to the bot."""


class InputBody(BaseModel):
    kind: str
    x: float | None = None
    y: float | None = None
    text: str | None = None
    key: str | None = None
    dy: float | None = None
    url: str | None = None


def _view(screen: Any, snap: dict[str, Any]) -> dict[str, Any]:
    return {
        "screen_id": screen.screen_id,
        "controller": screen.controller,
        "url": snap.get("url", ""),
        "title": snap.get("title", ""),
        "snapshot": snap,
        "rendered": render(snap),
    }


def create_app(profile_dir: Path, *, headless: bool = True) -> FastAPI:
    computer = Computer(profile_dir, headless=headless)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await computer.start()
        try:
            yield
        finally:
            await computer.stop()

    app = FastAPI(title="agent-org computer", lifespan=lifespan)
    app.state.computer = computer

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {
            "ok": computer.running,
            "screens": len(computer.screens()),
            "started_at": computer.started_at,
            "actions": sorted(ACTION_TYPES),
        }

    @app.get("/screens")
    async def screens() -> dict[str, Any]:
        return {
            "screens": [
                {
                    "screen_id": s.screen_id,
                    "label": s.label,
                    "controller": s.controller,
                    "url": s.page.url,
                    "last_action": s.last_action,
                    "last_active": s.last_active,
                }
                for s in computer.screens()
            ]
        }

    @app.post("/screens/{screen_id}/observe")
    async def observe(screen_id: str, body: ObserveBody) -> dict[str, Any]:
        screen = await computer.screen(screen_id, label=body.label)
        try:
            snap = await computer.observe(screen)
        except ComputerError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        view = _view(screen, snap)
        if body.screenshot:
            image = await computer.bot_screenshot(screen)
            view["screenshot"] = base64.b64encode(image).decode("ascii")
        return view

    @app.post("/screens/{screen_id}/act")
    async def act(screen_id: str, body: ActBody) -> dict[str, Any]:
        screen = await computer.screen(screen_id, label=body.label)
        try:
            snap = await computer.act(screen, body.action)
        except HumanInControlError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ComputerError as exc:
            # The action failed but the screen is fine; hand back what is there now so
            # the bot can recover without another round trip.
            try:
                snap = await computer.observe(screen)
            except ComputerError:
                snap = {}
            return {**_view(screen, snap), "ok": False, "error": str(exc)}
        return {**_view(screen, snap), "ok": True, "error": None}

    @app.post("/screens/{screen_id}/fill")
    async def fill(screen_id: str, body: FillBody) -> dict[str, Any]:
        screen = await computer.screen(screen_id, label=body.label)
        fields = [
            {"elements": f.elements, "value": f.value.get_secret_value(), "password": f.password}
            for f in body.fields
        ]
        try:
            snap = await computer.fill(
                screen, expect_host=body.expect_host, fields=fields, submit=body.submit
            )
        except HumanInControlError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ComputerError as exc:
            try:
                snap = await computer.observe(screen)
            except ComputerError:
                snap = {}
            return {**_view(screen, snap), "ok": False, "error": str(exc)}
        finally:
            fields.clear()
        return {**_view(screen, snap), "ok": True, "error": None}

    @app.get("/screens/{screen_id}/screenshot")
    async def screenshot(screen_id: str, quality: int = 70) -> Response:
        screen = await computer.screen(screen_id)
        image = await computer.screenshot(screen, quality=max(20, min(95, quality)))
        return Response(
            content=image,
            media_type="image/jpeg",
            headers={
                "Cache-Control": "no-store",
                "X-Controller": screen.controller,
                "X-Url": screen.page.url[:500],
            },
        )

    @app.post("/screens/{screen_id}/control")
    async def control(screen_id: str, body: ControlBody) -> dict[str, Any]:
        screen = await computer.screen(screen_id)
        computer.set_controller(screen, body.controller)
        return {"screen_id": screen_id, "controller": screen.controller}

    @app.post("/screens/{screen_id}/input")
    async def human_input(screen_id: str, body: InputBody) -> dict[str, Any]:
        screen = await computer.screen(screen_id)
        try:
            await computer.human_input(screen, body.model_dump(exclude_none=True))
        except ComputerError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)[:300]) from exc
        return {"ok": True, "url": screen.page.url}

    @app.post("/screens/{screen_id}/recording")
    async def recording(screen_id: str, body: RecordingBody) -> dict[str, Any]:
        screen = await computer.screen(screen_id)
        if body.action == "start":
            started = await computer.start_recording(screen)
            return {
                "recording": True,
                "controller": screen.controller,
                "started_at": started.started_at,
            }
        steps = computer.stop_recording(screen)
        if body.hand_back:
            computer.set_controller(screen, "bot")
        return {"recording": False, "controller": screen.controller, "steps": steps}

    @app.get("/screens/{screen_id}/recording")
    async def recording_status(screen_id: str) -> dict[str, Any]:
        screen = await computer.screen(screen_id)
        current = screen.recording
        if current is None:
            return {"recording": False}
        return {
            "recording": True,
            "steps": len(current.steps),
            "elapsed": round(time.time() - current.started_at, 1),
            "full": current.expired(),
            "last": current.steps[-1] if current.steps else None,
        }

    @app.delete("/screens/{screen_id}")
    async def close(screen_id: str) -> dict[str, Any]:
        await computer.close_screen(screen_id)
        return {"closed": screen_id}

    @app.post("/reset")
    async def reset() -> dict[str, Any]:
        await computer.reset()
        return {"ok": True}

    return app
