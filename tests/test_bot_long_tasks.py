"""Long tasks: a bot's turn fits its ceilings, and carries on past one run.

A run is `MAX_STEPS` steps. Before this, two things went wrong with anything longer:
the tool-call ceiling (40) was below what 24 steps need (each step looks, then acts),
so a busy turn died with `CeilingExceeded` around step 20; and a turn that did reach
its step budget stopped and waited for the person to say "continue".

Now the ceilings are derived from `MAX_STEPS`, stale bots are republished to them, and
a turn at its step budget sends itself `bot.continue` — the dispatcher starts the next
chunk, which picks up the step log and working memory.

The graph half runs on the in-memory fakes from `test_bots.py`; the rest needs
Postgres (`runtime_test`, never the dev database).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import text

from runtime.domain.bots import BOT_TURN_PRIORITY, MAX_STEPS
from runtime.graphs.bot_agent.graph import _mark
from runtime.graphs.registry import GRAPH_KEY, get_graph
from runtime.org.bots import BotService, bot_actor_spec, refresh_bot_actor
from tests.test_bots import FakeBots, FakePageGateway, ScriptedModel, _Bot, _Ctx, _Node, _Org

# --- ceilings ---------------------------------------------------------------------------


def test_a_full_turn_fits_inside_the_ceilings() -> None:
    """Every pass observes and may act; one may also resume a parked action."""
    ceilings = bot_actor_spec("bot-x").ceilings
    assert ceilings.max_tool_calls >= 2 * MAX_STEPS + 1
    assert ceilings.max_llm_calls >= 2 * MAX_STEPS


# --- the graph --------------------------------------------------------------------------


class ChunkBots(FakeBots):
    def __init__(self, bot: _Bot, *, max_chunks: int) -> None:
        super().__init__(bot)
        self.max_chunks = max_chunks
        self.continued: list[dict[str, Any]] = []
        self.claimed: list[Any] = []

    async def continue_later(self, bot, *, run_id, step, turn, chunk, carried):  # type: ignore[no-untyped-def]
        if chunk >= self.max_chunks:
            return False
        self.continued.append({"turn": turn, "chunk": chunk + 1, "carried": carried})
        return True

    async def claim_run(self, bot_id: uuid.UUID, run_id: Any) -> None:
        self.claimed.append(run_id)


async def _invoke(node: _Node, state: dict[str, Any]) -> dict[str, Any]:
    graph = get_graph("bot_agent@1")().compile()
    result = await graph.ainvoke(
        state, config={"recursion_limit": 120, "configurable": {GRAPH_KEY: node}}
    )
    return dict(result.get("output", {}))


def _at_budget(bot: _Bot, **extra: Any) -> dict[str, Any]:
    """A turn at its last step: the state a run is in after `MAX_STEPS` passes."""
    return {
        "input": {"bot_id": str(bot.id), "turn": bot.turn, **extra},
        "n": MAX_STEPS,
        "log": [f"{i + 1}. clicked something" for i in range(12)],
        "plan": ["[x] open the site", "[>] collect prices"],
        "notes": "3 of 10 vendors done",
    }


async def test_a_turn_at_its_step_budget_carries_on_in_a_new_run() -> None:
    bot = _Bot(id=uuid.uuid4(), turn=3)
    bots = ChunkBots(bot, max_chunks=5)
    out = await _invoke(
        _Node(_Ctx(), FakePageGateway(), ScriptedModel([]), _Org(bots)), _at_budget(bot)
    )

    assert out["status"] == "continuing"
    (sent,) = bots.continued
    assert sent["turn"] == 3 and sent["chunk"] == 2
    assert sent["carried"]["notes"] == "3 of 10 vendors done"
    assert sent["carried"]["plan"] == ["[x] open the site", "[>] collect prices"]
    assert len(sent["carried"]["log"]) == 12
    # Not a pause: no "say continue", and the turn is not ended — it is still going.
    assert not bots.said("bot")
    assert bots.turns_ended == 0


async def test_the_last_chunk_stops_and_asks_for_continue() -> None:
    bot = _Bot(id=uuid.uuid4())
    bots = ChunkBots(bot, max_chunks=5)
    out = await _invoke(
        _Node(_Ctx(), FakePageGateway(), ScriptedModel([]), _Org(bots)), _at_budget(bot, chunk=5)
    )
    assert out["status"] == "step_budget"
    assert not bots.continued
    assert 'Say "continue"' in bots.said("bot")[-1].content


def _later_part(bot: _Bot, *, log: list[str], stalled: int, moved: bool) -> dict[str, Any]:
    """A turn at the end of part 3, whose plan and notes did or did not move."""
    state = _at_budget(bot, chunk=3)
    mark = _mark(state["plan"], state["notes"], {})
    state["input"]["carried"] = {"mark": "elsewhere" if moved else mark, "stalled": stalled}
    state["log"] = log
    return state


async def test_parts_that_get_nowhere_stop_the_task_and_say_why() -> None:
    """Not a count of parts: three in a row whose plan and notes did not move."""
    bot = _Bot(id=uuid.uuid4())
    bots = ChunkBots(bot, max_chunks=20)
    clicks = [f"{i + 1}. click 'Manual' → ok" for i in range(20)]
    state = _later_part(bot, log=clicks, stalled=2, moved=False)
    out = await _invoke(_Node(_Ctx(), FakePageGateway(), ScriptedModel([]), _Org(bots)), state)

    assert out["status"] == "blocked" and not bots.continued
    assert "the last 3 got nowhere" in bots.said("bot")[-1].content
    assert bots.turns_ended == 1


async def test_a_part_that_moved_on_or_waited_carries_on_with_the_count_reset() -> None:
    bot = _Bot(id=uuid.uuid4())
    clicks = [f"{i + 1}. click 'Next page' → ok" for i in range(20)]
    waits = [f"{i + 1}. wait 30s → ok (now at x)" for i in range(20)]
    for log, moved, waited in ((clicks, True, False), (waits, False, True)):
        bots = ChunkBots(bot, max_chunks=20)
        state = _later_part(bot, log=log, stalled=2, moved=moved)
        out = await _invoke(_Node(_Ctx(), FakePageGateway(), ScriptedModel([]), _Org(bots)), state)
        assert out["status"] == "continuing"
        (sent,) = bots.continued
        assert sent["carried"]["stalled"] == 0
        assert sent["carried"]["waited"] is waited


async def test_the_next_part_hears_it_made_no_progress_or_only_waited() -> None:
    bot = _Bot(id=uuid.uuid4(), turn=3)
    model = ScriptedModel([{"thought": "Done", "action": "reply", "text": "Done."}])
    carried = {"log": ["24. wait 30s → ok"], "plan": ["[>] x"], "stalled": 1, "waited": True}
    state = {"input": {"bot_id": str(bot.id), "turn": 3, "chunk": 4, "carried": carried}}
    await _invoke(
        _Node(_Ctx(), FakePageGateway(), model, _Org(ChunkBots(bot, max_chunks=20))), state
    )

    assert "the last 1 part(s) made no progress" in model.prompts[0]
    assert "check_back instead of waiting here" in model.prompts[0]


async def test_near_the_runaway_guard_the_bot_is_told_to_wrap_up() -> None:
    bot = _Bot(id=uuid.uuid4(), turn=3)
    model = ScriptedModel([{"thought": "Done", "action": "reply", "text": "Done."}])
    state = {
        "input": {"bot_id": str(bot.id), "turn": 3, "chunk": 20, "carried": {}},
        "n": MAX_STEPS - 2,
    }
    await _invoke(
        _Node(_Ctx(), FakePageGateway(), model, _Org(ChunkBots(bot, max_chunks=20))), state
    )
    assert "near its limit for this request" in model.prompts[0]


async def test_a_task_from_another_bot_does_not_carry_on_behind_its_back() -> None:
    """The asking bot waits for *this* run's output; a later chunk's would go nowhere."""
    bot = _Bot(id=uuid.uuid4())
    bots = ChunkBots(bot, max_chunks=5)
    state = _at_budget(bot, _delegation={"objective": "find 10 vendors"}, from_bot_name="Lead")
    out = await _invoke(_Node(_Ctx(), FakePageGateway(), ScriptedModel([]), _Org(bots)), state)
    assert out["status"] == "step_budget" and not bots.continued


async def test_the_next_chunk_picks_up_the_step_log_and_working_memory() -> None:
    bot = _Bot(id=uuid.uuid4(), turn=3)
    bots = ChunkBots(bot, max_chunks=5)
    model = ScriptedModel([{"thought": "Done", "action": "reply", "text": "All 10 found."}])
    ctx = _Ctx()
    carried = {
        "log": ["24. opened vendor 3 → ok"],
        "plan": ["[x] open the site", "[>] collect prices"],
        "notes": "3 of 10 vendors done",
        "answers": [],
        "tried": [],
    }
    state = {"input": {"bot_id": str(bot.id), "turn": 3, "chunk": 2, "carried": carried}}
    out = await _invoke(_Node(ctx, FakePageGateway(), model, _Org(bots)), state)

    assert out["status"] == "replied"
    prompt = model.prompts[0]
    assert "opened vendor 3" in prompt and "3 of 10 vendors done" in prompt
    assert "collect prices" in prompt
    assert "part 2 of a long task" in prompt
    assert bots.claimed == [ctx.run_id], "the chat must show this run as the working one"
    # The carried plan is the plan, not news: it is not announced again.
    assert not [
        m for m in bots.said("activity") if m.payload.get("action", {}).get("type") == "plan"
    ]


async def test_a_newer_message_still_stops_a_carried_on_task() -> None:
    bot = _Bot(id=uuid.uuid4(), turn=4)
    bots = ChunkBots(bot, max_chunks=5)
    model = ScriptedModel([])
    state = {"input": {"bot_id": str(bot.id), "turn": 3, "chunk": 2, "carried": {}}}
    out = await _invoke(_Node(_Ctx(), FakePageGateway(), model, _Org(bots)), state)
    assert out["status"] == "superseded" and model.prompts == []


# --- Postgres -----------------------------------------------------------------------


async def _bot(uow_factory: Any, organization_id: Any, settings: Any) -> Any:
    from runtime.runtime.bots import BotManager
    from runtime.runtime.run_service import RunService

    manager = BotManager(uow_factory, RunService(uow_factory, settings=settings))
    await manager.ensure_organization(organization_id, "long tasks")
    return manager, await manager.create(organization_id, name="Scout")


async def test_a_stale_bot_is_republished_to_the_new_ceilings(
    uow_factory: Any, organization_id: Any, settings: Any
) -> None:
    manager, bot = await _bot(uow_factory, organization_id, settings)
    # Make it look like a bot published before the ceilings moved.
    async with uow_factory.transaction() as uow:
        await uow.session.execute(
            text(
                "UPDATE actor_versions SET spec_hash = 'stale' WHERE actor_id = "
                "(SELECT id FROM actors WHERE organization_id = :org AND name = :name)"
            ),
            {"org": organization_id, "name": bot.actor_name},
        )
        assert await refresh_bot_actor(uow, organization_id, bot.actor_name) == 2
        assert await refresh_bot_actor(uow, organization_id, bot.actor_name) is None
        active = await uow.actors.resolve_active(organization_id, bot.actor_name)
    assert active.version == 2
    assert active.spec["ceilings"]["max_tool_calls"] == 3 * MAX_STEPS + 12

    # And the API's once-per-organization pass is a no-op on a current bot.
    await manager.ensure_organization(organization_id, "long tasks")
    async with uow_factory() as uow:
        assert (await uow.actors.resolve_active(organization_id, bot.actor_name)).version == 2


async def test_the_dispatcher_starts_the_next_chunk_at_bot_priority(
    uow_factory: Any, organization_id: Any, settings: Any
) -> None:
    from runtime.org.inbox import InboxService
    from runtime.runtime.dispatcher import Dispatcher
    from runtime.runtime.run_service import RunService

    _, bot = await _bot(uow_factory, organization_id, settings)
    service = BotService(uow_factory, inbox=InboxService(uow_factory), max_chunks=3)
    run_id = uuid.uuid4()
    carried = {"log": ["24. ok"], "plan": ["[>] go on"], "notes": "n", "answers": [], "tried": []}
    assert await service.continue_later(
        bot, run_id=run_id, step=MAX_STEPS, turn=7, chunk=1, carried=carried
    )
    # A replayed step sends nothing new.
    assert await service.continue_later(
        bot, run_id=run_id, step=MAX_STEPS, turn=7, chunk=1, carried=carried
    )
    assert not await service.continue_later(
        bot, run_id=uuid.uuid4(), step=MAX_STEPS, turn=7, chunk=3, carried=carried
    ), "the last chunk does not carry on"

    # `actors=None`, as `runtime.worker.main` passes: recipients come from the database.
    dispatcher = Dispatcher(
        uow_factory, RunService(uow_factory, settings=settings), settings=settings, actors=None
    )
    assert await dispatcher.drain() == 1
    async with uow_factory() as uow:
        row = (
            await uow.session.execute(
                text(
                    "SELECT r.priority, s.spec FROM runs r JOIN run_specs s ON s.run_id = r.id "
                    "JOIN actors a ON a.id = r.actor_id WHERE a.name = :name"
                ),
                {"name": bot.actor_name},
            )
        ).one()
        said = await uow.bots.messages(bot.id, after_seq=0)
    assert row.priority == BOT_TURN_PRIORITY
    run_input = row.spec["input"]
    assert run_input["bot_id"] == str(bot.id) and run_input["turn"] == 7
    assert run_input["chunk"] == 2 and run_input["carried"]["plan"] == ["[>] go on"]
    assert row.spec["spec"]["ceilings"]["max_tool_calls"] == 3 * MAX_STEPS + 12
    assert [m.content for m in said if m.role == "system"] == [
        "Still working — this is a long task, so I'm carrying on (part 2)."
    ]
