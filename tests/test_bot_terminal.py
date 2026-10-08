"""The terminal: a sandboxed shell in a shared /workspace, commands on the person's own
machine with their approval, browser downloads, and copying files in and out.

The gate is pure; the sandbox is real (bubblewrap) and so is the browser when one is
installed; the graph runs on the fakes from `test_bots.py`; rules and approvals use
Postgres (`runtime_features_test`, never the dev database).
"""

from __future__ import annotations

import base64
import os
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from runtime.domain.bots import BotRule, BotStep, needs_approval
from runtime.graphs.registry import GRAPH_KEY, get_graph
from tests.test_bots import FakeBots, ScriptedModel, _Bot, _Ctx, _Node, _Org

# --- the gate ------------------------------------------------------------------------------


def _cmd(*, local: bool = False, sensitive: bool = False) -> BotStep:
    return BotStep(
        thought="t", action="run_command", text="ls", local=local, sensitive=sensitive
    )


def _gate(step: BotStep, *rules: BotRule) -> tuple[bool, bool]:
    decided = needs_approval(step, page_url="", element=None, rules=rules)
    return decided.ask, decided.deny


def test_the_sandbox_runs_and_the_persons_machine_asks() -> None:
    assert _gate(_cmd()) == (False, False)
    assert _gate(_cmd(local=True)) == (True, False)
    assert _gate(_cmd(sensitive=True)) == (True, False)


def test_always_allow_for_the_sandbox_is_not_always_allow_on_the_laptop() -> None:
    sandbox_ok = BotRule("run_command", "", "allow")
    local_ok = BotRule("run_local", "", "allow")
    assert _gate(_cmd(local=True), sandbox_ok) == (True, False)
    assert _gate(_cmd(local=True), local_ok) == (False, False)
    assert _gate(_cmd(sensitive=True), sandbox_ok) == (False, False)


def test_never_beats_ask_beats_allow_for_commands_and_clicks() -> None:
    never = BotRule("run_local", "", "deny")
    assert _gate(_cmd(local=True), never, BotRule("run_local", "", "allow")) == (False, True)
    assert _gate(_cmd(), BotRule("run_command", "", "ask"), BotRule("*", "", "allow")) == (
        True,
        False,
    )
    click = BotStep(thought="t", action="click", element=1)
    no_bank = BotRule("click", "bank.test", "deny")
    decided = needs_approval(click, page_url="https://www.bank.test/x", element=None,
                             rules=(no_bank, BotRule("*", "", "allow")))
    assert decided.deny and "never click on bank.test" in decided.reason
    assert not needs_approval(click, page_url="https://shop.test/", element=None,
                              rules=(no_bank,)).deny


def test_a_command_and_a_copy_need_their_fields() -> None:
    with pytest.raises(ValueError, match="run_command needs"):
        BotStep(thought="t", action="run_command")
    with pytest.raises(ValueError, match="copy_file needs"):
        BotStep(thought="t", action="copy_file", path="/workspace/a.csv")


# --- the sandbox ---------------------------------------------------------------------------

needs_bwrap = pytest.mark.skipif(shutil.which("bwrap") is None, reason="no bubblewrap")


@needs_bwrap
async def test_a_sandboxed_command_sees_only_the_workspace(tmp_path: Path) -> None:
    from runtime.computer.terminal import Terminal

    terminal = Terminal(tmp_path / "ws")
    (tmp_path / "secret.env").write_text("KEY=hunter2")
    ran = await terminal.run(
        "bot", "echo made > notes.txt; pwd; ls /home 2>&1; touch /usr/x 2>&1; env", timeout_s=20
    )
    assert ran.exit_code == 0 and ran.mode == "sandbox"
    assert "/workspace" in ran.stdout and "No such file or directory" in ran.stdout
    assert "Read-only file system" in ran.stdout
    assert "hunter2" not in ran.stdout and "RUNTIME_" not in ran.stdout
    assert (tmp_path / "ws" / "notes.txt").read_text() == "made\n"
    again = await terminal.run("other-bot", "cat notes.txt", timeout_s=20)
    assert again.stdout == "made\n", "the workspace is shared by every bot"


@needs_bwrap
async def test_a_command_is_stopped_with_its_children_and_keeps_what_it_printed(
    tmp_path: Path,
) -> None:
    from runtime.computer.terminal import OUTPUT_BYTES, Terminal

    terminal = Terminal(tmp_path / "ws")
    ran = await terminal.run("bot", "sleep 60 & echo started; sleep 60", timeout_s=1)
    assert ran.timed_out and ran.exit_code is None
    assert ran.stdout == "started\n" and "timeout" in ran.stderr
    assert ran.seconds < 10, "the background sleep must not hold the pipes open"
    big = await terminal.run("bot", "yes | head -c 300000", timeout_s=20)
    assert big.truncated and len(big.stdout) == OUTPUT_BYTES


async def test_a_local_command_has_no_runtime_secrets_and_can_be_switched_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runtime.computer.terminal import Terminal, TerminalError

    monkeypatch.setenv("RUNTIME_DEEPSEEK_API_KEY", "sk-should-not-leak")
    terminal = Terminal(tmp_path / "ws")
    ran = await terminal.run("bot", "pwd; env", timeout_s=20, local=True)
    assert ran.mode == "local" and str((tmp_path / "ws").resolve()) in ran.stdout
    assert "sk-should-not-leak" not in ran.stdout

    monkeypatch.setenv("COMPUTER_LOCAL_COMMANDS", "off")
    with pytest.raises(TerminalError, match="turned off"):
        await Terminal(tmp_path / "ws").run("bot", "pwd", timeout_s=5, local=True)


def test_workspace_paths_cannot_leave_it(tmp_path: Path) -> None:
    from runtime.computer.terminal import Terminal, TerminalError

    terminal = Terminal(tmp_path / "ws")
    (tmp_path / "ws" / "out").symlink_to("/etc")
    for bad in ("/workspace/../x", "/workspace/out/passwd", "../../etc/passwd"):
        with pytest.raises(TerminalError, match="outside /workspace"):
            terminal.resolve(bad)
    written = terminal.write("/workspace/reports/a.csv", b"a,b\n")
    assert terminal.shown(written) == "/workspace/reports/a.csv"
    assert terminal.read("reports/a.csv")[1] == b"a,b\n"
    assert [e["name"] for e in terminal.listing("/workspace")] == ["downloads", "reports", "out"]
    assert terminal.free_download_path("r?eport.pdf").name == "report.pdf"


@pytest.mark.skipif(
    not (os.environ.get("COMPUTER_CHROMIUM_PATH") or os.path.isfile("/usr/bin/google-chrome")),
    reason="no Chromium to drive",
)
async def test_a_download_lands_in_the_workspace_and_the_next_look_reports_it_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the computer's own HTTP surface, as the gateway calls it: the bot decides
    from observations, so the act that started a download must not use up its report."""
    import asyncio
    import http.server
    import threading

    import httpx

    from runtime.computer.app import create_app

    if not os.environ.get("COMPUTER_CHROMIUM_PATH"):
        monkeypatch.setenv("COMPUTER_CHROMIUM_PATH", "/usr/bin/google-chrome")
    site = tmp_path / "site"
    site.mkdir()
    (site / "report.csv").write_text("a,b\n1,2\n")
    (site / "index.html").write_text("<a id=d href='report.csv' download>Get the report</a>")
    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(  # noqa: E731
        *a, directory=str(site), **k
    )
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    app = create_app(tmp_path / "profile", workspace=tmp_path / "ws")
    await app.state.computer.start()
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://computer"
        ) as http_:
            url = f"http://127.0.0.1:{server.server_address[1]}/"
            await http_.post("/screens/bot/act", json={"action": {"type": "navigate", "url": url}})
            page = (await http_.post("/screens/bot/observe", json={})).json()
            link = next(
                e for e in page["snapshot"]["elements"] if "report" in e.get("label", "").lower()
            )
            acted = (
                await http_.post(
                    "/screens/bot/act", json={"action": {"type": "click", "element": link["id"]}}
                )
            ).json()
            await asyncio.sleep(0.5)
            first = (await http_.post("/screens/bot/observe", json={})).json()
            second = (await http_.post("/screens/bot/observe", json={})).json()
            listed = (await http_.get("/workspace", params={"path": "/workspace/downloads"})).json()
    finally:
        await app.state.computer.stop()
        server.shutdown()
    assert (tmp_path / "ws" / "downloads" / "report.csv").read_text() == "a,b\n1,2\n"
    assert acted["ok"] is True
    assert "saved /workspace/downloads/report.csv (8 bytes)" in first["rendered"]
    assert "DOWNLOADED" not in second["rendered"], "a download is reported once"
    assert [e["name"] for e in listed["entries"]] == ["report.csv"]


# --- the graph -----------------------------------------------------------------------------


@dataclass
class _R:
    value: dict[str, Any]


@dataclass
class ShellGateway:
    """The browser on a blank page, a shell that echoes, and a workspace in a dict."""

    files: dict[str, bytes] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def execute(self, ctx: Any, call: Any) -> _R:
        self.calls.append({"tool": call.tool, **call.args})
        if call.tool == "browser.observe@1":
            return _R({"ok": True, "url": "about:blank", "rendered": "", "elements": []})
        if call.tool == "terminal.run@1":
            return _R({"ok": True, "exit_code": 0, "stdout": f"ran: {call.args['command']}\n",
                       "stderr": "", "seconds": 0.1, "mode": "local" if call.args["local"]
                       else "sandbox"})
        if call.tool == "workspace.read@1":
            data = self.files.get(call.args["path"])
            if data is None:
                return _R({"ok": False, "error": "no such file"})
            return _R({"ok": True, "path": call.args["path"], "bytes": len(data),
                       "data": base64.b64encode(data).decode()})
        if call.tool == "workspace.write@1":
            self.files[call.args["path"]] = base64.b64decode(call.args["data"])
            return _R({"ok": True, "path": call.args["path"], "bytes": len(self.files[call.args["path"]])})
        raise AssertionError(call.tool)


@dataclass
class _File:
    id: uuid.UUID
    path: str
    chars: int = 10


@dataclass
class _Change:
    file: _File


@dataclass
class UploadDrive:
    uploaded: list[tuple[str, str, bytes, str]] = field(default_factory=list)
    blobs: dict[str, bytes] = field(default_factory=dict)

    async def summary(self, team: Any, limit: int) -> tuple[int, list[Any]]:
        return 0, []

    async def upload(self, team, folder, name, data, media_type, *, editor, op_id=None):  # type: ignore[no-untyped-def]
        self.uploaded.append((folder, name, data, media_type))
        return _Change(_File(uuid.uuid4(), f"{folder.rstrip('/')}/{name}"))

    async def blob_at(self, team: Any, path: str) -> tuple[_File, bytes]:
        return _File(uuid.uuid4(), path), self.blobs[path]


async def _invoke(node: _Node, **extra: Any) -> dict[str, Any]:
    graph = get_graph("bot_agent@1")().compile()
    result = await graph.ainvoke(
        {"input": {"bot_id": str(node.org.bots.bot.id), **extra}},
        config={"recursion_limit": 60, "configurable": {GRAPH_KEY: node}},
    )
    return dict(result.get("output", {}))


DONE = {"thought": "Done", "action": "reply", "text": "Done."}


async def test_a_sandboxed_command_runs_and_its_output_comes_back_fenced() -> None:
    bot = _Bot(id=uuid.uuid4())
    gateway = ShellGateway()
    run = {"thought": "Count rows", "action": "run_command", "text": "wc -l data.csv",
           "timeout": 30}
    model = ScriptedModel([run, DONE])
    bots = FakeBots(bot)
    await _invoke(_Node(_Ctx(), gateway, model, _Org(bots)))
    (call,) = [c for c in gateway.calls if c["tool"] == "terminal.run@1"]
    assert call["local"] is False and call["timeout_s"] == 30.0
    assert "<<<OUTPUT\nran: wc -l data.csv\nOUTPUT>>>" in model.prompts[1]
    (said,) = [m for m in bots.said("activity") if m.payload["action"]["type"] == "run_command"]
    assert said.payload["exit_code"] == 0 and "ran: wc -l" in said.payload["output"]


async def test_a_command_on_the_persons_machine_waits_for_them_then_runs() -> None:
    bot = _Bot(id=uuid.uuid4())
    gateway = ShellGateway()
    local = {"thought": "Open it", "action": "run_command", "text": "open ~/Downloads",
             "local": True}
    bots = FakeBots(bot)
    out = await _invoke(_Node(_Ctx(), gateway, ScriptedModel([local]), _Org(bots)))
    assert out["status"] == "awaiting_approval"
    assert not [c for c in gateway.calls if c["tool"] == "terminal.run@1"]
    (pid, pending), = bots.pendings.items()
    assert pending.action["rule"] == "run_local" and pending.action["command"] == "open ~/Downloads"
    (card,) = bots.said("approval")
    assert card.payload["action"]["host"] == "your computer"

    pending.status = "allowed"
    out = await _invoke(
        _Node(_Ctx(), gateway, ScriptedModel([DONE]), _Org(bots)), resume_pending_id=str(pid)
    )
    (call,) = [c for c in gateway.calls if c["tool"] == "terminal.run@1"]
    assert call["local"] is True and out["status"] == "replied"


async def test_never_allow_refuses_a_command_without_asking() -> None:
    bot = _Bot(id=uuid.uuid4())
    gateway = ShellGateway()
    bots = FakeBots(bot, rules=(BotRule("run_local", "", "deny"),))
    local = {"thought": "x", "action": "run_command", "text": "rm -rf ~/tmp", "local": True}
    model = ScriptedModel([local, DONE])
    out = await _invoke(_Node(_Ctx(), gateway, model, _Org(bots)))
    assert out["status"] == "replied" and not bots.pendings
    assert "run_command refused: your rule: never running commands on your machine" in (
        model.prompts[1]
    )


async def test_copy_file_moves_a_download_into_the_drive_and_a_file_out() -> None:
    bot = _Bot(id=uuid.uuid4())
    gateway = ShellGateway(files={"/workspace/downloads/report.pdf": b"%PDF-1.4 x"})
    drive = UploadDrive(blobs={"/data/in.csv": b"a,b\n"})
    steps = [
        {"thought": "Keep it", "action": "copy_file", "path": "/workspace/downloads/report.pdf",
         "to": "/reports/"},
        {"thought": "Process it", "action": "copy_file", "path": "/data/in.csv",
         "to": "/workspace/in.csv"},
        {"thought": "Wrong", "action": "copy_file", "path": "/a.md", "to": "/b.md"},
        DONE,
    ]
    model = ScriptedModel(steps)
    await _invoke(_Node(_Ctx(), gateway, model, _Org(FakeBots(bot), files=drive)))
    assert drive.uploaded == [("/reports", "report.pdf", b"%PDF-1.4 x", "application/pdf")]
    assert gateway.files["/workspace/in.csv"] == b"a,b\n"
    assert "Copied /workspace/downloads/report.pdf to /reports/report.pdf" in model.prompts[1]
    assert "exactly one of path and to starts with /workspace" in model.prompts[3]


# --- approvals in Postgres ------------------------------------------------------------------


async def test_always_allow_on_a_local_command_files_a_run_local_rule(
    uow_factory: Any, organization_id: Any, settings: Any
) -> None:
    from runtime.runtime.bootstrap import Registrar
    from runtime.runtime.bots import BotManager
    from runtime.runtime.run_service import RunService

    await Registrar(uow_factory).ensure_organization(organization_id, "terminal")
    manager = BotManager(uow_factory, RunService(uow_factory, settings=settings))
    bot = await manager.create(organization_id, name="Ops")
    pid = uuid.uuid4()
    async with uow_factory.transaction() as uow:
        await uow.bots.add_pending(
            pid, bot.id, run_id=None,
            action={"type": "run_command", "command": "make", "local": True,
                    "rule": "run_local", "host": ""},
            reason="the command runs on your own machine",
        )
    await manager.decide(bot.id, pid, "always")
    async with uow_factory() as uow:
        (rule,) = await uow.bots.rules(bot.id)
        await uow.bots.put_rule(bot.id, "run_local", "", "deny")
    assert (rule.action_type, rule.host, rule.decision) == ("run_local", "", "allow")
    async with uow_factory.transaction() as uow:
        await uow.bots.put_rule(bot.id, "click", "bank.test", "deny")
        decisions = {r.action_type: r.decision for r in await uow.bots.rules(bot.id)}
    assert decisions["click"] == "deny", "the database takes the third decision"
