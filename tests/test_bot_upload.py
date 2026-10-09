"""Uploads: a bot gives a website files — a team drive file or one in /workspace.

The computer is checked in real Chrome against a local page with both shapes sites use:
a plain file field, and a hidden one behind an "Select from computer" button. The
gateway is checked reading a real team drive file for a real run; the graph, that an
upload waits for the person like a post would.
"""

from __future__ import annotations

import base64
import json
import os
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from runtime.domain.bots import BotRule, BotStep, needs_approval
from runtime.domain.review import review_kind
from tests.test_bots import FakeBots, FakePageGateway, ScriptedModel, _Bot, _Ctx, _Node, _Org, _turn

# --- the step ----------------------------------------------------------------------------------


def test_an_upload_names_its_element_and_files_and_asks_first() -> None:
    with pytest.raises(ValueError, match="needs `element`"):
        BotStep(thought="t", action="upload", path="/a.png")
    with pytest.raises(ValueError, match="needs `path`"):
        BotStep(thought="t", action="upload", element=3)
    step = BotStep(
        thought="t", action="upload", element=3, path="/a.png", paths=["/b.png", "/a.png"]
    )
    assert step.browser_action() == {"type": "upload", "element": 3, "paths": ["/a.png", "/b.png"]}

    asked = needs_approval(step, page_url="https://www.instagram.com/", element=None, rules=())
    assert asked.ask and "sends your file to www.instagram.com" in asked.reason
    allowed = needs_approval(
        step,
        page_url="https://www.instagram.com/",
        element=None,
        rules=(BotRule("upload", "instagram.com", "allow"),),
    )
    assert not allowed.ask, "the person's 'always allow' for that site"
    never = needs_approval(
        step, page_url="https://x.test/", element=None, rules=(BotRule("upload", "", "deny"),)
    )
    assert never.deny
    assert review_kind(step) == "sending one of your person's files to a website"


# --- the computer, in Chrome -------------------------------------------------------------------

PAGE = """<!doctype html><title>Upload</title>
<input type=file id=plain multiple aria-label="Choose files">
<button id=pick>Select from computer</button>
<input type=file id=hidden style="display:none">
<button id=nothing>Does nothing</button>
<p id=out>none</p>
<script>
const show = async (input) => {
  const parts = [];
  for (const f of input.files) parts.push(f.name + ":" + f.size + ":" + (await f.text()).trim());
  document.getElementById('out').textContent = input.id + " got " + parts.join(" | ");
};
document.getElementById('plain').addEventListener('change', e => show(e.target));
document.getElementById('hidden').addEventListener('change', e => show(e.target));
document.getElementById('pick').addEventListener('click', () =>
  document.getElementById('hidden').click());
</script>"""


async def test_the_computer_fills_a_file_field_and_answers_a_buttons_file_chooser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import http.server
    import threading

    from runtime.computer.app import create_app as computer_app

    if not os.environ.get("COMPUTER_CHROMIUM_PATH"):
        monkeypatch.setenv("COMPUTER_CHROMIUM_PATH", "/usr/bin/google-chrome")

    class Site(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(PAGE.encode())

        def log_message(self, *args: Any) -> None:
            return None

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    profile = f"m-{uuid.uuid4().hex}"
    workspace = tmp_path / f"ws-{profile}"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("from the workspace")
    app = computer_app(tmp_path / "profile", workspace=tmp_path / "ws")
    await app.state.computer.start()

    def b64(text: str) -> str:
        return base64.b64encode(text.encode()).decode()

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://computer"
        ) as http_:
            q = {"profile": profile}
            await http_.post(
                "/screens/s/act",
                params=q,
                json={
                    "action": {
                        "type": "navigate",
                        "url": f"http://127.0.0.1:{server.server_address[1]}/",
                    }
                },
            )
            page = (await http_.post("/screens/s/observe", params=q, json={})).json()
            ids = {e.get("label", ""): e["id"] for e in page["snapshot"]["elements"]}
            field = next(v for k, v in ids.items() if "Choose files" in k)
            button = ids["Select from computer"]
            plain = (
                await http_.post(
                    "/screens/s/upload",
                    params=q,
                    json={
                        "element": field,
                        "files": [
                            {"name": "a.txt", "data": b64("alpha")},
                            {"name": "../../b.txt", "data": b64("beta")},
                        ],
                        "workspace": ["/workspace/notes.txt"],
                    },
                )
            ).json()
            chooser = (
                await http_.post(
                    "/screens/s/upload",
                    params=q,
                    json={"element": button, "files": [{"name": "photo.png", "data": b64("png!")}]},
                )
            ).json()
            none = (
                await http_.post(
                    "/screens/s/upload",
                    params=q,
                    json={
                        "element": ids["Does nothing"],
                        "files": [{"name": "x.txt", "data": b64("x")}],
                    },
                )
            ).json()
            two = (
                await http_.post(
                    "/screens/s/upload",
                    params=q,
                    json={
                        "element": button,
                        "files": [
                            {"name": "1.txt", "data": b64("1")},
                            {"name": "2.txt", "data": b64("2")},
                        ],
                    },
                )
            ).json()
            missing = await http_.post(
                "/screens/s/upload",
                params=q,
                json={"element": field, "workspace": ["/workspace/nope.txt"]},
            )
            await http_.post("/screens/s/control", params=q, json={"controller": "human"})
            held = await http_.post(
                "/screens/s/upload",
                params=q,
                json={"element": field, "files": [{"name": "y.txt", "data": b64("y")}]},
            )
            upload_dir = app.state.computer.upload_dir("s")
            kept = sorted(p.name for p in upload_dir.iterdir())
            await http_.delete("/screens/s")
    finally:
        await app.state.computer.stop()
        server.shutdown()

    assert plain["ok"] is True and plain["uploaded"] == ["a.txt", "b.txt", "notes.txt"]
    assert (
        "plain got a.txt:5:alpha | b.txt:4:beta | notes.txt:18:from the workspace"
        in (plain["rendered"])
    ), "the page read the files' contents"
    assert chooser["ok"] is True and "hidden got photo.png:4:png!" in chooser["rendered"]
    assert none["ok"] is False and "did not open a file chooser" in none["error"]
    assert two["ok"] is False and "takes one file" in two["error"]
    assert missing.status_code == 422 and "no file /workspace/nope.txt" in missing.text
    assert held.status_code == 409, "not while a person holds the screen"
    assert kept == ["y.txt"], "the files wait until the next upload (a site may read them again)"
    assert not upload_dir.exists(), "closing the screen clears them"


# --- the gateway: the bot's own team drive ------------------------------------------------------


async def test_the_browser_tool_sends_a_team_file_and_passes_workspace_paths(
    uow_factory: Any, settings: Any, organization_id: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runtime.domain.files import Editor, team_of
    from runtime.gateway.builtin import browser
    from runtime.gateway.tools import ToolContext
    from runtime.org.files import TeamDrive
    from runtime.runtime.bootstrap import Registrar
    from runtime.runtime.bots import BotManager
    from runtime.runtime.run_service import RunService

    await Registrar(uow_factory).ensure_organization(organization_id, "x")
    manager = BotManager(uow_factory, RunService(uow_factory, settings=settings))
    bot = await manager.create(organization_id, name="Social")
    other = await manager.create(organization_id, name="Other")
    image = b"\x89PNG\r\n fake image bytes"
    await TeamDrive(uow_factory).upload(
        team_of(bot), "/attachments", "post.png", image, "image/png", editor=Editor("person")
    )
    run = (await manager.send(bot.id, "post it")).run_id
    ctx = ToolContext(
        run_id=str(run),
        organization_id=str(organization_id),
        logical_call_id="c",
        idempotency_key="k",
        marker="m",
        attempt=1,
    )
    sent: list[dict[str, Any]] = []

    def computer(request: httpx.Request) -> httpx.Response:
        sent.append({"path": request.url.path, **json.loads(request.content)})
        return httpx.Response(200, json={"ok": True, "snapshot": {}, "url": "u", "rendered": ""})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        browser.httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(computer), **kw),
    )
    (_, _), (_, act) = browser.build(settings, uow_factory)
    done = await act(
        ctx,
        browser.ActArgs(
            screen_id=str(bot.id),
            action={
                "type": "upload",
                "element": 7,
                "paths": ["/attachments/post.png", "/workspace/a.csv"],
            },
        ),
    )
    assert done.ok is True
    (call,) = sent
    assert call["path"] == f"/screens/{bot.id}/upload" and call["element"] == 7
    assert call["files"] == [{"name": "post.png", "data": base64.b64encode(image).decode()}]
    assert call["workspace"] == ["/workspace/a.csv"]

    missing = await act(
        ctx,
        browser.ActArgs(
            screen_id=str(bot.id),
            action={"type": "upload", "element": 7, "paths": ["/attachments/nope.png"]},
        ),
    )
    assert missing.ok is False and "nope.png" in (missing.error or "")
    stolen = await act(
        ctx,
        browser.ActArgs(
            screen_id=str(other.id),
            action={"type": "upload", "element": 7, "paths": ["/attachments/post.png"]},
        ),
    )
    assert stolen.ok is False and "its own screen" in (stolen.error or "")
    assert len(sent) == 1


# --- the graph -----------------------------------------------------------------------------------


async def test_an_upload_waits_for_the_person_then_runs_exactly_as_approved() -> None:
    bot = _Bot(id=uuid.uuid4())
    bots = FakeBots(bot)
    page = FakePageGateway()
    model = ScriptedModel(
        [
            {"thought": "Open the site", "action": "navigate", "url": "https://social.test"},
            {
                "thought": "Give it the photo",
                "action": "upload",
                "element": 1,
                "path": "/attachments/post.png",
            },
        ]
    )
    out = await _turn(_Node(_Ctx(), page, model, _Org(bots)), bot.id)
    assert out["status"] == "awaiting_approval"
    (card,) = bots.said("approval")
    assert card.payload["action"]["type"] == "upload"
    assert card.payload["action"]["host"] == "social.test"
    assert [c["action"]["type"] for c in page.calls if c["tool"] == "browser.act@1"] == [
        "navigate"
    ], "nothing was sent before the person said yes"

    (pending,) = bots.pendings.values()
    pending.status = "allowed"
    model2 = ScriptedModel([{"thought": "Done", "action": "reply", "text": "Uploaded."}])
    await _turn(_Node(_Ctx(), page, model2, _Org(bots)), bot.id, resume_pending_id=str(pending.id))
    uploaded = [c for c in page.calls if c["tool"] == "browser.act@1"][-1]["action"]
    assert uploaded == {"type": "upload", "element": 1, "paths": ["/attachments/post.png"]}
