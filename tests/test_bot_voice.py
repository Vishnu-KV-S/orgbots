"""Voice: a spoken message gets a spoken-length answer, and a call leaves a card.

Speech itself is the browser's (recognition and synthesis), so what the runtime owns is
small: the flag on a spoken message and its run, the instruction it puts in the bot's
prompt, and the card a finished call leaves. Postgres is `runtime_features_test`.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest_asyncio

from runtime.api.app import create_app
from runtime.api.bots import ORG_HEADER
from runtime.graphs.registry import GRAPH_KEY, get_graph
from tests.test_bots import FakeBots, FakePageGateway, ScriptedModel, _Bot, _Ctx, _Node, _Org


async def _turn(model: ScriptedModel, **extra: Any) -> None:
    bot = _Bot(id=uuid.uuid4())
    graph = get_graph("bot_agent@1")().compile()
    await graph.ainvoke(
        {"input": {"bot_id": str(bot.id), **extra}},
        config={
            "recursion_limit": 30,
            "configurable": {GRAPH_KEY: _Node(_Ctx(), FakePageGateway(), model, _Org(FakeBots(bot)))},
        },
    )


async def test_a_spoken_turn_asks_for_a_spoken_answer() -> None:
    reply = {"thought": "Answer", "action": "reply", "text": "It's sunny, about twenty degrees."}
    spoken = ScriptedModel([reply])
    await _turn(spoken, voice=True)
    assert "talking to you by voice" in spoken.prompts[0]
    typed = ScriptedModel([reply])
    await _turn(typed)
    assert "by voice" not in typed.prompts[0]


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


async def test_a_call_marks_its_messages_and_leaves_a_card(
    api: httpx.AsyncClient, uow_factory: Any
) -> None:
    bot = (await api.post("/v1/bots", json={"name": "Talker"})).json()
    sent = (await api.post(f"/v1/bots/{bot['id']}/messages",
                           json={"text": "what's on today?", "voice": True})).json()
    card = await api.post(f"/v1/bots/{bot['id']}/voice-calls", json={"seconds": 135, "turns": 3})
    assert card.status_code == 201

    async with uow_factory() as uow:
        spec = await uow.runs.get_spec(uuid.UUID(sent["run_id"]))
        messages = await uow.bots.messages(uuid.UUID(bot["id"]), after_seq=0)
    assert spec is not None and spec.spec["input"]["voice"] is True
    said = next(m for m in messages if m.role == "user")
    assert said.payload == {"voice": True}
    call = messages[-1]
    assert call.role == "system" and call.content == "Voice chat · 2 min 15 s · 3 turns"
    assert call.payload["voice_call"] == {"seconds": 135, "turns": 3}
