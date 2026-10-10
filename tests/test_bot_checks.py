"""Check-ins: a bot that waits on something wakes itself later to look again.

The graph half runs on the fakes from `test_bots.py`; the queue — held until due, one
per bot, dropped by Stop — uses Postgres (`runtime_test`, never the dev database).
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest

from runtime.domain.bots import MAX_CHECKS, BotStep
from runtime.graphs.registry import GRAPH_KEY, get_graph
from runtime.org.groups import GroupService
from runtime.runtime.wakes import WakeRunner
from tests.test_bot_groups import FakeManager, _bots
from tests.test_bots import FakeBots, FakePageGateway, ScriptedModel, _Bot, _Ctx, _Node, _Org

TASK = (
    "Open https://www.instagram.com/direct/t/178/ and see if Vishnu KV answered "
    "'Hey Vishnu! How's it going?'. If he did, reply casually."
)


def test_check_back_needs_a_delay_and_a_task() -> None:
    with pytest.raises(ValueError, match="in_minutes"):
        BotStep(thought="t", action="check_back", task="look")
    with pytest.raises(ValueError, match="task"):
        BotStep(thought="t", action="check_back", in_minutes=5)
    with pytest.raises(ValueError):
        BotStep(thought="t", action="check_back", in_minutes=0, task="look")
    step = BotStep(thought="t", action="check_back", in_minutes=5, task="look")
    assert step.ends_turn and not step.text


# --- the graph, against fakes ------------------------------------------------------------


@dataclass
class _Check:
    id: uuid.UUID
    task: str
    due_at: dt.datetime
    hops: int


@dataclass
class FakeChecks:
    """The check-in half of `GroupService`, in memory."""

    queued: _Check | None = None
    set_: list[tuple[int, str, int]] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)

    async def peers(self, bot: Any) -> list[Any]:
        return []

    async def wake(self, wake_id: Any) -> None:
        return None

    async def pending_check(self, bot_id: Any) -> _Check | None:
        return self.queued

    async def check_back(self, bot, *, run_id, step, minutes, task, count):  # type: ignore[no-untyped-def]
        self.set_.append((minutes, task, count))
        self.queued = _Check(
            uuid.uuid4(), task, dt.datetime.now(dt.UTC) + dt.timedelta(minutes=minutes), count
        )
        return self.queued

    async def cancel_checks(self, bot_id: Any, reason: str) -> int:
        self.cancelled.append(reason)
        dropped, self.queued = self.queued, None
        return int(dropped is not None)


async def _invoke(node: _Node, **extra: Any) -> dict[str, Any]:
    graph = get_graph("bot_agent@1")().compile()
    result = await graph.ainvoke(
        {"input": {"bot_id": str(node.org.bots.bot.id), **extra}},
        config={"recursion_limit": 60, "configurable": {GRAPH_KEY: node}},
    )
    return dict(result.get("output", {}))


def _node(model: ScriptedModel, checks: FakeChecks, bots: FakeBots | None = None) -> _Node:
    bots = bots or FakeBots(_Bot(id=uuid.uuid4()))
    return _Node(_Ctx(), FakePageGateway(), model, _Org(bots, groups=checks))


async def test_watching_for_a_reply_tells_the_person_and_sets_a_check_in() -> None:
    checks = FakeChecks()
    model = ScriptedModel(
        [
            {
                "thought": "Sent; he hasn't answered. Watch the thread.",
                "action": "check_back",
                "in_minutes": 3,
                "task": TASK,
                "text": "Sent! I'll keep an eye on the thread and answer when he replies.",
            }
        ]
    )
    node = _node(model, checks)
    out = await _invoke(node, turn=0)

    assert out["status"] == "replied" and out["kind"] == "check_back"
    assert checks.set_ == [(3, TASK, 1)], "a person's turn starts a watch at check-in 1"
    (said,) = node.org.bots.said("bot")
    assert said.content.startswith("Sent!")
    assert node.org.bots.notified[0][0] == "reply"
    assert "check_back" in model.system[0] and "reply when he" in model.system[0]


async def test_a_check_in_that_finds_nothing_ends_quietly_and_checks_again() -> None:
    checks = FakeChecks()
    model = ScriptedModel(
        [{"thought": "No reply yet.", "action": "check_back", "in_minutes": 6, "task": TASK}]
    )
    node = _node(model, checks)
    out = await _invoke(
        node,
        turn=0,
        wake_id=str(uuid.uuid4()),
        check_task=TASK,
        check_count=2,
        check_set_at=(dt.datetime.now(dt.UTC) - dt.timedelta(minutes=3)).isoformat(),
    )

    assert out == {"status": "checking_back", "steps": 0, "in_minutes": 6, "count": 3}
    assert checks.set_ == [(6, TASK, 3)]
    assert node.org.bots.said("bot") == [] and node.org.bots.notified == []
    assert node.org.bots.quiet_ends == 1, "no unread dot for a look that found nothing"
    prompt = model.prompts[0]
    assert "check-in 2 of at most" in prompt and "set 3 minutes ago" in prompt
    assert TASK in prompt


async def test_a_watch_that_never_sees_anything_ends_and_says_so() -> None:
    checks = FakeChecks()
    model = ScriptedModel(
        [
            {"thought": "Still nothing.", "action": "check_back", "in_minutes": 30, "task": TASK},
            {"thought": "Stop watching.", "action": "reply", "text": "He hasn't answered yet."},
        ]
    )
    node = _node(model, checks)
    out = await _invoke(
        node, turn=0, wake_id=str(uuid.uuid4()), check_task=TASK, check_count=MAX_CHECKS
    )

    assert checks.set_ == [] and out["status"] == "replied"
    assert f"checked {MAX_CHECKS} times in a row" in model.prompts[1]


async def test_the_person_turn_sees_the_check_in_to_come_and_can_cancel_it() -> None:
    checks = FakeChecks(
        queued=_Check(uuid.uuid4(), TASK, dt.datetime.now(dt.UTC) + dt.timedelta(minutes=10), 1)
    )
    model = ScriptedModel(
        [
            {"thought": "They said stop.", "action": "cancel_check"},
            {"thought": "Done", "action": "reply", "text": "OK, I've stopped watching."},
        ]
    )
    await _invoke(_node(model, checks), turn=0)

    assert "You have a check-in set for" in model.prompts[0] and TASK in model.prompts[0]
    assert checks.cancelled == ["the bot cancelled it"]
    assert "cancelled your check-in" in model.prompts[1]
    assert "You have a check-in set" not in model.prompts[1]


async def test_a_helper_answering_its_parent_cannot_wander_off_to_watch() -> None:
    checks = FakeChecks()
    model = ScriptedModel(
        [
            {"thought": "Wait for it", "action": "check_back", "in_minutes": 5, "task": "look"},
            {"thought": "Answer", "action": "reply", "text": "Not there yet."},
        ]
    )
    await _invoke(
        _node(model, checks),
        _delegation={"objective": "Has the parcel shipped?"},
        from_bot_name="Lead",
    )
    assert checks.set_ == [] and "a task from another bot" in model.prompts[1]


# --- Postgres: the queue -------------------------------------------------------------------


async def test_a_check_in_waits_until_due_and_a_newer_one_replaces_it(
    uow_factory: Any, organization_id: Any
) -> None:
    (scout,) = await _bots(uow_factory, organization_id, "Scout")
    service = GroupService(uow_factory)
    first = await service.check_back(
        scout, run_id=uuid.uuid4(), step=4, minutes=5, task="look", count=1
    )
    second = await service.check_back(
        scout, run_id=uuid.uuid4(), step=2, minutes=10, task="look again", count=1
    )
    pending = await service.pending_check(scout.id)
    assert pending is not None and pending.id == second.id and pending.task == "look again"
    async with uow_factory() as uow:
        assert (await uow.groups.get_wake(first.id)).status == "skipped"  # type: ignore[union-attr]
        assert await uow.groups.queued_wakes() == [], "not due for ten minutes"

    async with uow_factory.transaction() as uow:
        await uow.groups.add_wake(
            uuid.uuid4(),
            scout.id,
            kind="check",
            hops=2,
            due_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1),
            task="due now",
        )
        await uow.groups.cancel_checks(scout.id, "test", keep=None)
        await uow.groups.add_wake(
            uuid.uuid4(),
            scout.id,
            kind="check",
            hops=2,
            due_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1),
            task="due now",
        )
    manager = FakeManager({scout.id: scout})
    tick = await WakeRunner(uow_factory, manager).tick()  # type: ignore[arg-type]
    assert tick.started == 1 and manager.started[0].task == "due now"


async def test_stop_drops_the_check_in(
    uow_factory: Any, organization_id: Any, settings: Any
) -> None:
    from runtime.runtime.bots import BotManager
    from runtime.runtime.run_service import RunService

    (scout,) = await _bots(uow_factory, organization_id, "Scout")
    service = GroupService(uow_factory)
    await service.check_back(scout, run_id=uuid.uuid4(), step=0, minutes=5, task="x", count=1)
    await BotManager(uow_factory, RunService(uow_factory, settings=settings)).stop(scout.id)
    assert await service.pending_check(scout.id) is None
