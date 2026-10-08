"""A bot's working memory: the plan and notes it keeps while it works on a task.

Each pass of `bot_agent@1` builds its prompt fresh, so what the model wrote down on an
earlier step is the only way a fact read three pages ago reaches the step that needs
it. These pin that the plan and notes are carried pass to pass, survive a step that
omits them, are shown to the person when the plan changes, and survive an approval —
which ends the run, so the approved action is a new run with empty state.

Also here: a page too big for the gateway's inline limit is still read in full, since
the page is what every step's decision is made from.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from runtime.domain.bots import MAX_PLAN_ITEMS, NOTES_CHARS, BotStep
from tests.test_bots import (
    FakeBots,
    FakePageGateway,
    ScriptedModel,
    _Bot,
    _Ctx,
    _Node,
    _Org,
    _Result,
    _turn,
)

PLAN = ["[>] Open the store", "[ ] Find the price", "[ ] Report it"]


async def test_plan_and_notes_carry_from_step_to_step() -> None:
    bot = _Bot(id=uuid.uuid4())
    bots = FakeBots(bot)
    model = ScriptedModel(
        [
            {
                "thought": "Start",
                "action": "navigate",
                "url": "https://site.test",
                "plan": PLAN,
            },
            {"thought": "Read it", "action": "observe", "notes": "Price: $12 at site.test/p/1"},
            {"thought": "Look again", "action": "observe"},
            {"thought": "Done", "action": "reply", "text": "It costs $12."},
        ]
    )
    out = await _turn(_Node(_Ctx(), FakePageGateway(), model, _Org(bots)), bot.id)

    assert out["status"] == "replied"
    assert "Your plan:" not in model.prompts[0]
    assert "[ ] Find the price" in model.prompts[1]
    assert "Price: $12" in model.prompts[2]
    # A step that writes neither keeps both.
    assert "[ ] Find the price" in model.prompts[3]
    assert "Price: $12" in model.prompts[3]


async def test_the_plan_is_shown_to_the_person_once_per_change() -> None:
    bot = _Bot(id=uuid.uuid4())
    bots = FakeBots(bot)
    revised = ["[x] Open the store", "[>] Find the price", "[ ] Report it"]
    model = ScriptedModel(
        [
            {"thought": "Start", "action": "observe", "plan": PLAN},
            {"thought": "Same plan", "action": "observe", "plan": PLAN},
            {"thought": "Progress", "action": "observe", "plan": revised},
            {"thought": "Done", "action": "reply", "text": "Done."},
        ]
    )
    await _turn(_Node(_Ctx(), FakePageGateway(), model, _Org(bots)), bot.id)

    shown = [m for m in bots.said("activity") if m.payload.get("action", {}).get("type") == "plan"]
    assert [m.payload["plan"] for m in shown] == [PLAN, revised]
    assert shown[0].content == "\n".join(PLAN)


async def test_working_memory_survives_an_approval() -> None:
    bot = _Bot(id=uuid.uuid4())
    bots = FakeBots(bot)
    page = FakePageGateway()
    model = ScriptedModel(
        [
            {
                "thought": "Open the site",
                "action": "navigate",
                "url": "https://site.test",
                "plan": ["[>] Confirm the order", "[ ] Download the invoice"],
                "notes": "Invoice is #4471",
            },
            {"thought": "Confirm it", "action": "click", "element": 1, "sensitive": True},
        ]
    )
    out = await _turn(_Node(_Ctx(), page, model, _Org(bots)), bot.id)
    assert out["status"] == "awaiting_approval"

    (pending,) = bots.pendings.values()
    pending.status = "allowed"
    model2 = ScriptedModel([{"thought": "Done", "action": "reply", "text": "Confirmed."}])
    await _turn(_Node(_Ctx(), page, model2, _Org(bots)), bot.id, resume_pending_id=str(pending.id))

    (prompt,) = model2.prompts
    assert "[ ] Download the invoice" in prompt
    assert "Invoice is #4471" in prompt
    clicked = [c for c in page.calls if c["tool"] == "browser.act@1"][-1]["action"]
    assert clicked == {"type": "click", "element": 1}, "working memory must not reach the computer"


class _Externalised(FakePageGateway):
    """Every result comes back the way the gateway returns one over its inline limit:
    a reference, with the real value in the artifact store."""

    def __init__(self) -> None:
        super().__init__()
        self.store: dict[str, bytes] = {}

    async def execute(self, ctx, call):  # type: ignore[no-untyped-def]
        real = (await super().execute(ctx, call)).value
        artifact_id = str(uuid.uuid4())
        self.store[artifact_id] = json.dumps(real).encode()
        ref = {"artifact_id": artifact_id, "sha256": "x", "size_bytes": 1}
        return _Result({"artifact": ref, "truncated": True})

    async def get(self, artifact_id: Any) -> bytes:
        return self.store[str(artifact_id)]


async def test_a_page_over_the_inline_limit_is_read_in_full() -> None:
    """A busy site's element list is tens of kilobytes. Read as the bare reference,
    the bot saw a blank page and took every action on it for a failure."""
    bot = _Bot(id=uuid.uuid4())
    bots = FakeBots(bot)
    page = _Externalised()
    model = ScriptedModel(
        [
            {"thought": "Open it", "action": "navigate", "url": "https://big.test"},
            {"thought": "Done", "action": "reply", "text": "Opened."},
        ]
    )
    await _turn(_Node(_Ctx(), page, model, _Org(bots), artifacts=page), bot.id)

    assert "URL: https://big.test" in model.prompts[1]
    (opened,) = [m for m in bots.said("activity") if m.payload.get("action", {}).get("url")]
    assert opened.payload["ok"] is True
    assert "→ ok" in model.prompts[1]


def test_working_memory_is_bounded() -> None:
    with pytest.raises(ValidationError):
        BotStep(thought="t", action="observe", plan=["[ ] x"] * (MAX_PLAN_ITEMS + 1))
    with pytest.raises(ValidationError):
        BotStep(thought="t", action="observe", notes="x" * (NOTES_CHARS + 1))
