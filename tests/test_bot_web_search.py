"""A bot that cannot find a feature asks the web, and keeps what it learned.

A bot sent to run Claude's Research mode looked through the model picker and the
permission menu for fifteen steps before it found the toggle behind +, and then never
wrote down where it was — so the next turn would have hunted again. `web_search` asks
the web without leaving the page, and a turn that searched is reminded to remember what
worked as a skill.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from runtime.domain.bots import BotStep
from runtime.domain.enums import WorkClass
from runtime.domain.errors import SpecError
from runtime.gateway.models import ModelResponse
from runtime.graphs.bot_agent.graph import _repeated, _unsaved_lesson
from runtime.graphs.bot_agent.search import NO_SEARCH
from runtime.org.bots import bot_actor_spec
from tests.test_bots import FakeBots, FakePageGateway, ScriptedModel, _Bot, _Ctx, _Node, _Org, _turn

HOW = "Click the + button beside the message box, then switch on Research."


class SearchingModel(ScriptedModel):
    """Decides steps from a script, and answers `web_search` calls on its own."""

    def __init__(self, steps: list[dict[str, Any]], *, searches: int) -> None:
        super().__init__(steps)
        self.searches = searches
        self.asked: list[Any] = []

    async def complete(self, ctx, req, *, work_class, call_site):  # type: ignore[no-untyped-def]
        if call_site != "bot_agent.web_search":
            return await super().complete(ctx, req, work_class=work_class, call_site=call_site)
        self.asked.append(req)
        return ModelResponse(
            text=HOW,
            provider="fake",
            model="scripted",
            input_tokens=1,
            output_tokens=1,
            cost_cents=0,
            server_searches=self.searches,
        )


class NoSearchEndpoint(FakePageGateway):
    async def execute(self, ctx, call):  # type: ignore[no-untyped-def]
        if call.tool == "web.search@1":
            self.calls.append({"tool": call.tool, **call.args})
            raise SpecError("web.search@1 is not configured")
        return await super().execute(ctx, call)


SEARCH = {
    "thought": "Not in the menus I opened; look it up",
    "action": "web_search",
    "text": "How do I turn on Research mode on claude.ai?",
}
REMEMBER = {
    "thought": "Keep where it is",
    "action": "remember",
    "memory_kind": "skill",
    "text": "claude.ai: Research mode = + beside the message box → Research toggle",
}
REPLY = {"thought": "Done", "action": "reply", "text": "Research is on."}


async def test_a_web_search_answers_beside_the_page_and_asks_for_the_lesson_to_be_kept() -> None:
    bot = _Bot(id=uuid.uuid4())
    page = FakePageGateway()
    model = SearchingModel([SEARCH, REMEMBER, REPLY], searches=2)
    out = await _turn(_Node(_Ctx(), page, model, _Org(FakeBots(bot))), bot.id)

    assert out["status"] == "replied"
    (asked,) = model.asked
    assert asked.tier == "search"
    assert "Research mode on claude.ai" in asked.prompt
    assert not [c for c in page.calls if c["tool"] == "browser.act@1"], "the page is kept"
    # The answer comes back, and until it is remembered the bot is told to keep it.
    assert HOW in model.prompts[1] and NO_SEARCH not in model.prompts[1]
    assert "searched the web" in model.prompts[1]
    assert "You looked up how to do something on the web" in model.prompts[1]
    assert "You looked up how to do something on the web" not in model.prompts[2]


async def test_a_search_that_could_not_run_says_its_answer_is_unchecked() -> None:
    bot = _Bot(id=uuid.uuid4())
    page = NoSearchEndpoint()
    model = SearchingModel([SEARCH, REPLY], searches=0)
    out = await _turn(_Node(_Ctx(), page, model, _Org(FakeBots(bot))), bot.id)

    assert out["status"] == "replied"
    # The front door was tried for real results, and was not set up either.
    assert [c["tool"] for c in page.calls if c["tool"].startswith("web.")] == ["web.search@1"]
    assert NO_SEARCH in model.prompts[1] and HOW in model.prompts[1]


def test_web_search_needs_a_question() -> None:
    with pytest.raises(ValidationError, match="web_search needs `text`"):
        BotStep.model_validate({"thought": "t", "action": "web_search"})


def test_a_bot_may_search_on_its_search_tier_only() -> None:
    spec = bot_actor_spec("bot-x")
    assert "web.search@1" in spec.allowed_tools
    profiles = spec.model_profiles
    assert profiles.for_work_class(WorkClass.WORK, "search").web_search is True
    assert profiles.for_work_class(WorkClass.WORK, "search").thinking is False
    assert profiles.for_work_class(WorkClass.WORK).web_search is False
    assert profiles.for_work_class(WorkClass.WORK, "think").web_search is False


def test_the_lesson_note_holds_until_something_is_remembered() -> None:
    assert _unsaved_lesson(["1. click [3] → ok"]) == ""
    searched = ["1. click [3] → ok", "2. searched the web: where is Research?"]
    assert "remember it" in _unsaved_lesson(searched)
    assert "remember it" in _unsaved_lesson([*searched, "3. click [7] → ok"])
    kept = [*searched, "3. saved in your memory: claude.ai: Research = + → Research"]
    assert _unsaved_lesson(kept) == ""


def test_going_round_in_circles_points_at_web_search() -> None:
    menu = "click 'Manual' → ok (now at https://claude.ai/new)"
    steps = [f"{i}. {line}" for i, line in enumerate([menu, "press Escape", menu, "x", menu])]
    assert "web_search" in _repeated(steps)
    assert "web_search" in _repeated([f"1. {menu}", f"2. {menu}"])


def test_a_step_written_as_tags_is_read_not_thrown_away() -> None:
    """DeepSeek, deep into a long prompt, answered a step as tags on every try — and
    the turn ended "confused" while Claude's research was still running."""
    from runtime.domain.bots import BOT_STEP
    from runtime.domain.schemas import SCHEMAS
    from runtime.graphs.common.structured import _as_object

    tags = (
        "<thought>\nResearch is still running.\n</thought>\n<action>wait</action>\n"
        "<seconds>30</seconds>\n<next_step>easy</next_step>"
    )
    step = _as_object(tags)
    assert step == {
        "thought": "Research is still running.",
        "action": "wait",
        "seconds": 30,
        "next_step": "easy",
    }
    assert SCHEMAS.get(BOT_STEP).check(step) == []
    assert _as_object("I will wait. <action>wait</action>") is None, "prose is not an answer"
