"""HTTP surface of the computer.

Two audiences and they get different verbs:

- **Bots**, through the gateway tools: `observe`, `act`, and `fill` — the vault's
  sign-in values, which `browser.act@1` opens and sends here so no run holds them.
  Refused with 409 while a person holds the screen.
- **People**, through the API proxy: `screenshot`, `control` and `input`. `input` is
  refused unless the person holds the screen, so watching can never become driving by
  accident. `recording` starts and stops a demonstration: the person's inputs on the
  screen, written down as steps for a bot to learn a skill from.

The **terminal and workspace** (`runtime.computer.terminal`) are the third surface:
`/terminal/run` runs a bot's command (sandboxed, or on this machine when the run says
`local` — the bot's approval gate decided that before the call left the gateway), and
`/workspace` lists, reads and writes the shared directory that browser downloads land in.

Every screen, terminal and workspace call names a **profile** (`?profile=`, `""` by
default): the browser profile a screen runs in, and the workspace its commands and
downloads use. The runtime decides it from the bot (`domain.members.computer_profile`);
a profile that is not `""`, `m-<hex>` or `t-<hex>` is refused before it becomes a path.

Binds to loopback by default. Like `/v1/control`, it has no authentication — anything
that can reach it can drive a browser that may be signed in to real accounts.
"""

from __future__ import annotations

import asyncio
import base64
import re
import shutil
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, SecretStr

from runtime.computer.browser import (
    ACTION_TYPES,
    PROFILE,
    Computer,
    ComputerError,
    HumanInControlError,
    check_profile,
)
from runtime.computer.snapshot import render
from runtime.computer.terminal import DEFAULT_TIMEOUT_S, Terminal, TerminalError


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


def _view(
    screen: Any, snap: dict[str, Any], downloads: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    rendered = render(snap)
    if downloads:
        lines = []
        for d in downloads:
            if d.get("error"):
                lines.append(f"- {d.get('name')}: the download failed ({d['error']})")
            else:
                lines.append(f"- saved {d['path']} ({int(d.get('bytes', 0)):,} bytes)")
        rendered += (
            "\n\nDOWNLOADED (into the shared workspace; copy_file one into your team drive "
            "to read it):\n" + "\n".join(lines)
        )
    return {
        "screen_id": screen.screen_id,
        "controller": screen.controller,
        "url": snap.get("url", ""),
        "title": snap.get("title", ""),
        "snapshot": snap,
        "rendered": rendered,
        "downloads": downloads or [],
    }


_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
MAX_UPLOAD_BYTES = 15 * 1024 * 1024


class UploadFile(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    data: str = Field(repr=False)
    """Base64."""


class UploadBody(BaseModel):
    """Files for a page: bytes from the runtime (a team drive file), or paths in this
    profile's /workspace. Only the gateway's `browser.act@1` sends this."""

    element: int
    files: list[UploadFile] = Field(default_factory=list, max_length=10)
    workspace: list[str] = Field(default_factory=list, max_length=10)
    label: str = ""


def _upload_name(raw: str, taken: set[str]) -> str:
    name = "".join(c for c in raw.rsplit("/", 1)[-1] if c not in '\\:*?"<>|\x00').strip(" .")
    name = name or "file"
    stem, dot, ext = name.rpartition(".")
    if not dot or not stem:
        stem, ext = name, ""
    candidate, n = name, 1
    while candidate in taken:
        n += 1
        candidate = f"{stem}-{n}" + (f".{ext}" if ext else "")
    taken.add(candidate)
    return candidate


class TerminalBody(BaseModel):
    screen_id: str = Field(min_length=1, max_length=64)
    command: str = Field(min_length=1, max_length=20_000)
    timeout_s: float = Field(default=DEFAULT_TIMEOUT_S, ge=1, le=300)
    local: bool = False
    profile: str = Field(default="", pattern=PROFILE.pattern)
    network: bool = True
    secrets: dict[str, str] = Field(default_factory=dict, repr=False, max_length=100)
    """The organization's team secrets, as environment variables in the sandbox."""


class WorkspaceWrite(BaseModel):
    path: str = Field(min_length=1, max_length=400)
    data: str = Field(repr=False)
    """Base64."""


def create_app(
    profile_dir: Path, *, headless: bool = True, workspace: Path | None = None
) -> FastAPI:
    base = workspace or profile_dir.parent / "workspace"
    terminals: dict[str, Terminal] = {}

    def terminal_for(profile: str) -> Terminal:
        """A profile's workspace: the base for `""`, a sibling directory for any other —
        never inside the base, where the default profile's bots could read it."""
        try:
            check_profile(profile)
        except ComputerError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        found = terminals.get(profile)
        if found is None:
            root = base if not profile else base.with_name(f"{base.name}-{profile}")
            found = terminals[profile] = Terminal(root)
        return found

    terminal = terminal_for("")
    computer = Computer(
        profile_dir,
        headless=headless,
        download_path=lambda name, profile: terminal_for(profile).free_download_path(name),
        shown=lambda path, profile: terminal_for(profile).shown(path),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await computer.start()
        try:
            yield
        finally:
            await computer.stop()

    app = FastAPI(title="agent-org computer", lifespan=lifespan)

    @app.middleware("http")
    async def network_policy(request: Request, call_next: Any) -> Any:
        """`X-Allow-Hosts` on a call about a profile is its organization's network
        policy: `*` for any host, else the hosts its browser may reach."""
        raw = request.headers.get("x-allow-hosts")
        if raw is not None:
            hosts = None if raw.strip() == "*" else tuple(h for h in raw.split(",") if h)
            try:
                await computer.set_allow(request.query_params.get("profile", ""), hosts)
            except ComputerError as exc:
                return JSONResponse(status_code=400, content={"detail": str(exc)})
        return await call_next(request)

    app.state.computer = computer
    app.state.terminal = terminal
    app.state.terminal_for = terminal_for

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
                    "profile": s.profile,
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
    async def observe(
        screen_id: str, body: ObserveBody, profile: str = Query(default="", pattern=PROFILE.pattern)
    ) -> dict[str, Any]:
        screen = await computer.screen(screen_id, label=body.label, profile=profile)
        try:
            snap = await computer.observe(screen)
        except ComputerError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        view = _view(screen, snap, computer.unseen_downloads(screen))
        if body.screenshot:
            image = await computer.bot_screenshot(screen)
            view["screenshot"] = base64.b64encode(image).decode("ascii")
        return view

    @app.post("/screens/{screen_id}/act")
    async def act(
        screen_id: str, body: ActBody, profile: str = Query(default="", pattern=PROFILE.pattern)
    ) -> dict[str, Any]:
        screen = await computer.screen(screen_id, label=body.label, profile=profile)
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
        # A click that starts a download returns before the file is saved. Waiting for
        # it (a bounded while) means the bot hears where it went with this result.
        for _ in range(40):
            if not screen.downloading:
                break
            await asyncio.sleep(0.25)
        return {
            **_view(screen, snap, computer.unseen_downloads(screen, consume=False)),
            "ok": True,
            "error": None,
        }

    @app.post("/screens/{screen_id}/fill")
    async def fill(
        screen_id: str, body: FillBody, profile: str = Query(default="", pattern=PROFILE.pattern)
    ) -> dict[str, Any]:
        screen = await computer.screen(screen_id, label=body.label, profile=profile)
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

    @app.post("/screens/{screen_id}/upload")
    async def upload(
        screen_id: str, body: UploadBody, profile: str = Query(default="", pattern=PROFILE.pattern)
    ) -> dict[str, Any]:
        screen = await computer.screen(screen_id, label=body.label, profile=profile)
        folder = computer.upload_dir(screen_id)
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir(parents=True, exist_ok=True)
        paths: list[Path] = []
        taken: set[str] = set()
        try:
            for item in body.files:
                data = base64.b64decode(item.data, validate=True)
                if len(data) > MAX_UPLOAD_BYTES:
                    raise ComputerError(f"{item.name} is over {MAX_UPLOAD_BYTES // 1_048_576} MB")
                target = folder / _upload_name(item.name, taken)
                target.write_bytes(data)
                paths.append(target)
            for raw in body.workspace:
                found = terminal_for(profile).resolve(raw)
                if not found.is_file():
                    raise ComputerError(f"there is no file {raw} in /workspace")
                paths.append(found)
        except (ComputerError, TerminalError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc).splitlines()[0]) from exc
        try:
            snap = await computer.upload(screen, body.element, paths)
        except HumanInControlError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ComputerError as exc:
            try:
                snap = await computer.observe(screen)
            except ComputerError:
                snap = {}
            return {**_view(screen, snap), "ok": False, "error": str(exc)}
        return {
            **_view(screen, snap),
            "ok": True,
            "error": None,
            "uploaded": [p.name for p in paths],
        }

    @app.get("/screens/{screen_id}/screenshot")
    async def screenshot(
        screen_id: str, quality: int = 70, profile: str = Query(default="", pattern=PROFILE.pattern)
    ) -> Response:
        screen = await computer.screen(screen_id, profile=profile)
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
    async def control(
        screen_id: str, body: ControlBody, profile: str = Query(default="", pattern=PROFILE.pattern)
    ) -> dict[str, Any]:
        screen = await computer.screen(screen_id, profile=profile)
        computer.set_controller(screen, body.controller)
        return {"screen_id": screen_id, "controller": screen.controller}

    @app.post("/screens/{screen_id}/input")
    async def human_input(
        screen_id: str, body: InputBody, profile: str = Query(default="", pattern=PROFILE.pattern)
    ) -> dict[str, Any]:
        screen = await computer.screen(screen_id, profile=profile)
        try:
            await computer.human_input(screen, body.model_dump(exclude_none=True))
        except ComputerError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)[:300]) from exc
        return {"ok": True, "url": screen.page.url}

    @app.post("/screens/{screen_id}/recording")
    async def recording(
        screen_id: str,
        body: RecordingBody,
        profile: str = Query(default="", pattern=PROFILE.pattern),
    ) -> dict[str, Any]:
        screen = await computer.screen(screen_id, profile=profile)
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
    async def recording_status(
        screen_id: str, profile: str = Query(default="", pattern=PROFILE.pattern)
    ) -> dict[str, Any]:
        screen = await computer.screen(screen_id, profile=profile)
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

    # --- the terminal and the workspace --------------------------------------------------

    @app.post("/terminal/run")
    async def terminal_run(body: TerminalBody) -> dict[str, Any]:
        try:
            ran = await terminal_for(body.profile).run(
                body.screen_id,
                body.command,
                timeout_s=body.timeout_s,
                local=body.local,
                network=body.network,
                secrets={k: v for k, v in body.secrets.items() if _ENV_NAME.match(k)},
            )
        except TerminalError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"ok": True, **ran.as_dict()}

    @app.get("/workspace")
    async def workspace_listing(
        path: str = "/workspace", profile: str = Query(default="", pattern=PROFILE.pattern)
    ) -> dict[str, Any]:
        terminal = terminal_for(profile)
        try:
            return {
                "path": terminal.shown(terminal.resolve(path)),
                "entries": terminal.listing(path),
            }
        except TerminalError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/workspace/file")
    async def workspace_read(
        path: str, profile: str = Query(default="", pattern=PROFILE.pattern)
    ) -> Response:
        terminal = terminal_for(profile)
        try:
            found, data = terminal.read(path)
        except TerminalError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return Response(
            content=data,
            media_type="application/octet-stream",
            headers={"X-Path": terminal.shown(found), "Cache-Control": "no-store"},
        )

    @app.post("/workspace/file")
    async def workspace_write(
        body: WorkspaceWrite, profile: str = Query(default="", pattern=PROFILE.pattern)
    ) -> dict[str, Any]:
        terminal = terminal_for(profile)
        try:
            data = base64.b64decode(body.data, validate=True)
            written = terminal.write(body.path, data)
        except (TerminalError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc).splitlines()[0]) from exc
        return {"path": terminal.shown(written), "bytes": len(data)}

    @app.delete("/screens/{screen_id}")
    async def close(screen_id: str) -> dict[str, Any]:
        await computer.close_screen(screen_id)
        return {"closed": screen_id}

    @app.post("/reset")
    async def reset() -> dict[str, Any]:
        await computer.reset()
        return {"ok": True}

    return app
