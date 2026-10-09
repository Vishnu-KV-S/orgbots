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
import datetime as dt
import json
import os
import socket
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import pytest

import runtime.graphs.bot_agent  # noqa: F401  registers bot_agent@1
from runtime.domain.bot_memory import (
    BotBrief,
    Memory,
    brief_changes,
    clean_memory,
    find_duplicate,
    search,
    select_for_prompt,
)
from runtime.domain.bots import (
    BotRule,
    BotStep,
    actor_name_for,
    is_secret_field,
    masked,
    message_id,
    needs_approval,
)
from runtime.gateway.models import ModelResponse
from runtime.graphs.registry import GRAPH_KEY, get_graph
from runtime.org.bots import (
    BROWSER_TOOLS,
    CONNECTOR_TOOLS,
    TERMINAL_TOOLS,
    BriefChange,
    BriefLockedError,
    Remembered,
    bot_actor_spec,
    brief_of,
)

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


def test_actor_spec_is_narrow() -> None:
    name = actor_name_for("Sales Scout!", "3f9a")
    assert name == "bot-sales-scout-3f9a"
    spec = bot_actor_spec(name)
    # The browser's two tools, the terminal's three, connector calls, and nothing else.
    assert spec.allowed_tools == BROWSER_TOOLS | TERMINAL_TOOLS | CONNECTOR_TOOLS
    assert spec.graph_ref == "bot_agent@1"
    assert all(p.provider == "deepseek" for p in spec.model_profiles.profiles.values())


# --- the graph, against fakes ----------------------------------------------------------


@dataclass
class _Bot:
    id: uuid.UUID
    name: str = "Scout"
    label: str = "Research"
    description: str = "Finds things on the web."
    brief: dict[str, Any] = field(default_factory=dict)
    brief_locked: bool = False
    stop_requested: bool = False
    turn: int = 0
    actor_name: str = "bot-scout-000000"
    parent_bot_id: uuid.UUID | None = None
    organization_id: uuid.UUID = field(default_factory=uuid.uuid4)
    team_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        # A bot nobody put on a team starts its own, as `bots.create` does.
        if self.team_id is None:
            self.team_id = self.id


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
        self.helpers_: list[_Bot] = []
        self.depth_ = 0
        self.refuse: str | None = None
        self.memory: dict[uuid.UUID, list[Memory]] = {}
        self.episodes: list[str] = []
        self.brief_edits: list[tuple[str, str, str, str]] = []
        self.notified: list[tuple[str, str, str]] = []
        self.screenshots: dict[uuid.UUID, tuple[str, bytes]] = {}

    async def get(self, bot_id: uuid.UUID) -> _Bot:
        return self._who(bot_id)

    async def record(self, bot_id, *, run_id, step, kind, role, content, payload=None):  # type: ignore[no-untyped-def]
        mid = message_id(run_id, step, kind)
        self.log.setdefault(mid, _Message(role, content, payload or {}))
        return mid

    async def conversation(self, bot_id: uuid.UUID) -> list[_Message]:
        return [m for m in self.log.values() if m.role in ("user", "bot")]

    async def rules(self, bot_id: uuid.UUID) -> tuple[BotRule, ...]:
        return self._rules

    # Memory and brief: the service's semantics, over dicts, using the same domain
    # functions the real service uses.

    def _who(self, bot_id: uuid.UUID) -> _Bot:
        return next(b for b in [self.bot, *self.helpers_] if b.id == bot_id)

    def mems(self, bot_id: uuid.UUID | None = None) -> list[Memory]:
        return self.memory.setdefault(bot_id or self.bot.id, [])

    async def recollect(self, bot_id, context, *, rehearse):  # type: ignore[no-untyped-def]
        return select_for_prompt(self.mems(bot_id), context, dt.datetime.now(dt.UTC))

    async def resolve(self, bot_id: uuid.UUID, handle: str) -> Memory | None:
        found = [m for m in self.mems(bot_id) if m.id.hex.startswith(handle.strip("[] "))]
        return found[0] if len(found) == 1 else None

    async def remember(self, bot_id, content, *, new_id, kind="fact", importance=3,  # type: ignore[no-untyped-def]
                       source_kind="self", source_name="", revise=None):
        mems = self.mems(bot_id)
        if revise:
            target = await self.resolve(bot_id, revise)
            if target is None:
                return Remembered(new_id, "unknown")
            mems[mems.index(target)] = Memory(
                target.id, kind, clean_memory(content), importance, target.pinned,
                target.created_at, None,
            )
            return Remembered(target.id, "revised")
        same = find_duplicate(mems, kind, content)
        if same is not None:
            return Remembered(same.id, "merged")
        mems.append(
            Memory(new_id, kind, clean_memory(content), importance, False,
                   dt.datetime.now(dt.UTC), None, 0, source_kind, source_name)
        )
        return Remembered(new_id, "saved")

    async def forget(self, bot_id: uuid.UUID, handle: str) -> Memory | None:
        target = await self.resolve(bot_id, handle)
        if target is not None:
            self.mems(bot_id).remove(target)
        return target

    async def recall(self, bot_id: uuid.UUID, query: str):  # type: ignore[no-untyped-def]
        return search(self.mems(bot_id), query, dt.datetime.now(dt.UTC)), []

    async def write_episode(self, bot_id, run_id, content, *, importance=2):  # type: ignore[no-untyped-def]
        self.episodes.append(content)

    async def update_brief(self, target, patch, *, revision, editor_kind, editor, reason):  # type: ignore[no-untyped-def]
        if target.brief_locked:
            raise BriefLockedError("locked by the person")
        before = brief_of(target)
        after = patch.apply(before)
        target.brief = after.model_dump()
        self.brief_edits.append((target.name, editor_kind, editor.name, reason))
        return BriefChange(after, 1, brief_changes(before, after))

    async def park(self, bot_id, *, run_id, step, action, display, reason, thought,  # type: ignore[no-untyped-def]
                   screenshot_id=None):
        pid = uuid.uuid5(uuid.NAMESPACE_URL, f"{run_id}:{step}")
        self.pendings[pid] = _Pending(pid, action)
        await self.record(
            bot_id,
            run_id=run_id,
            step=step,
            kind="approval",
            role="approval",
            content=thought,
            payload={"pending_id": str(pid), "action": display,
                     **({"screenshot_id": str(screenshot_id)} if screenshot_id else {})},
        )
        return pid

    async def save_screenshot(self, bot_id, *, run_id, step, kind, data, page_url=""):  # type: ignore[no-untyped-def]
        sid = uuid.uuid5(uuid.NAMESPACE_URL, f"botshot:{run_id}:{step}:{kind}")
        self.screenshots[sid] = (kind, data)
        return sid

    async def helpers(self, bot_id: uuid.UUID) -> list[_Bot]:
        return list(self.helpers_)

    async def depth(self, bot_id: uuid.UUID) -> int:
        return self.depth_

    async def create_helper(self, parent, *, run_id, step, name, label, role,  # type: ignore[no-untyped-def]
                            brief=None, seed_memories=None):
        from runtime.org.bots import HelperRefusedError

        if self.refuse:
            raise HelperRefusedError(self.refuse)
        helper = _Bot(
            id=uuid.uuid5(uuid.NAMESPACE_URL, f"{run_id}:{step}"),
            name=name,
            label=label,
            description=role,
            actor_name=f"bot-{name.lower()}-abc123",
            parent_bot_id=parent.id,
            organization_id=parent.organization_id,
            team_id=parent.team_id,
            brief=(brief or BotBrief(mission=role)).model_dump(),
        )
        self.helpers_.append(helper)
        for i, fact in enumerate(seed_memories or []):
            await self.remember(helper.id, fact, new_id=uuid.uuid5(helper.id, str(i)),
                                source_kind="parent", source_name=parent.name)
        return helper, True

    async def pending(self, pid: uuid.UUID) -> _Pending | None:
        return self.pendings.get(pid)

    async def end_turn(self, bot_id: uuid.UUID, *, needs_attention: bool = False) -> None:
        self.turns_ended += 1

    async def notify(self, bot, kind, body, *, run_id, step, url=""):  # type: ignore[no-untyped-def]
        self.notified.append((kind, body, url))

    def said(self, role: str) -> list[_Message]:
        return [m for m in self.log.values() if m.role == role]


class ScriptedModel:
    def __init__(self, steps: list[dict[str, Any]]) -> None:
        self._steps = list(steps)
        self.prompts: list[str] = []
        self.system: list[str] = []

    async def complete(self, ctx, req, *, work_class, call_site):  # type: ignore[no-untyped-def]
        self.prompts.append(req.prompt)
        self.system.append(req.system)
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


class EmptyDrive:
    """A team with nothing in its drive, for the tests that are not about files. The
    ones that are (`tests/test_team_files.py`) use the real `TeamDrive` on Postgres."""

    async def summary(self, team: Any, limit: int) -> tuple[int, list[Any]]:
        return 0, []


class NoRoutines:
    """A bot with no routines, for the tests that are not about them
    (`tests/test_bot_routines.py` is)."""

    async def for_bot(self, bot_id: uuid.UUID) -> list[Any]:
        return []


class NoSkills:
    """An organization with an empty skills library (`tests/test_bot_skills.py` has one)."""

    async def index(self, organization_id: Any) -> list[Any]:
        return []

    async def mentioned_in(self, organization_id: Any, text: str) -> list[Any]:
        return []

    async def used(self, skills: list[Any]) -> None:
        return None


class NoGroups:
    """A bot in no group, with no other bots (`tests/test_bot_groups.py` has some)."""

    async def peers(self, bot: Any) -> list[Any]:
        return []

    async def wake(self, wake_id: Any) -> None:
        return None


class OpenPolicies:
    """An organization with the default policies and no team secrets."""

    async def for_prompt(self, organization_id: Any) -> str:
        return ""


class NoConnectors:
    """An organization with no apps connected (`tests/test_bot_connectors.py` has some)."""

    async def usable(self, organization_id: Any) -> list[Any]:
        return []

    async def for_prompt(self, organization_id: Any) -> list[Any]:
        return []


@dataclass
class _Org:
    bots: FakeBots
    files: Any = field(default_factory=EmptyDrive)
    routines: Any = field(default_factory=NoRoutines)
    skills: Any = field(default_factory=NoSkills)
    groups: Any = field(default_factory=NoGroups)
    connectors: Any = field(default_factory=NoConnectors)
    policies: Any = field(default_factory=lambda: OpenPolicies())


@dataclass
class _Node:
    ctx: _Ctx
    gateway: Any
    models: Any
    org: _Org
    delegated: list[tuple[str, Any]] = field(default_factory=list)
    helper_reply: str = "Scout's answer: 3 vendors found."
    artifacts: Any = None
    """Only read when a result was externalised; the fake gateway never does that."""

    async def delegate(self, target_actor: str, context: Any, **_: Any) -> Any:
        from runtime.domain.delegation import DelegationOutcome

        self.delegated.append((target_actor, context))
        return DelegationOutcome(
            child_run_id=uuid.uuid4(),
            status="SUCCESS",
            output={"status": "replied", "reply": self.helper_reply},
        )


async def _turn(node: _Node, bot_id: uuid.UUID, **extra: Any) -> dict[str, Any]:
    graph = get_graph("bot_agent@1")().compile()
    result = await graph.ainvoke(
        {"input": {"bot_id": str(bot_id), **extra}},
        config={"recursion_limit": 120, "configurable": {GRAPH_KEY: node}},
    )
    return dict(result.get("output", {}))


async def test_a_turn_parks_a_consequential_click_then_resumes_exactly_that_action() -> None:
    """Park and resume. (A password used to be the example here; typing one is now a
    sign_in and never reaches the gate — see tests/test_vault.py.)"""
    bot = _Bot(id=uuid.uuid4())
    bots = FakeBots(bot)
    page = FakePageGateway()
    model = ScriptedModel(
        [
            {"thought": "Open the site", "action": "navigate", "url": "https://site.test"},
            {"thought": "Go to login", "action": "click", "element": 1},
            {"thought": "Submit it", "action": "click", "element": 2, "sensitive": True},
        ]
    )
    node = _Node(_Ctx(), page, model, _Org(bots))
    out = await _turn(node, bot.id)

    assert out["status"] == "awaiting_approval"
    (card,) = bots.said("approval")
    assert card.payload["action"]["type"] == "click"
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
    clicked = [c for c in page.calls if c["tool"] == "browser.act@1"][-1]["action"]
    assert clicked == {"type": "click", "element": 2}
    assert len(model2.prompts) == 1, "the approved action must not be re-decided"
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
    assert [m.content for m in bots.mems()] == ["Prefers window seats"]
    # The memory is in front of the model for the next step, with its [id].
    assert "Prefers window seats" in model.system[1]
    assert f"[{bots.mems()[0].handle}]" in model.system[1]
    # And the turn left a diary line.
    assert len(bots.episodes) == 1


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


# --- helper bots -----------------------------------------------------------------------


async def test_a_bot_creates_a_helper_asks_it_and_uses_the_answer() -> None:
    bot = _Bot(id=uuid.uuid4(), name="Lead")
    bots = FakeBots(bot)
    model = ScriptedModel(
        [
            {"thought": "Need a scout", "action": "create_bot", "bot": "Scout",
             "label": "Vendor scout", "text": "Find vendors and list them with links."},
            {"thought": "Ask it", "action": "ask_bot", "bot": "scout",
             "text": "Find CRM vendors under $50 per seat."},
            {"thought": "Report", "action": "reply", "text": "Scout found 3 vendors."},
        ]
    )
    node = _Node(_Ctx(), FakePageGateway(), model, _Org(bots))
    out = await _turn(node, bot.id)

    assert out["status"] == "replied"
    (helper,) = bots.helpers_
    assert helper.parent_bot_id == bot.id
    ((target, context),) = node.delegated
    assert target == helper.actor_name
    # The helper gets the task and who asked — not the lead's conversation.
    assert context.task.objective == "Find CRM vendors under $50 per seat."
    assert context.task.input["bot_id"] == str(helper.id)
    assert context.task.input["from_bot_name"] == "Lead"
    assert context.carries_history() is False
    # The answer is in front of the model for the step after it.
    assert "Scout's answer: 3 vendors found." in model.prompts[2]


async def test_a_delegated_turn_records_who_asked_and_returns_the_reply() -> None:
    helper = _Bot(id=uuid.uuid4(), name="Scout")
    bots = FakeBots(helper)
    model = ScriptedModel([{"thought": "Done", "action": "reply", "text": "Three vendors."}])
    node = _Node(_Ctx(), FakePageGateway(), model, _Org(bots))
    out = await _turn(
        node,
        helper.id,
        from_bot_id=str(uuid.uuid4()),
        from_bot_name="Lead",
        _delegation={"objective": "Find CRM vendors.", "facts": []},
    )
    assert out == {"status": "replied", "steps": 0, "kind": "reply", "reply": "Three vendors."}
    incoming = bots.said("user")[0]
    assert incoming.content == "Find CRM vendors."
    assert incoming.payload["from_bot_name"] == "Lead"
    assert "Lead (the bot that created you): Find CRM vendors." in model.prompts[0]


async def test_a_refused_helper_is_reported_to_the_bot_not_raised() -> None:
    bot = _Bot(id=uuid.uuid4())
    bots = FakeBots(bot)
    bots.refuse = "you already have 5 helpers"
    model = ScriptedModel(
        [
            {"thought": "t", "action": "create_bot", "bot": "Extra", "text": "help"},
            {"thought": "t", "action": "reply", "text": "I'll do it myself."},
        ]
    )
    out = await _turn(_Node(_Ctx(), FakePageGateway(), model, _Org(bots)), bot.id)
    assert out["status"] == "replied"
    (failed,) = [m for m in bots.said("activity") if m.payload.get("ok") is False]
    assert "5 helpers" in failed.payload["error"]
    assert "could not create helper Extra" in model.prompts[1]


async def test_asking_a_helper_that_does_not_exist_names_the_real_ones() -> None:
    bot = _Bot(id=uuid.uuid4())
    bots = FakeBots(bot)
    bots.helpers_.append(_Bot(id=uuid.uuid4(), name="Scout"))
    model = ScriptedModel(
        [
            {"thought": "t", "action": "ask_bot", "bot": "Writer", "text": "draft it"},
            {"thought": "t", "action": "reply", "text": "ok"},
        ]
    )
    node = _Node(_Ctx(), FakePageGateway(), model, _Org(bots))
    await _turn(node, bot.id)
    assert node.delegated == []
    assert "your helpers: Scout" in model.prompts[1]


def test_create_and_ask_need_a_name_and_text() -> None:
    with pytest.raises(ValueError, match="bot"):
        BotStep(thought="t", action="ask_bot", text="x")
    with pytest.raises(ValueError, match="role"):
        BotStep(thought="t", action="create_bot", bot="Scout")


# --- memory and the brief --------------------------------------------------------------


def _mem(content: str, kind: str = "fact", importance: int = 3, *, pinned: bool = False,
         days_old: float = 0.0) -> Memory:
    at = dt.datetime.now(dt.UTC) - dt.timedelta(days=days_old)
    return Memory(uuid.uuid4(), kind, content, importance, pinned, at, None)


def test_a_brief_renders_as_a_job_and_a_patch_changes_only_what_it_names() -> None:
    brief = BotBrief(
        mission="Book travel for Ada.",
        duties="- Find flights\nFind hotels\nFind flights",
        boundaries=["Never pay without asking"],
    )
    assert brief.duties == ["Find flights", "Find hotels"]
    text = brief.render()
    assert "Mission: Book travel for Ada." in text
    assert "Boundaries — never cross these:\n- Never pay without asking" in text

    from runtime.domain.bot_memory import BriefPatch

    after = BriefPatch(style="Short answers.").apply(brief)
    assert after.mission == brief.mission and after.style == "Short answers."
    assert brief_changes(brief, after) == ["style"]


def test_recall_keeps_pins_and_strong_preferences_and_ranks_the_rest() -> None:
    pinned = _mem("Ada's company is Lovelace Ltd", pinned=True, days_old=200)
    strong = _mem("Ada prefers aisle seats", "preference", 5, days_old=90)
    flights = _mem("The cheapest flights to Lisbon are on Tuesdays", days_old=30)
    cooking = _mem("Ada's favourite pasta recipe uses sage", days_old=1)
    picked = select_for_prompt(
        [cooking, flights, strong, pinned], "find me flights to Lisbon", dt.datetime.now(dt.UTC),
        budget=170,
    )
    assert picked.shown[:2] == [strong, pinned] or picked.shown[:2] == [pinned, strong]
    assert flights in picked.shown and cooking not in picked.shown
    assert picked.hidden == 1


def test_saving_the_same_thing_twice_strengthens_one_memory() -> None:
    first = _mem("Ada prefers window seats on long flights", "preference")
    assert find_duplicate([first], "preference", "ada prefers window seats on long flights.")
    assert find_duplicate([first], "fact", "Ada prefers window seats on long flights") is None
    assert find_duplicate([first], "preference", "Ada's dog is called Byron") is None


def test_forgetting_takes_the_weakest_and_never_a_pin() -> None:
    from runtime.domain.bot_memory import to_forget

    keep = _mem("pinned but ancient", importance=1, pinned=True, days_old=900)
    weak = _mem("trivial and old", importance=1, days_old=400)
    strong = _mem("essential and recent", importance=5)
    assert to_forget([keep, weak, strong], dt.datetime.now(dt.UTC), cap=2) == [weak]


def test_memory_and_brief_steps_need_their_fields() -> None:
    with pytest.raises(ValueError, match="memory"):
        BotStep(thought="t", action="forget")
    with pytest.raises(ValueError, match="brief"):
        BotStep(thought="t", action="update_brief", text="why")
    with pytest.raises(ValueError, match="why"):
        BotStep(thought="t", action="update_brief", brief={"style": "terse"})
    with pytest.raises(ValueError, match="text"):
        BotStep(thought="t", action="recall")


async def test_the_brief_is_the_primary_instruction_and_the_bot_can_revise_its_own() -> None:
    bot = _Bot(id=uuid.uuid4(), brief={"mission": "Track competitor prices.",
                                       "boundaries": ["Never buy anything"]})
    bots = FakeBots(bot)
    model = ScriptedModel(
        [
            {"thought": "They changed my job", "action": "update_brief",
             "brief": {"duties": ["Check prices every Monday"]},
             "text": "Ada asked me to do this weekly"},
            {"thought": "Done", "action": "reply", "text": "Updated.",
             "diary": "Ada made the price check weekly.", "importance": 4},
        ]
    )
    await _turn(_Node(_Ctx(), FakePageGateway(), model, _Org(bots)), bot.id)
    assert "YOUR PRIMARY INSTRUCTION" in model.system[0]
    assert "Mission: Track competitor prices." in model.system[0]
    assert bot.brief["duties"] == ["Check prices every Monday"]
    assert bot.brief["boundaries"] == ["Never buy anything"]
    assert "- Check prices every Monday" in model.system[1]
    assert bots.brief_edits == [("Scout", "self", "Scout", "Ada asked me to do this weekly")]
    assert bots.episodes == ["Ada made the price check weekly."]


async def test_a_parent_rewrites_its_helpers_brief_and_teaches_it_but_no_one_elses() -> None:
    lead = _Bot(id=uuid.uuid4(), name="Lead")
    bots = FakeBots(lead)
    helper = _Bot(id=uuid.uuid4(), name="Scout", parent_bot_id=lead.id)
    bots.helpers_.append(helper)
    model = ScriptedModel(
        [
            {"thought": "Refocus Scout", "action": "update_brief", "bot": "scout",
             "brief": {"mission": "Find EU vendors only."}, "text": "Ada only buys in the EU"},
            {"thought": "Teach it", "action": "remember", "bot": "Scout",
             "text": "Ada's budget is 50 EUR per seat", "memory_kind": "preference"},
            {"thought": "Not mine", "action": "update_brief", "bot": "Stranger",
             "brief": {"mission": "x"}, "text": "y"},
            {"thought": "Done", "action": "reply", "text": "ok"},
        ]
    )
    await _turn(_Node(_Ctx(), FakePageGateway(), model, _Org(bots)), lead.id)
    assert helper.brief["mission"] == "Find EU vendors only."
    assert bots.brief_edits[0][:3] == ("Scout", "parent", "Lead")
    (taught,) = bots.mems(helper.id)
    assert (taught.kind, taught.source_kind, taught.source_name) == ("preference", "parent", "Lead")
    assert bots.mems(lead.id) == []
    # The helper's own conversation says who changed it and why.
    told = [m.content for m in bots.said("system")]
    assert any("Lead updated my brief (mission): Ada only buys in the EU" in t for t in told)
    assert any("Lead taught me" in t for t in told)
    assert "Stranger is not you or one of your helpers" in model.prompts[3]


async def test_a_locked_brief_cannot_be_changed_by_a_bot() -> None:
    bot = _Bot(id=uuid.uuid4(), brief={"mission": "m"}, brief_locked=True)
    bots = FakeBots(bot)
    model = ScriptedModel(
        [
            {"thought": "t", "action": "update_brief", "brief": {"mission": "new"}, "text": "r"},
            {"thought": "t", "action": "reply", "text": "I can't."},
        ]
    )
    await _turn(_Node(_Ctx(), FakePageGateway(), model, _Org(bots)), bot.id)
    assert bot.brief == {"mission": "m"}
    assert "Locked by your person" in model.system[0]
    assert "update_brief failed: locked" in model.prompts[1]


async def test_forget_and_revise_by_id_and_recall_searches_everything() -> None:
    bot = _Bot(id=uuid.uuid4())
    bots = FakeBots(bot)
    wrong = _mem("Ada lives in Paris")
    old = _mem("Ada's dentist is Dr Crown, call 555-0101", "person", days_old=300)
    bots.mems().extend([wrong, old])
    model = ScriptedModel(
        [
            {"thought": "Moved", "action": "remember", "memory": wrong.handle,
             "text": "Ada lives in Lisbon"},
            {"thought": "Who's the dentist?", "action": "recall", "text": "dentist"},
            {"thought": "Stale", "action": "forget", "memory": old.handle},
            {"thought": "t", "action": "reply", "text": "Dr Crown."},
        ]
    )
    await _turn(_Node(_Ctx(), FakePageGateway(), model, _Org(bots)), bot.id)
    assert [m.content for m in bots.mems()] == ["Ada lives in Lisbon"]
    assert "Dr Crown, call 555-0101" in model.prompts[2]
    assert "forgot" in model.prompts[3]


async def test_a_helper_is_created_with_a_brief_and_starting_memories() -> None:
    lead = _Bot(id=uuid.uuid4(), name="Lead")
    bots = FakeBots(lead)
    model = ScriptedModel(
        [
            {"thought": "Need a scout", "action": "create_bot", "bot": "Scout",
             "text": "Find vendors.",
             "brief": {"mission": "Find CRM vendors.", "boundaries": ["Never sign up"]},
             "seed_memories": ["Ada's company has 40 seats"]},
            {"thought": "t", "action": "reply", "text": "ok"},
        ]
    )
    await _turn(_Node(_Ctx(), FakePageGateway(), model, _Org(bots)), lead.id)
    (helper,) = bots.helpers_
    assert helper.brief["mission"] == "Find CRM vendors."
    assert helper.brief["boundaries"] == ["Never sign up"]
    assert [m.content for m in bots.mems(helper.id)] == ["Ada's company has 40 seats"]
