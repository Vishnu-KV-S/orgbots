"""The bots' shell: where their commands run, apart from the browser.

On the bots' computer (`docker/computer`) a command never runs beside Chrome. Chrome's
profiles hold the cookies of every site the bots are signed in to, and the browser is
driven over a debugging connection that anything on its machine could open; a command a
web page talked a bot into running must reach neither. So commands run in a sibling
container, the shell, which shares one thing with the desktop — the workspace volume,
where downloads land and work is kept — and has a network of its own: the internet, and
no route to the computer, its API or its desktop. The computer hands it each command over
a Unix socket on a volume only the two of them mount (`COMPUTER_SHELL_SOCKET`).

Inside, a command runs exactly as `runtime.computer.terminal` runs one anywhere: in its
browser profile's workspace, one at a time per bot, under bubblewrap, with the
organization's network policy and team secrets.

`python -m runtime.computer.shell` — reads RUNTIME_COMPUTER_WORKSPACE (the default
profile's workspace; others are its siblings) and COMPUTER_SHELL_SOCKET.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from runtime.computer.browser import PROFILE
from runtime.computer.terminal import DEFAULT_TIMEOUT_S, Terminal, TerminalError, workspace_for

SOCKET = "/run/shell/shell.sock"


class CommandBody(BaseModel):
    """One command, as the runtime's gateway sends it to the computer."""

    screen_id: str = Field(min_length=1, max_length=64)
    command: str = Field(min_length=1, max_length=20_000)
    timeout_s: float = Field(default=DEFAULT_TIMEOUT_S, ge=1, le=300)
    local: bool = False
    profile: str = Field(default="", pattern=PROFILE.pattern)
    network: bool = True
    secrets: dict[str, str] = Field(default_factory=dict, repr=False, max_length=100)
    """The organization's team secrets, as environment variables in the sandbox."""


def create_shell(base: Path) -> FastAPI:
    terminals: dict[str, Terminal] = {}
    app = FastAPI(title="agent-org shell")

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {"ok": True}

    @app.post("/run")
    async def run(body: CommandBody) -> dict[str, Any]:
        terminal = terminals.get(body.profile)
        if terminal is None:
            terminal = terminals[body.profile] = Terminal(workspace_for(base, body.profile))
        try:
            ran = await terminal.run(
                body.screen_id,
                body.command,
                timeout_s=body.timeout_s,
                local=body.local,
                network=body.network,
                secrets=body.secrets,
            )
        except TerminalError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"ok": True, **ran.as_dict()}

    return app


class RemoteShell:
    """The computer's side: commands sent to the shell container over its socket."""

    def __init__(self, socket: str) -> None:
        self._client = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=socket), base_url="http://shell"
        )

    async def run(self, body: CommandBody) -> dict[str, Any]:
        try:
            response = await self._client.post(
                "/run", json=body.model_dump(), timeout=body.timeout_s + 15
            )
        except httpx.TransportError as exc:
            raise TerminalError("the bots' shell is not running; commands cannot run") from exc
        if response.status_code == 422:
            raise TerminalError(str(response.json().get("detail", "the command was refused")))
        response.raise_for_status()
        return dict(response.json())

    async def close(self) -> None:
        await self._client.aclose()


def main() -> None:
    import uvicorn

    socket = os.environ.get("COMPUTER_SHELL_SOCKET", SOCKET)
    base = Path(os.environ.get("RUNTIME_COMPUTER_WORKSPACE", "/data/workspaces/workspace"))
    Path(socket).parent.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(FileNotFoundError):
        Path(socket).unlink()  # left by a shell that was stopped hard
    uvicorn.run(create_shell(base), uds=socket, log_level="info")


if __name__ == "__main__":
    main()
