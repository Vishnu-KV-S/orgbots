"""Skills: the shared library, `/name` in a message, and teaching by demonstration.

Pure parts first, then the computer's recorder (a real browser when one is available),
the graph on the fakes from `test_bots.py`, and the library and API on Postgres
(`runtime_features_test`, never the dev database).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
import pytest_asyncio

from runtime.api.app import create_app
from runtime.api.bots import ORG_HEADER
from runtime.domain.bots import BotStep
from runtime.domain.skills import (
    SkillBody,
    SkillDraft,
    SkillError,
    check_name,
    describe_step,
    mentioned,
    render_index,
    render_recording,
    render_skill,
    slug,
)
from runtime.graphs.registry import GRAPH_KEY, get_graph
from runtime.org.skills import SkillService
from tests.test_bots import FakeBots, FakePageGateway, ScriptedModel, _Bot, _Ctx, _Node, _Org

# --- names, mentions and rendering --------------------------------------------------------


def test_a_skill_name_is_a_hyphenated_slug() -> None:
    assert check_name("/Weekly-Vendor-Check") == "weekly-vendor-check"
    assert slug("Weekly vendor price check!") == "weekly-vendor-price-check"
    for bad in ("a", "has space", "under_score", "-leading", "x" * 70):
        with pytest.raises(SkillError):
            check_name(bad)


def test_a_mention_is_a_slash_name_not_a_path() -> None:
    text = "Run /vendor-check on /notes/todo.md, then /compare-products and /vendor-check."
    assert mentioned(text) == ["vendor-check", "compare-products"]
    assert mentioned("see https://a.com/compare-products and a/b") == []


def test_a_loaded_skill_reads_as_a_procedure() -> None:
    body = SkillBody(
        title="Vendor check",
        when="Weekly",
        steps=["Open the vendor list", "  Note   prices  "],
        checks="Prices are today's",
        approvals="Never order",
    )
    text = render_skill("vendor-check", body, status="draft")
    assert text.startswith("SKILL /vendor-check — Vendor check")
    assert "Draft: not yet reviewed" in text
    assert "1. Open the vendor list\n2. Note prices" in text
    assert "Needs approval: Never order" in text
    index = render_index([("vendor-check", "Vendor check", "Weekly vendor prices")])
    assert "- /vendor-check — Weekly vendor prices" in index


def test_a_drafts_omitted_fields_keep_what_is_there() -> None:
    draft = SkillDraft(name="vendor-check", steps=["one"], checks="")
    assert draft.body_changes() == {"steps": ["one"]}
    with pytest.raises(ValueError, match="skill"):
        BotStep(thought="t", action="use_skill")


def test_a_recording_is_fenced_and_never_shows_a_secret() -> None:
    steps = [
        {"kind": "start", "url": "https://shop.test/"},
        {"kind": "click", "target": {"role": "button", "label": "Sign in"},
         "url": "https://shop.test/"},
        {"kind": "type", "target": {"tag": "input", "label": "Password", "secret": True},
         "text": "hunter2", "url": "https://shop.test/login"},
        {"kind": "click", "target": {"tag": "a", "label": "Orders",
                                     "href": "https://shop.test/orders"}},
        {"kind": "scroll", "dy": -300},
    ]
    text = render_recording("Download last month's invoices", steps)
    assert text.startswith("I just showed you how to do this task")
    assert "hunter2" not in text and "Typed ••• into input “Password”" in text
    assert "link “Orders” → https://shop.test/orders" in text
    assert "<<<RECORDING" in text and text.endswith("RECORDING>>>")
    assert describe_step({"kind": "scroll", "dy": -300}) == "Scrolled up"
    assert describe_step({"kind": "click", "target": None}) == "Clicked the page"


# --- the computer's recorder ---------------------------------------------------------------


def test_typing_and_scrolling_merge_and_a_secret_never_accumulates() -> None:
    from runtime.computer.browser import RECORDING_STEPS, Recording

    rec = Recording()
    field_ = {"tag": "input", "label": "Search"}
    rec.add({"kind": "type", "target": field_, "text": "robot ", "url": "u"})
    rec.add({"kind": "type", "target": field_, "text": "vacuum", "url": "u"})
    rec.add({"kind": "scroll", "dy": 300, "url": "u"})
    rec.add({"kind": "scroll", "dy": 200, "url": "u"})
    secret = {"tag": "input", "label": "Password", "secret": True}
    rec.add({"kind": "type", "target": secret, "text": "•••", "url": "u"})
    rec.add({"kind": "type", "target": secret, "text": "•••", "url": "u"})
    assert [s["kind"] for s in rec.steps] == ["type", "scroll", "type"]
    assert rec.steps[0]["text"] == "robot vacuum" and rec.steps[1]["dy"] == 500
    assert rec.steps[2]["text"] == "•••"

    for i in range(RECORDING_STEPS + 5):
        rec.add({"kind": "click", "target": {"label": str(i)}, "url": "u"})
    assert len(rec.steps) == RECORDING_STEPS and rec.expired()


def _chromium_available() -> bool:
    return bool(os.environ.get("COMPUTER_CHROMIUM_PATH")) or os.path.isfile(
        "/usr/bin/google-chrome"
    )


@pytest.mark.skipif(not _chromium_available(), reason="no Chromium to drive")
async def test_the_computer_records_what_a_person_does_but_not_their_password(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import http.server
    import threading

    from runtime.computer.browser import Computer

    if not os.environ.get("COMPUTER_CHROMIUM_PATH"):
        monkeypatch.setenv("COMPUTER_CHROMIUM_PATH", "/usr/bin/google-chrome")
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text(
        "<title>Shop</title>"
        "<input id=q aria-label='Search products' style='position:absolute;top:20px;left:20px'>"
        "<input id=p type=password aria-label=Password "
        "style='position:absolute;top:80px;left:20px'>"
        "<button style='position:absolute;top:140px;left:20px;width:120px;height:40px'>"
        "Find it</button>"
    )
    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(  # noqa: E731
        *a, directory=str(site), **k
    )
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    computer = Computer(tmp_path / "profile")
    try:
        screen = await computer.screen("s1")
        await screen.page.goto(f"http://127.0.0.1:{server.server_address[1]}/?token=abc")
        await computer.start_recording(screen)
        assert screen.controller == "human"
        await computer.human_input(screen, {"kind": "click", "x": 40, "y": 30})
        await computer.human_input(screen, {"kind": "type", "text": "robot"})
        await computer.human_input(screen, {"kind": "type", "text": " vacuum"})
        await computer.human_input(screen, {"kind": "click", "x": 40, "y": 90})
        await computer.human_input(screen, {"kind": "type", "text": "hunter2"})
        await computer.human_input(screen, {"kind": "click", "x": 60, "y": 160})
        steps = computer.stop_recording(screen)
    finally:
        await computer.stop()
        server.shutdown()

    kinds = [s["kind"] for s in steps]
    assert kinds == ["start", "click", "type", "click", "type", "click"]
    assert steps[2]["text"] == "robot vacuum"
    assert steps[2]["target"]["label"] == "Search products"
    assert steps[4]["text"] == "•••" and steps[4]["target"]["secret"] is True
    assert steps[5]["target"]["label"] == "Find it"
    assert "token" not in str(steps), "a recording drops query strings"
    assert "hunter2" not in str(steps)


@pytest.mark.skipif(not _chromium_available(), reason="no Chromium to drive")
async def test_a_recording_reads_the_live_input_a_taken_over_screen_sends(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The takeover pane sends raw input — moves, presses and releases, one key at a
    time — and a demonstration still comes out as clicks, typing per field and keys."""
    import http.server
    import threading

    from runtime.computer.browser import Computer

    if not os.environ.get("COMPUTER_CHROMIUM_PATH"):
        monkeypatch.setenv("COMPUTER_CHROMIUM_PATH", "/usr/bin/google-chrome")
    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text(
        "<title>Shop</title>"
        "<input id=q aria-label='Search products' style='position:absolute;top:20px;left:20px'>"
        "<input id=p type=password aria-label=Password "
        "style='position:absolute;top:80px;left:20px'>"
        "<button style='position:absolute;top:140px;left:20px;width:120px;height:40px'>"
        "Find it</button><div style='height:3000px'></div>"
    )
    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(  # noqa: E731
        *a, directory=str(site), **k
    )
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def click(x: float, y: float, clicks: int = 1) -> list[dict[str, Any]]:
        at = {"x": x, "y": y, "button": "left", "clicks": clicks}
        return [{"kind": "move", "x": x, "y": y}, {"kind": "down", **at}, {"kind": "up", **at}]

    def keys(*names: str) -> list[dict[str, Any]]:
        return [{"kind": kind, "key": k} for k in names for kind in ("keydown", "keyup")]

    def wheel(dy: float) -> dict[str, Any]:
        return {"kind": "wheel", "x": 200, "y": 300, "dx": 0, "dy": dy}

    computer = Computer(tmp_path / "profile")
    try:
        screen = await computer.screen("s1")
        await screen.page.goto(f"http://127.0.0.1:{server.server_address[1]}/")
        await computer.start_recording(screen)
        await computer.human_inputs(
            screen,
            [
                *click(40, 30),
                *click(40, 30, clicks=2),  # the second press of a double click
                *keys("r", "o", "b", "o", "t"),
                {"kind": "keydown", "key": "Control"},
                *keys("a"),
                {"kind": "keyup", "key": "Control"},
                *click(40, 90),
                *keys("h", "i"),
                *click(60, 160),
                wheel(120),
                wheel(120),
            ],
        )
        await computer.human_inputs(screen, [wheel(-240)])
        steps = computer.stop_recording(screen)
    finally:
        await computer.stop()
        server.shutdown()

    kinds = [s["kind"] for s in steps]
    assert kinds == ["start", "click", "type", "key", "click", "type", "click", "scroll"], steps
    assert steps[1]["target"]["label"] == "Search products"
    assert steps[2]["text"] == "robot" and steps[2]["target"]["label"] == "Search products"
    assert steps[3]["key"] == "Control+a"
    assert steps[5]["text"] == "•••" and steps[5]["target"]["secret"] is True
    assert steps[6]["target"]["label"] == "Find it"
    assert steps[7]["dy"] == 0, "scrolling down and back up is one step"


# --- the graph ----------------------------------------------------------------------------


@dataclass
class _SkillRow:
    id: uuid.UUID
    name: str
    status: str = "ready"
    version: int = 1
    title: str = ""
    steps: list[str] = field(default_factory=list)
    organization_id: Any = None

    def body(self) -> dict[str, Any]:
        return {"title": self.title, "when": "", "inputs": "", "steps": self.steps,
                "checks": "", "output": "", "approvals": ""}


@dataclass
class FakeSkills:
    rows: list[_SkillRow] = field(default_factory=list)
    used_: list[str] = field(default_factory=list)
    saved: list[tuple[str, Any]] = field(default_factory=list)

    async def index(self, organization_id: Any) -> list[tuple[str, str, str]]:
        return [(r.name, r.title, "") for r in self.rows if r.status == "ready"]

    async def by_name(self, organization_id: Any, name: str) -> _SkillRow | None:
        return next((r for r in self.rows if r.name == name), None)

    async def mentioned_in(self, organization_id: Any, text: str) -> list[_SkillRow]:
        return [r for n in mentioned(text) for r in self.rows if r.name == n]

    async def used(self, skills: list[_SkillRow]) -> None:
        self.used_ += [s.name for s in skills]

    async def save_draft(self, bot, draft, *, run_id, step, recording_id=None):  # type: ignore[no-untyped-def]
        from runtime.org.skills import SavedSkill

        row = _SkillRow(uuid.uuid4(), check_name(draft.name),
                        status="draft" if recording_id else "ready", steps=list(draft.steps))
        self.rows.append(row)
        self.saved.append((row.name, recording_id))
        return SavedSkill(row, "created")  # type: ignore[arg-type]


async def _invoke(node: _Node, **extra: Any) -> dict[str, Any]:
    graph = get_graph("bot_agent@1")().compile()
    result = await graph.ainvoke(
        {"input": {"bot_id": str(node.org.bots.bot.id), **extra}},
        config={"recursion_limit": 60, "configurable": {GRAPH_KEY: node}},
    )
    return dict(result.get("output", {}))


DONE = {"thought": "Done", "action": "reply", "text": "Done."}


async def test_a_skill_the_person_names_is_in_the_prompt_and_counted_once() -> None:
    bot = _Bot(id=uuid.uuid4())
    bots = FakeBots(bot)
    await bots.record(bot.id, run_id="r0", step=0, kind="m", role="user",
                      content="Please run /vendor-check for Acme.")
    skills = FakeSkills([_SkillRow(uuid.uuid4(), "vendor-check", steps=["Open acme.test"]),
                         _SkillRow(uuid.uuid4(), "draft-one", status="draft")])
    look = {"thought": "Reading", "action": "observe"}
    model = ScriptedModel([look, DONE])
    await _invoke(_Node(_Ctx(), FakePageGateway(), model, _Org(bots, skills=skills)))

    assert all("SKILL /vendor-check" in p and "1. Open acme.test" in p for p in model.prompts)
    assert skills.used_ == ["vendor-check"], "counted on the turn's first pass only"
    assert "- /vendor-check" in model.system[0]
    assert "/draft-one" not in model.system[0], "drafts are not offered to bots"


async def test_use_skill_loads_it_into_the_turns_results() -> None:
    bot = _Bot(id=uuid.uuid4())
    skills = FakeSkills([_SkillRow(uuid.uuid4(), "compare-products", steps=["Find 5 options"])])
    use = {"thought": "This matches", "action": "use_skill", "skill": {"name": "compare-products"}}
    missing = {"thought": "Try another", "action": "use_skill", "skill": {"name": "nope-skill"}}
    model = ScriptedModel([use, missing, DONE])
    bots = FakeBots(bot)
    await _invoke(_Node(_Ctx(), FakePageGateway(), model, _Org(bots, skills=skills)))
    assert "SKILL /compare-products" in model.prompts[1]
    assert "there is no skill /nope-skill (ready ones: /compare-products)" in model.prompts[2]


SAVE = {
    "thought": "Keeping this",
    "action": "save_skill",
    "skill": {"name": "invoice-download", "title": "Download invoices",
              "steps": ["Open the billing page", "Download each invoice"]},
}


async def test_a_demonstrations_write_up_is_saved_as_a_draft_tied_to_its_recording() -> None:
    bot = _Bot(id=uuid.uuid4())
    skills = FakeSkills()
    bots = FakeBots(bot)
    recording = uuid.uuid4()
    await _invoke(
        _Node(_Ctx(), FakePageGateway(), ScriptedModel([SAVE, DONE]), _Org(bots, skills=skills)),
        recording_id=str(recording),
    )
    assert skills.saved == [("invoice-download", recording)]
    assert skills.rows[0].status == "draft"
    (said,) = [m for m in bots.said("activity") if m.payload["action"]["type"] == "save_skill"]
    assert said.payload["status"] == "draft"


async def test_only_the_persons_own_turn_can_save_a_skill() -> None:
    bot = _Bot(id=uuid.uuid4())
    skills = FakeSkills()
    bots = FakeBots(bot)
    model = ScriptedModel([SAVE, DONE])
    await _invoke(
        _Node(_Ctx(), FakePageGateway(), model, _Org(bots, skills=skills)),
        routine_id="r", trigger="event",
    )
    assert not skills.rows
    assert "skills are saved only on your person's own request" in model.prompts[1]


# --- Postgres: the library -----------------------------------------------------------------


async def _bot(uow_factory: Any, organization_id: Any, name: str = "Scout") -> Any:
    from runtime.runtime.bootstrap import Registrar

    await Registrar(uow_factory).ensure_organization(organization_id, "skills")
    bot_id = uuid.uuid4()
    async with uow_factory.transaction() as uow:
        await uow.bots.create(
            bot_id, organization_id, actor_name=f"bot-s-{bot_id.hex[:6]}", name=name
        )
        return await uow.bots.get(bot_id)


async def test_the_library_holds_names_drafts_versions_and_mentions(
    uow_factory: Any, organization_id: Any
) -> None:
    bot = await _bot(uow_factory, organization_id)
    service = SkillService(uow_factory)
    made = await service.create(organization_id, "Vendor-Check", SkillBody(steps=["a"]))
    assert (made.name, made.status, made.version) == ("vendor-check", "ready", 1)
    with pytest.raises(SkillError, match="already a skill"):
        await service.create(organization_id, "VENDOR-CHECK", SkillBody(steps=["b"]))
    with pytest.raises(SkillError, match="at least one step"):
        await service.create(organization_id, "empty-one", SkillBody())

    # A demonstration's write-up: a draft, linked to its recording, out of the index.
    recording = uuid.uuid4()
    async with uow_factory.transaction() as uow:
        await uow.skills.start_recording(recording, organization_id, bot.id, "invoices")
    first = await service.save_draft(
        bot, SkillDraft(name="invoices", steps=["open billing"]), run_id="r1", step=2,
        recording_id=recording,
    )
    again = await service.save_draft(
        bot, SkillDraft(name="invoices", steps=["open billing"]), run_id="r1", step=2,
        recording_id=recording,
    )
    assert first.outcome == "created" and again.outcome == "unchanged"
    assert first.skill.status == "draft" and first.skill.source == "demonstration"
    assert [n for n, _, _ in await service.index(organization_id)] == ["vendor-check"]
    async with uow_factory() as uow:
        rec = await uow.skills.get_recording(recording)
    assert rec is not None and rec.skill_id == first.skill.id

    # A bot's change keeps the status; the person's review promotes it.
    changed = await service.save_draft(
        bot, SkillDraft(name="invoices", checks="totals match"), run_id="r2", step=0
    )
    assert changed.outcome == "updated"
    assert (changed.skill.status, changed.skill.version) == ("draft", 2)
    assert changed.skill.steps == ["open billing"] and changed.skill.updated_by_name == "Scout"
    ready = await service.update(changed.skill, status="ready")
    assert ready.status == "ready" and ready.version == 3

    named = await service.mentioned_in(organization_id, "do /invoices then /nope-nope")
    assert [s.name for s in named] == ["invoices"]


# --- the API ---------------------------------------------------------------------------------


@pytest_asyncio.fixture
async def api(
    settings: Any, uow_factory: Any, organization_id: Any
) -> AsyncIterator[httpx.AsyncClient]:
    from runtime.runtime.run_service import RunService

    app = create_app(settings)
    app.state.settings = settings
    app.state.uow = uow_factory
    app.state.service = RunService(uow_factory, settings=settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={ORG_HEADER: str(organization_id)},
    ) as http:
        yield http


async def test_a_person_writes_reviews_and_installs_skills(api: httpx.AsyncClient) -> None:
    made = await api.post(
        "/v1/skills", json={"title": "Weekly vendor check", "steps": ["Open the list"]}
    )
    assert made.status_code == 201, made.text
    skill = made.json()
    assert skill["name"] == "weekly-vendor-check" and skill["source"] == "person"

    patched = await api.patch(
        f"/v1/skills/{skill['id']}", json={"steps": ["Open the list", "Note prices"],
                                          "status": "draft"}
    )
    assert patched.json()["version"] == 2 and patched.json()["status"] == "draft"
    assert (await api.patch(f"/v1/skills/{skill['id']}", json={"steps": []})).status_code == 422

    market = (await api.get("/v1/marketplace")).json()["skills"]
    assert {"research-brief", "compare-products"} <= {m["key"] for m in market}
    installed = await api.post("/v1/marketplace/skills/research-brief")
    assert installed.status_code == 201 and installed.json()["source"] == "marketplace"
    assert (await api.post("/v1/marketplace/skills/research-brief")).status_code == 409
    listed = (await api.get("/v1/marketplace")).json()["skills"]
    assert next(m for m in listed if m["key"] == "research-brief")["installed"] is True

    library = (await api.get("/v1/skills")).json()["skills"]
    assert {s["name"] for s in library} == {"weekly-vendor-check", "research-brief"}
    assert (await api.delete(f"/v1/skills/{skill['id']}")).status_code == 200


async def test_teaching_records_hands_back_and_sends_the_bot_the_recording(
    api: httpx.AsyncClient, uow_factory: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    import runtime.api.skills as skills_api

    calls: list[tuple[str, str, Any]] = []
    recorded = [
        {"kind": "start", "url": "https://bank.test/"},
        {"kind": "click", "target": {"role": "link", "label": "Statements"}, "url": "u"},
        {"kind": "type", "target": {"label": "Password", "secret": True}, "text": "•••"},
    ]

    async def computer(request, method, path, *, json=None, quiet=False, timeout_s=30.0, profile=None):  # type: ignore[no-untyped-def]  # noqa: E501
        calls.append((method, path, json))
        if method == "POST" and json and json.get("action") == "stop":
            return httpx.Response(200, json={"recording": False, "steps": recorded})
        return httpx.Response(200, json={"recording": True})

    monkeypatch.setattr(skills_api, "_computer_call", computer)
    bot = (await api.post("/v1/bots", json={"name": "Teller"})).json()
    teach = f"/v1/bots/{bot['id']}/teach"

    started = await api.post(teach, json={"goal": "Download last month's statement"})
    assert started.status_code == 201, started.text
    assert (await api.post(teach, json={"goal": "again"})).status_code == 409
    assert calls[0] == ("POST", f"/screens/{bot['id']}/recording", {"action": "start"})

    stopped = await api.post(f"{teach}/stop", json={})
    body = stopped.json()
    assert body["status"] == "stopped" and body["steps"] == 2 and body["admitted"] is True
    assert calls[-1][2] == {"action": "stop", "hand_back": True}

    async with uow_factory() as uow:
        messages = await uow.bots.messages(uuid.UUID(bot["id"]), after_seq=0)
        rec = await uow.skills.get_recording(uuid.UUID(started.json()["recording_id"]))
        run = await uow.runs.get_spec(uuid.UUID(body["run_id"]))
    (said,) = [m for m in messages if m.role == "user"]
    assert said.payload["demonstration"] == "Download last month's statement"
    assert "Clicked link “Statements”" in said.content and "Typed •••" in said.content
    assert rec is not None and rec.status == "stopped" and len(rec.steps) == 3
    assert run is not None and run.spec["input"]["recording_id"] == started.json()["recording_id"]
    assert (await api.post(f"{teach}/stop", json={})).status_code == 409
