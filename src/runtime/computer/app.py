"""HTTP surface of the computer.

Two audiences and they get different verbs:

- **Bots**, through the gateway tools: `observe` and `act`. Refused with 409 while a
  person holds the screen.
- **People**, through the API proxy: `screenshot`, `control` and `input`. `input` is
  refused unless the person holds the screen, so watching can never become driving by
  accident.

Binds to loopback by default. Like `/v1/control`, it has no authentication — anything
that can reach it can drive a browser that may be signed in to real accounts.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel, Field

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


class ControlBody(BaseModel):
    controller: str = Field(pattern="^(bot|human)$")


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
        return _view(screen, snap)

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

    @app.delete("/screens/{screen_id}")
    async def close(screen_id: str) -> dict[str, Any]:
        await computer.close_screen(screen_id)
        return {"closed": screen_id}

    @app.post("/reset")
    async def reset() -> dict[str, Any]:
        await computer.reset()
        return {"ok": True}

    return app
