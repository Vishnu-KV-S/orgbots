"""A bot's step goes to the model its difficulty calls for (`graphs.bot_agent.tiers`).

Flash for a step with nothing to work out, Pro for the usual one, Pro thinking when the
bot is stuck — and the spec, not the call site, says which model each tier is.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from runtime.domain.bots import BotStep
from runtime.domain.enums import WorkClass
from runtime.domain.hashing import canonical_hash
from runtime.domain.specs import ModelProfile, ModelProfiles
from runtime.gateway.models import ModelResponse
from runtime.graphs.bot_agent.tiers import QUICK, THINK, place
from runtime.org.bots import bot_actor_spec
from runtime.org.department import FLASH, PRO
from tests.test_bots import FakeBots, FakePageGateway, ScriptedModel, _Bot, _Ctx, _Node, _Org, _turn

STUCK = "3. click [92] 'Effort Medium' → ok, but nothing on the page changed (now at x)"


def test_small_talk_is_quick_and_a_task_is_usual() -> None:
    assert place(n=0, steps=[], request="hi!").tier == QUICK
    assert place(n=0, steps=[], request="thanks").tier == QUICK
    assert place(n=0, steps=[], request="Find me a flight to Goa on Friday").tier is None
    # A "hi" in the middle of a task is not small talk to answer cheaply.
    assert place(n=0, steps=[], request="hi", has_plan=True).tier is None


def test_trouble_thinks_and_overrides_an_easy_hint() -> None:
    assert place(n=3, steps=[STUCK], request="x", hint="easy").tier == THINK
    assert place(n=3, steps=[], request="x", repeated="WARNING: …").tier == THINK
    assert place(n=0, steps=[], request="do a deep research, not this simple one").tier == THINK


def test_the_bots_own_word_and_waiting() -> None:
    assert place(n=2, steps=["2. click [4] → ok"], request="x", hint="hard").tier == THINK
    assert place(n=2, steps=["2. click [4] → ok"], request="x", hint="easy").tier == QUICK
    assert place(n=2, steps=["2. wait 20s → ok (now at x)"], request="x").tier == QUICK
    assert place(n=2, steps=["2. click [4] → ok"], request="x").tier is None


def test_the_spec_names_the_models_and_old_actors_keep_their_hash() -> None:
    profiles = bot_actor_spec("bot-x").model_profiles
    assert profiles.for_work_class(WorkClass.WORK, QUICK).model == FLASH
    think = profiles.for_work_class(WorkClass.WORK, THINK)
    assert think.model == PRO and think.thinking
    assert profiles.for_work_class(WorkClass.WORK).model == PRO
    # A tier the spec lacks (a run admitted under an older one) is the usual profile.
    plain = ModelProfiles(profiles={WorkClass.WORK: ModelProfile(provider="p", model="m")})
    assert plain.for_work_class(WorkClass.WORK, THINK).model == "m"
    # Tiers only reach WORK: a summary is never routed to a thinking model.
    assert profiles.for_work_class(WorkClass.SUMMARIZATION, THINK).model == FLASH
    assert "work_tiers" not in plain.model_dump(mode="json")
    assert canonical_hash(plain) == canonical_hash(plain.model_dump(mode="json"))


class _TierModel(ScriptedModel):
    def __init__(self, steps: list[dict[str, Any]]) -> None:
        super().__init__(steps)
        self.tiers: list[str | None] = []

    async def complete(self, ctx, req, *, work_class, call_site):  # type: ignore[no-untyped-def]
        self.tiers.append(req.tier)
        return await super().complete(ctx, req, work_class=work_class, call_site=call_site)


async def test_a_turn_moves_between_tiers_as_it_goes() -> None:
    bot = _Bot(id=uuid.uuid4())
    bots = FakeBots(bot)
    model = _TierModel(
        [
            {
                "thought": "Open",
                "action": "navigate",
                "url": "https://site.test",
                "next_step": "easy",
            },
            {"thought": "Wait for it", "action": "wait", "seconds": 1},
            {"thought": "Read", "action": "observe", "next_step": "hard"},
            {"thought": "Done", "action": "reply", "text": "Done."},
        ]
    )
    out = await _turn(_Node(_Ctx(), FakePageGateway(), model, _Org(bots)), bot.id)

    assert out["status"] == "replied"
    # usual → the bot said easy → waiting → the bot said hard
    assert model.tiers == [None, QUICK, QUICK, THINK]


def test_next_step_is_optional() -> None:
    assert BotStep(thought="t", action="observe").next_step is None


class _SeeingPage(FakePageGateway):
    def _page(self) -> dict[str, Any]:
        page = super()._page()
        page["elements"] = [{"id": i, "tag": "button", "label": f"B{i}"} for i in range(1, 6)]
        return page

    async def execute(self, ctx, call):  # type: ignore[no-untyped-def]
        result = await super().execute(ctx, call)
        if call.args.get("screenshot"):
            result.value["screenshot"] = "aGVsbG8="
        return result


class _SeeingModel(_TierModel):
    def __init__(self, steps: list[dict[str, Any]]) -> None:
        super().__init__(steps)
        self.glances = 0

    async def complete(self, ctx, req, *, work_class, call_site):  # type: ignore[no-untyped-def]
        if call_site == "bot_agent.glance":
            self.glances += 1
            assert req.images, "a glance sends the screenshot"
            return ModelResponse(
                text=json.dumps({"answer": "A menu is open: Research (off), Web search (on)."}),
                provider="fake",
                model="eyes",
                input_tokens=1,
                output_tokens=1,
                cost_cents=0,
            )
        return await super().complete(ctx, req, work_class=work_class, call_site=call_site)


async def test_a_stuck_step_is_decided_with_the_screen_described() -> None:
    bot = _Bot(id=uuid.uuid4())
    bots = FakeBots(bot)
    model = _SeeingModel(
        [
            {"thought": "Open", "action": "navigate", "url": "https://site.test"},
            {"thought": "Hmm", "action": "observe", "next_step": "hard"},
            {"thought": "Found it", "action": "reply", "text": "Research is in the menu."},
        ]
    )
    await _turn(_Node(_Ctx(), _SeeingPage(), model, _Org(bots)), bot.id)

    assert model.tiers == [None, None, THINK]
    assert model.glances == 1
    assert "Research (off), Web search (on)" in model.prompts[2]
    assert "Research (off)" not in model.prompts[1]


def test_an_unreadable_decision_is_retried_on_plainer_models() -> None:
    """The thinking model on a long page is the one that drifted out of JSON, every try:
    a decision that could not be read is made again without thinking, then quickly."""
    assert place(n=3, steps=["3. wait 5s → ok"], request="x", confused=1).tier is None
    assert place(n=3, steps=["3. wait 5s → ok"], request="x", confused=2).tier == QUICK
    assert place(n=3, steps=[], request="x", confused=1, hint="hard").tier is None
