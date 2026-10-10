"""A request is held to how it was asked, and a message sent mid-task joins the task.

The failure these pin: "on claude do a deepresearch for the market of AI learning apps"
got a Google search, because the bot kept the topic in mind and not *on Claude*; and
"i mean on claude", sent while it searched, started a fresh run that knew nothing of
what it had been doing, saw a Google results page, and carried on with it.

The graph half runs on the in-memory fakes from `test_bots.py`; the lock between a
message and the turn's end needs Postgres.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text

from runtime.domain.bots import ASK_CHARS, BotStep, render_ask, steerable
from runtime.graphs.bot_agent.tiers import THINK, place
from runtime.graphs.registry import GRAPH_KEY, get_graph
from runtime.org.bots import BotService
from tests.test_bots import FakeBots, FakePageGateway, ScriptedModel, _Bot, _Ctx, _Node, _Org

ASKED = "on claude do a deepresearch for the market of ai learning apps and its scope"
CORRECTION = "i mean on claude and why you not using claude?"


class _Model(ScriptedModel):
    """Scripted steps, a record of each step's tier, and something the person does
    while a given step is being decided (`during[i]`, after that step's prompt)."""

    def __init__(self, steps: list[dict[str, Any]], during: dict[int, Any] | None = None):
        super().__init__(steps)
        self.tiers: list[str | None] = []
        self.during = during or {}

    async def complete(self, ctx, req, *, work_class, call_site):  # type: ignore[no-untyped-def]
        self.tiers.append(req.tier)
        out = await super().complete(ctx, req, work_class=work_class, call_site=call_site)
        hook = self.during.get(len(self.prompts) - 1)
        if hook is not None:
            hook()
        return out


async def _invoke(node: _Node, state: dict[str, Any]) -> dict[str, Any]:
    graph = get_graph("bot_agent@1")().compile()
    result = await graph.ainvoke(
        state, config={"recursion_limit": 120, "configurable": {GRAPH_KEY: node}}
    )
    return dict(result.get("output", {}))


def _person_turn(bot: _Bot, **extra: Any) -> dict[str, Any]:
    return {"input": {"bot_id": str(bot.id), "turn": bot.turn, **extra}}


GOOGLE = {
    "thought": "Search for market reports",
    "action": "navigate",
    "url": "https://www.google.com/search?q=AI+learning+apps+market",
    "request": {"goal": "Market research on AI learning apps and their scope"},
}


# --- the request, written down ----------------------------------------------------------


def test_the_request_names_where_and_is_cut_not_refused() -> None:
    step = BotStep.model_validate(
        {
            "thought": "t",
            "action": "observe",
            "request": {
                "goal": "x" * (ASK_CHARS + 50),
                "where": "on Claude, with its Research mode",
                "must": ["market size", "scope", *(["y"] * 10)],
            },
        }
    )
    assert step.request is not None
    assert len(step.request.goal) == ASK_CHARS and len(step.request.must) == 6
    assert render_ask(step.request.model_dump())[1] == (
        "Where / with what: on Claude, with its Research mode"
    )
    assert BotStep(thought="t", action="observe").request is None


async def test_the_request_rides_every_step_and_is_shown_once() -> None:
    bot = _Bot(id=uuid.uuid4(), turn=1)
    bots = FakeBots(bot)
    bots.person_says(ASKED)
    claude = {
        "goal": "Deep research on the AI learning apps market and its scope",
        "where": "on Claude, with deep research",
    }
    model = _Model(
        [
            {
                "thought": "Open Claude",
                "action": "navigate",
                "url": "https://claude.ai",
                "request": claude,
            },
            {"thought": "Look", "action": "observe", "request": claude},
            {"thought": "Done", "action": "reply", "text": "Here it is."},
        ]
    )
    out = await _invoke(_Node(_Ctx(), FakePageGateway(), model, _Org(bots)), _person_turn(bot))

    assert out["status"] == "replied"
    assert "Where / with what: on Claude, with deep research" not in model.prompts[0]
    assert all("Where / with what: on Claude, with deep research" in p for p in model.prompts[1:])
    # Shown to the person when written, not again when unchanged.
    shown = [m for m in bots.said("activity") if m.payload["action"]["type"] == "request"]
    assert len(shown) == 1 and shown[0].payload["request"] == {**claude, "must": []}
    # The rule is general, and says the brief does not decide how.
    assert "Read the whole request, not only its topic" in model.system[0]
    assert "wins over the way your brief would do it" in model.system[0]


# --- steering -----------------------------------------------------------------------------


def test_which_turns_a_message_can_steer() -> None:
    assert steerable({"bot_id": "b", "turn": 3})
    assert steerable({"bot_id": "b", "turn": 3, "member_id": "m", "voice": True})
    assert steerable({"bot_id": "b", "turn": 3, "follow_up": True, "carried": {}})
    assert steerable({"bot_id": "b", "turn": 3, "chunk": 2, "carried": {}, "mode": "continue"})
    assert not steerable({"bot_id": "b"}), "no turn: a task from another bot"
    assert not steerable({"bot_id": "b", "turn": 3, "routine_id": "r"})
    assert not steerable({"bot_id": "b", "turn": 3, "group_id": "g"})
    assert not steerable({"bot_id": "b", "turn": 3, "check_task": "look again"})
    assert not steerable({"bot_id": "b", "turn": 3, "_delegation": {}})


def test_a_correction_mid_task_or_before_it_is_thought_about() -> None:
    assert place(n=4, steps=["4. click [3] → ok"], request=ASKED, steered=True).tier == THINK
    assert place(n=0, steps=[], request=CORRECTION).tier == THINK
    assert place(n=0, steps=[], request="i meant the 2025 numbers").tier == THINK


async def test_a_message_sent_mid_task_is_read_as_new_with_the_work_so_far() -> None:
    bot = _Bot(id=uuid.uuid4(), turn=1)
    bots = FakeBots(bot)
    bots.person_says(ASKED)
    model = _Model(
        [
            GOOGLE,
            {
                "thought": "They want it done on Claude: open it",
                "action": "navigate",
                "url": "https://claude.ai",
                "request": {
                    "goal": "Market research on AI learning apps and their scope",
                    "where": "on Claude, with deep research",
                },
            },
            {"thought": "Done", "action": "reply", "text": "Started on Claude."},
        ],
        # The person writes while the first step is being decided.
        during={0: lambda: bots.person_says(CORRECTION)},
    )
    node = _Node(_Ctx(), FakePageGateway(), model, _Org(bots))
    out = await _invoke(node, _person_turn(bot))

    assert out["status"] == "replied"
    assert "NEW" not in model.prompts[0]
    second = model.prompts[1]
    assert f"<<<NEW\n{CORRECTION}\nNEW>>>" in second
    # With what the bot had been doing and what it had written down.
    assert "open https://www.google.com/search" in second
    assert "Goal: Market research on AI learning apps" in second
    # Decided on the thinking tier; read once, not again on the step after.
    assert model.tiers[1] == THINK
    assert "<<<NEW" not in model.prompts[2]
    assert [m.content for m in bots.said("system")] == [
        "Got your message — taking it into account."
    ]
    # The turn closed, and nothing was left for a follow-up.
    assert bots.steer_turn is None and bots.follow_ups == []


async def test_a_turn_is_open_for_steering_while_it_works() -> None:
    bot = _Bot(id=uuid.uuid4(), turn=2)
    bots = FakeBots(bot)
    bots.person_says(ASKED)
    opened: list[int | None] = []
    model = _Model(
        [GOOGLE, {"thought": "Done", "action": "reply", "text": "Done."}],
        during={0: lambda: opened.append(bots.steer_turn)},
    )
    await _invoke(_Node(_Ctx(), FakePageGateway(), model, _Org(bots)), _person_turn(bot))
    assert opened == [2] and bots.steer_turn is None


async def test_a_message_that_lands_as_the_turn_finishes_gets_a_follow_up() -> None:
    bot = _Bot(id=uuid.uuid4(), turn=1)
    bots = FakeBots(bot)
    bots.person_says(ASKED)
    model = _Model(
        [{**GOOGLE, "action": "reply", "text": "Here is what Google says."}],
        # Sent after the last step read the conversation.
        during={0: lambda: bots.person_says(CORRECTION)},
    )
    out = await _invoke(_Node(_Ctx(), FakePageGateway(), model, _Org(bots)), _person_turn(bot))

    assert out["status"] == "replied"
    (follow,) = bots.follow_ups
    assert follow["turn"] == 2 == bot.turn and bots.steer_turn == 2
    carried = follow["carried"]
    assert carried["ask"]["goal"].startswith("Market research")
    assert "You wrote as I was finishing — picking that up now." in [
        m.content for m in bots.said("system")
    ]

    # The follow-up reads the message as new, with the last turn's request in hand.
    model = _Model([{"thought": "On Claude", "action": "reply", "text": "Opening Claude."}])
    out = await _invoke(
        _Node(_Ctx(), FakePageGateway(), model, _Org(bots)),
        _person_turn(bot, follow_up=True, carried=carried),
    )
    assert out["status"] == "replied"
    assert f"<<<NEW\n{CORRECTION}\nNEW>>>" in model.prompts[0]
    assert "Goal: Market research" in model.prompts[0]
    assert model.tiers == [THINK]
    assert len(bots.follow_ups) == 1


async def test_stop_ends_a_turn_without_a_follow_up() -> None:
    bot = _Bot(id=uuid.uuid4(), turn=1, stop_requested=True)
    bots = FakeBots(bot)
    bots.person_says(ASKED)
    bots.person_says(CORRECTION)
    out = await _invoke(_Node(_Ctx(), FakePageGateway(), _Model([]), _Org(bots)), _person_turn(bot))
    assert out["status"] == "stopped" and bots.follow_ups == []


async def test_a_turn_nobody_can_steer_is_left_alone() -> None:
    """A task from another bot (no turn) neither opens steering nor settles."""
    bot = _Bot(id=uuid.uuid4(), turn=1)
    bots = FakeBots(bot)
    bots.person_says(ASKED)
    model = _Model(
        [{"thought": "Done", "action": "reply", "text": "Done."}],
        during={0: lambda: bots.person_says(CORRECTION)},
    )
    await _invoke(
        _Node(_Ctx(), FakePageGateway(), model, _Org(bots)), {"input": {"bot_id": str(bot.id)}}
    )
    assert bots.steer_turn is None and bots.follow_ups == [] and bot.turn == 1


# --- Postgres: the message and the turn's end, under one lock ------------------------------


async def _manager(uow_factory: Any, organization_id: Any, settings: Any) -> Any:
    from runtime.runtime.bots import BotManager
    from runtime.runtime.run_service import RunService

    manager = BotManager(uow_factory, RunService(uow_factory, settings=settings))
    await manager.ensure_organization(organization_id, "steering")
    return manager, await manager.create(organization_id, name="Researcher")


async def _row(uow_factory: Any, bot_id: uuid.UUID) -> Any:
    async with uow_factory() as uow:
        return await uow.bots.get(bot_id)


async def test_a_message_steers_the_working_turn_until_it_closes(
    uow_factory: Any, organization_id: Any, settings: Any
) -> None:
    from runtime.org.inbox import InboxService
    from runtime.runtime.dispatcher import Dispatcher
    from runtime.runtime.run_service import RunService

    manager, bot = await _manager(uow_factory, organization_id, settings)
    first = await manager.send(bot.id, ASKED)
    assert first.admitted and not first.steered
    row = await _row(uow_factory, bot.id)
    assert row.steer_turn == row.turn, "a person's turn is open from the start"
    turn = row.turn

    # Sent while it works: no new run, no new turn.
    second = await manager.send(bot.id, CORRECTION)
    assert second.steered and second.run_id == first.run_id
    assert (await _row(uow_factory, bot.id)).turn == turn

    async with uow_factory() as uow:
        said = [m for m in await uow.bots.messages(bot.id) if m.role == "user"]
    assert [m.content for m in said] == [ASKED, CORRECTION]

    # The turn read both: it closes, and the next message starts a turn of its own.
    service = BotService(uow_factory, inbox=InboxService(uow_factory), max_chunks=3)
    assert (
        await service.settle_turn(
            row, run_id=first.run_id, turn=turn, seen=said[-1].seq, carried={}
        )
        == []
    )
    assert (await _row(uow_factory, bot.id)).steer_turn is None
    third = await manager.send(bot.id, "and compare Duolingo with Khanmigo")
    assert not third.steered and third.run_id != first.run_id
    row = await _row(uow_factory, bot.id)
    assert row.turn == turn + 1 and row.steer_turn == turn + 1

    # A message the turn never read: settling starts a follow-up that carries the work.
    fourth = await manager.send(bot.id, "only 2025 sources")
    assert fourth.steered
    unseen = await service.settle_turn(
        row,
        run_id=third.run_id,
        turn=row.turn,
        seen=said[-1].seq + 1,
        carried={"plan": ["[>] compare"], "ask": {"goal": "compare"}},
    )
    assert [m.content for m in unseen] == ["only 2025 sources"]
    after = await _row(uow_factory, bot.id)
    assert after.turn == row.turn + 1 and after.steer_turn == after.turn

    dispatcher = Dispatcher(
        uow_factory, RunService(uow_factory, settings=settings), settings=settings, actors=None
    )
    assert await dispatcher.drain() == 1
    async with uow_factory() as uow:
        spec = (
            await uow.session.execute(
                text(
                    "SELECT s.spec FROM runs r JOIN run_specs s ON s.run_id = r.id "
                    "JOIN actors a ON a.id = r.actor_id WHERE a.name = :name "
                    "ORDER BY r.created_at DESC LIMIT 1"
                ),
                {"name": bot.actor_name},
            )
        ).scalar_one()
    run_input = spec["input"]
    assert run_input["follow_up"] and run_input["turn"] == after.turn
    assert run_input["carried"]["plan"] == ["[>] compare"]
    assert run_input["carried"]["seen"] == said[-1].seq + 1
    assert steerable(run_input)


async def test_stop_and_a_superseded_turn_are_never_steered(
    uow_factory: Any, organization_id: Any, settings: Any
) -> None:
    manager, bot = await _manager(uow_factory, organization_id, settings)
    first = await manager.send(bot.id, ASKED)
    await manager.stop(bot.id)
    after_stop = await manager.send(bot.id, CORRECTION)
    assert not after_stop.steered and after_stop.run_id != first.run_id

    # The old turn's end, after a newer one began, changes nothing.
    row = await _row(uow_factory, bot.id)
    service = BotService(uow_factory)
    assert (
        await service.settle_turn(row, run_id=first.run_id, turn=row.turn - 1, seen=0, carried={})
        == []
    )
    assert (await _row(uow_factory, bot.id)).steer_turn == row.turn
