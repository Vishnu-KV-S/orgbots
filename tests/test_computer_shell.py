"""The bots' shell (`runtime.computer.shell`): commands run apart from the browser.

On the bots' computer a command runs in a sibling container, never beside Chrome. Here
the shell runs on a Unix socket in-process, and the computer's `/terminal/run` must hand
every command to it — into the shell's workspace, not its own — with the team secrets,
in the bot's profile's workspace, and say plainly when the shell is not there.
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import uvicorn

from runtime.computer.app import create_app
from runtime.computer.shell import CommandBody, RemoteShell, create_shell

needs_bwrap = pytest.mark.skipif(shutil.which("bwrap") is None, reason="no bubblewrap")
MEMBER = "m-" + "a" * 32


@pytest.fixture
async def shell(tmp_path: Path) -> AsyncIterator[tuple[str, Path]]:
    """A shell on a socket, with its own workspace."""
    socket = tmp_path / "run" / "shell.sock"
    socket.parent.mkdir()
    workspace = tmp_path / "shell" / "workspace"
    server = uvicorn.Server(
        uvicorn.Config(create_shell(workspace), uds=str(socket), log_level="warning")
    )
    serving = asyncio.create_task(server.serve())
    for _ in range(100):
        if socket.exists():
            break
        await asyncio.sleep(0.05)
    try:
        yield str(socket), workspace
    finally:
        server.should_exit = True
        await serving


@needs_bwrap
async def test_a_command_runs_in_the_shells_workspace_with_the_team_secrets(
    shell: tuple[str, Path],
) -> None:
    socket, workspace = shell
    remote = RemoteShell(socket)
    try:
        ran = await remote.run(
            CommandBody(
                screen_id="bot",
                command="echo made > note.txt && pwd && echo $DEPLOY_KEY",
                secrets={"DEPLOY_KEY": "k-123"},
            )
        )
        assert ran["exit_code"] == 0, ran
        assert ran["stdout"].split() == ["/workspace", "k-123"]
        assert (workspace / "note.txt").read_text() == "made\n"

        # A member's bot works in that member's workspace, beside the default one.
        await remote.run(CommandBody(screen_id="bot", command="touch mine", profile=MEMBER))
        assert (workspace.with_name(f"workspace-{MEMBER}") / "mine").exists()
        assert not (workspace / "mine").exists()
    finally:
        await remote.close()


@needs_bwrap
async def test_the_computer_hands_every_command_to_the_shell(
    tmp_path: Path, shell: tuple[str, Path]
) -> None:
    socket, workspace = shell
    app = create_app(tmp_path / "profile", workspace=tmp_path / "computer-ws", shell_socket=socket)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://computer"
    ) as client:
        answer = await client.post(
            "/terminal/run",
            json={"screen_id": "bot", "command": "echo hi > from-computer.txt"},
        )
        assert answer.status_code == 200, answer.text
        assert (workspace / "from-computer.txt").exists()
        assert not (tmp_path / "computer-ws" / "from-computer.txt").exists()


async def test_a_shell_that_is_not_running_is_said_so(tmp_path: Path) -> None:
    app = create_app(
        tmp_path / "profile",
        workspace=tmp_path / "computer-ws",
        shell_socket=str(tmp_path / "nobody.sock"),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://computer"
    ) as client:
        answer = await client.post("/terminal/run", json={"screen_id": "bot", "command": "true"})
    assert answer.status_code == 422
    assert "shell is not running" in answer.json()["detail"]
