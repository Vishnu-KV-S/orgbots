"""Bots: the step schema, the approval gate, and `bot_agent@1` driven end to end.

No database here. The graph is run against an in-memory `BotService` stand-in and a
scripted model, so the loop's behaviour — what it writes to the conversation, when it
parks an action, how a resumed turn performs exactly the approved action — is checked
without Postgres. The browser half runs the *real* `browser.*` tool functions against
the real computer when one is listening on `RUNTIME_COMPUTER_URL` (`python -m
runtime.computer.main`); otherwise those tests skip and a scripted page stands in.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import pytest

import runtime.graphs.bot_agent  # noqa: F401  registers bot_agent@1
from runtime.domain.bots import (
    BotRule,
    BotStep,
    actor_name_for,
    is_secret_field,
    masked,
    message_id,
    needs_approval,
    trim_memory,
)
from runtime.gateway.models import ModelResponse
from runtime.graphs.registry import GRAPH_KEY, get_graph
from runtime.org.bots import BROWSER_TOOLS, bot_actor_spec

# --- domain ---------------------------------------------------------------------------


def test_step_requires_the_fields_its_action_needs() -> None:
    with pytest.raises(ValueError, match="element"):
        BotStep(thought="t", action="click")
    with pytest.raises(ValueError, match="url"):
        BotStep(thought="t", action="navigate")
    with pytest.raises(ValueError, match="text"):
        BotStep(thought="t", action="reply")
    step = BotStep(thought="t", action="type", element=4, text="hi", submit=True)
    assert step.browser_action() == {"type": "type", "element": 4, "text": "hi", "submit": True}


def test_ask_first_wins_over_allow() -> None:
    step = BotStep(thought="t", action="click", element=1)
    rules = (BotRule("click", "shop.com", "allow"), BotRule("*", "", "ask"))
    assert needs_approval(step, page_url="https://a.shop.com/x", element=None, rules=rules).ask


def test_allow_rule_covers_subdomains_and_beats_the_models_flag() -> None:
    step = BotStep(thought="t", action="click", element=1, sensitive=True)
    rules = (BotRule("click", "shop.com", "allow"),)
    assert not needs_approval(step, page_url="https://a.shop.com", element=None, rules=rules).ask
    assert needs_approval(step, page_url="https://other.com", element=None, rules=rules).ask


def test_secret_fields_ask_and_are_masked() -> None:
    step = BotStep(thought="t", action="type", element=2, text="hunter2")
    password = {"type": "password", "label": "Password"}
    assert is_secret_field(password)
    assert is_secret_field({"type": "text", "label": "One-time code (OTP)"})
    assert not is_secret_field({"type": "text", "label": "Search"})
    assert needs_approval(step, page_url="https://x.com", element=password, rules=()).ask
    shown = masked({"type": "type", "text": "hunter2", "secret": True})
    assert "hunter2" not in json.dumps(shown)


def test_navigate_is_judged_by_its_destination() -> None:
    step = BotStep(thought="t", action="navigate", url="https://bank.example/pay")
    rules = (BotRule("navigate", "bank.example", "ask"),)
    assert needs_approval(step, page_url="https://news.example", element=None, rules=rules).ask


def test_message_ids_are_stable_per_run_step_kind() -> None:
    run = uuid.uuid4()
    assert message_id(run, 3, "act") == message_id(run, 3, "act")
    assert message_id(run, 3, "act") != message_id(run, 4, "act")


def test_memory_is_bounded_and_deduplicated() -> None:
    memory = ""
    for i in range(400):
        memory = trim_memory(memory, f"note number {i} " + "x" * 20)
    assert len(memory) <= 4_000
    assert memory.endswith("x" * 20)
    assert trim_memory("- a", "a").count("a") == 1


def test_actor_spec_is_narrow() -> None:
    name = actor_name_for("Sales Scout!", "3f9a")
    assert name == "bot-sales-scout-3f9a"
    spec = bot_actor_spec(name)
    assert spec.allowed_tools == BROWSER_TOOLS
    assert spec.graph_ref == "bot_agent@1"
    assert all(p.provider == "deepseek" for p in spec.model_profiles.profiles.values())


# --- the graph, against fakes ----------------------------------------------------------


@dataclass
class _Bot:
    id: uuid.UUID
    name: str = "Scout"
    label: str = "Research"
    description: str = "Finds things on the web."
    instructions: str = ""
    memory: str = ""
    stop_requested: bool = False
    turn: int = 0


@dataclass
class _Message:
    role: str
    content: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Pending:
    id: uuid.UUID
    action: dict[str, Any]
    status: str = "pending"


class FakeBots:
    """The slice of `BotService` the graph uses, in memory."""

    def __init__(self, bot: _Bot, rules: tuple[BotRule, ...] = ()) -> None:
        self.bot = bot
        self._rules = rules
        self.log: dict[uuid.UUID, _Message] = {}
        self.pendings: dict[uuid.UUID, _Pending] = {}
        self.turns_ended = 0

    async def get(self, bot_id: uuid.UUID) -> _Bot:
        return self.bot

    async def record(self, bot_id, *, run_id, step, kind, role, content, payload=None):  # type: ignore[no-untyped-def]
        mid = message_id(run_id, step, kind)
        self.log.setdefault(mid, _Message(role, content, payload or {}))
        return mid

    async def conversation(self, bot_id: uuid.UUID) -> list[_Message]:
        return [m for m in self.log.values() if m.role in ("user", "bot")]

    async def rules(self, bot_id: uuid.UUID) -> tuple[BotRule, ...]:
        return self._rules

    async def remember(self, bot_id: uuid.UUID, note: str) -> str:
        self.bot.memory = trim_memory(self.bot.memory, note)
        return self.bot.memory

    async def park(self, bot_id, *, run_id, step, action, display, reason, thought):  # type: ignore[no-untyped-def]
        pid = uuid.uuid5(uuid.NAMESPACE_URL, f"{run_id}:{step}")
        self.pendings[pid] = _Pending(pid, action)
        await self.record(
            bot_id,
            run_id=run_id,
            step=step,
            kind="approval",
            role="approval",
            content=thought,
            payload={"pending_id": str(pid), "action": display},
        )
        return pid

    async def pending(self, pid: uuid.UUID) -> _Pending | None:
        return self.pendings.get(pid)

    async def end_turn(self, bot_id: uuid.UUID, *, needs_attention: bool = False) -> None:
        self.turns_ended += 1

    def said(self, role: str) -> list[_Message]:
        return [m for m in self.log.values() if m.role == role]


class ScriptedModel:
    def __init__(self, steps: list[dict[str, Any]]) -> None:
        self._steps = list(steps)
        self.prompts: list[str] = []

    async def complete(self, ctx, req, *, work_class, call_site):  # type: ignore[no-untyped-def]
        self.prompts.append(req.prompt)
        step = self._steps.pop(0)
        return ModelResponse(
            text=json.dumps(step),
            provider="fake",
            model="scripted",
            input_tokens=1,
            output_tokens=1,
            cost_cents=0,
        )


class FakePageGateway:
    """A two-page site, so the graph runs with no computer at all."""

    def __init__(self) -> None:
        self.url = "about:blank"
        self.calls: list[dict[str, Any]] = []

    def _page(self) -> dict[str, Any]:
        elements = (
            [
                {"id": 1, "tag": "input", "type": "password", "label": "Password", "in_view": True},
                {"id": 2, "tag": "button", "type": "submit", "label": "Log in", "in_view": True},
            ]
            if "login" in self.url
            else [{"id": 1, "tag": "a", "label": "Log in", "href": "/login", "in_view": True}]
        )
        return {
            "ok": True,
            "url": self.url,
            "title": "t",
            "controller": "bot",
            "rendered": f"URL: {self.url}",
            "elements": elements,
        }

    async def execute(self, ctx, call):  # type: ignore[no-untyped-def]
        self.calls.append({"tool": call.tool, **call.args})
        if call.tool == "browser.act@1":
            action = call.args["action"]
            assert "secret" not in action, "the secret flag must not reach the computer"
            if action["type"] == "navigate":
                self.url = action["url"]
            elif action["type"] == "click" and "login" not in self.url:
                self.url = self.url.rstrip("/") + "/login"
        return _Result(self._page())


@dataclass
class _Result:
    value: dict[str, Any]


class _Ctx:
    def __init__(self) -> None:
        self.run_id = uuid.uuid4()

    @contextlib.contextmanager
    def node(self, name: str, *, checkpoint_ns: str = "", iteration: int | None = None):  # type: ignore[no-untyped-def]
        yield None

    def log_fields(self) -> dict[str, object]:
        return {"run_id": str(self.run_id)}


@dataclass
class _Org:
    bots: FakeBots


@dataclass
class _Node:
    ctx: _Ctx
    gateway: Any
    models: Any
    org: _Org


async def _turn(node: _Node, bot_id: uuid.UUID, **extra: Any) -> dict[str, Any]:
    graph = get_graph("bot_agent@1")().compile()
    result = await graph.ainvoke(
        {"input": {"bot_id": str(bot_id), **extra}},
        config={"recursion_limit": 120, "configurable": {GRAPH_KEY: node}},
    )
    return dict(result.get("output", {}))


async def test_a_turn_parks_a_password_then_resumes_exactly_that_action() -> None:
    bot = _Bot(id=uuid.uuid4())
    bots = FakeBots(bot)
    page = FakePageGateway()
    model = ScriptedModel(
        [
            {"thought": "Open the site", "action": "navigate", "url": "https://site.test"},
            {"thought": "Go to login", "action": "click", "element": 1},
            {"thought": "Enter the password", "action": "type", "element": 1, "text": "hunter2"},
        ]
    )
    node = _Node(_Ctx(), page, model, _Org(bots))
    out = await _turn(node, bot.id)

    assert out["status"] == "awaiting_approval"
    (card,) = bots.said("approval")
    assert card.payload["action"]["secret"] is True
    assert "hunter2" not in json.dumps(card.payload)
    assert [c["action"]["type"] for c in page.calls if c["tool"] == "browser.act@1"] == [
        "navigate",
        "click",
    ]

    # The person allows it; a fresh run performs the parked action without asking the
    # model, then the model finishes the turn.
    (pending,) = bots.pendings.values()
    pending.status = "allowed"
    model2 = ScriptedModel([{"thought": "Done", "action": "reply", "text": "Logged in."}])
    node2 = _Node(_Ctx(), page, model2, _Org(bots))
    out2 = await _turn(node2, bot.id, resume_pending_id=str(pending.id))

    assert out2["status"] == "replied"
    typed = [c for c in page.calls if c["tool"] == "browser.act@1"][-1]["action"]
    assert typed == {"type": "type", "element": 1, "text": "hunter2", "submit": False}
    assert len(model2.prompts) == 1, "the approved action must not be re-decided"
    assert "hunter2" not in "".join(json.dumps(m.payload) + m.content for m in bots.log.values())
    assert bots.said("bot")[-1].content == "Logged in."


async def test_stop_ends_the_turn_before_the_next_step() -> None:
    bot = _Bot(id=uuid.uuid4(), stop_requested=True)
    bots = FakeBots(bot)
    node = _Node(_Ctx(), FakePageGateway(), ScriptedModel([]), _Org(bots))
    out = await _turn(node, bot.id)
    assert out["status"] == "stopped"
    assert bots.said("system")[-1].content == "Stopped."


async def test_a_newer_instruction_supersedes_a_running_turn() -> None:
    bot = _Bot(id=uuid.uuid4(), turn=5)
    bots = FakeBots(bot)
    model = ScriptedModel([])
    out = await _turn(_Node(_Ctx(), FakePageGateway(), model, _Org(bots)), bot.id, turn=4)
    assert out["status"] == "superseded"
    assert model.prompts == []


async def test_remember_writes_memory_and_continues() -> None:
    bot = _Bot(id=uuid.uuid4())
    bots = FakeBots(bot)
    model = ScriptedModel(
        [
            {"thought": "Note it", "action": "remember", "text": "Prefers window seats"},
            {"thought": "Tell them", "action": "reply", "text": "Noted."},
        ]
    )
    out = await _turn(_Node(_Ctx(), FakePageGateway(), model, _Org(bots)), bot.id)
    assert out["status"] == "replied"
    assert "Prefers window seats" in bot.memory


# --- the graph, against the real computer ----------------------------------------------


def _computer_url() -> str:
    return os.environ.get("RUNTIME_COMPUTER_URL", "http://127.0.0.1:8020")


def _listening(url: str) -> bool:
    parsed = urlparse(url)
    with (
        contextlib.suppress(OSError),
        socket.create_connection((parsed.hostname or "127.0.0.1", parsed.port or 80), timeout=0.5),
    ):
        return True
    return False


class DirectGateway:
    """The real `browser.*` tool functions, minus the governance pipeline (which needs
    Postgres). What is under test is the tool ↔ computer contract."""

    def __init__(self, url: str) -> None:
        from runtime.gateway.builtin.browser import build
        from runtime.settings import Settings

        self._tools = {
            f"{d.name}@{d.version}": (d, fn) for d, fn in build(Settings(computer_url=url))
        }

    async def execute(self, ctx, call):  # type: ignore[no-untyped-def]
        definition, fn = self._tools[call.tool]
        result = await fn(None, definition.args_model.model_validate(call.args))
        return _Result(result.model_dump())


@pytest.mark.skipif(not _listening(_computer_url()), reason="no computer running")
async def test_real_browser_fills_and_submits_a_form(tmp_path: Any) -> None:
    import http.server
    import threading

    (tmp_path / "index.html").write_text(
        "<title>Form</title><form action='/done.html'><label for=n>Name</label>"
        "<input id=n name=n><button>Send</button></form>"
    )
    (tmp_path / "done.html").write_text("<title>Done</title>Thanks!")
    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(  # noqa: E731
        *a, directory=str(tmp_path), **k
    )
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    site = f"http://127.0.0.1:{server.server_address[1]}/"

    try:
        bot = _Bot(id=uuid.uuid4())
        bots = FakeBots(bot)
        model = ScriptedModel(
            [
                {"thought": "Open it", "action": "navigate", "url": site},
                {"thought": "Name", "action": "type", "element": 1, "text": "Ada", "submit": True},
                {"thought": "Report", "action": "reply", "text": "Submitted."},
            ]
        )
        out = await _turn(_Node(_Ctx(), DirectGateway(_computer_url()), model, _Org(bots)), bot.id)
        assert out["status"] == "replied"
        # The third prompt is what the model saw after submitting: the done page.
        assert "Thanks!" in model.prompts[2]
        acts = bots.said("activity")
        assert all(m.payload.get("ok") for m in acts)
    finally:
        server.shutdown()
