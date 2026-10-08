"""Group chats, bots messaging bots, and reactions.

Mentions and routing are pure; the service, the wake runner and the API use Postgres
(`runtime_features_test`, never the dev database); the graph's group and peer turns run
on the fakes from `test_bots.py`.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
import pytest_asyncio

from runtime.api.app import create_app
from runtime.api.bots import ORG_HEADER
from runtime.domain.groups import DAILY_BOT_MESSAGES, MAX_HOPS, Member, mentions, recipients
from runtime.graphs.registry import GRAPH_KEY, get_graph
from runtime.org.groups import GroupError, GroupService, Said
from runtime.runtime.bots import Sent
from runtime.runtime.wakes import WakeRunner
from tests.test_bots import FakeBots, FakePageGateway, ScriptedModel, _Bot, _Ctx, _Node, _Org

A, B, C = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
TEAM = [Member(A, "Ana"), Member(B, "Ana Lee"), Member(C, "Sales Scout")]

# --- mentions and routing ------------------------------------------------------------------


def test_mentions_match_whole_names_longest_first() -> None:
    assert mentions("@Ana Lee and @sales scout, please", TEAM) == ([B, C], False)
    assert mentions("@Ana — then @Ana Lee", TEAM) == ([A, B], False)
    assert mentions("mail ana@example.com", TEAM) == ([], False)
    assert mentions("thanks @everyone", TEAM) == ([], True)


def test_a_person_with_no_mention_reaches_the_lead_and_a_bot_reaches_nobody() -> None:
    assert recipients("what's next?", TEAM, lead=C, author=None) == [C]
    assert recipients("@everyone go", TEAM, lead=C, author=None) == [A, B, C]
    assert recipients("done.", TEAM, lead=C, author=A) == []
    assert recipients("@Ana Lee your turn, @Ana", TEAM, lead=C, author=A) == [B]


# --- Postgres: the service -------------------------------------------------------------------


async def _bots(uow_factory: Any, organization_id: Any, *names: str) -> list[Any]:
    from runtime.runtime.bootstrap import Registrar

    await Registrar(uow_factory).ensure_organization(organization_id, "groups")
    out = []
    async with uow_factory.transaction() as uow:
        for name in names:
            bot_id = uuid.uuid4()
            await uow.bots.create(
                bot_id, organization_id, actor_name=f"bot-g-{bot_id.hex[:6]}", name=name
            )
            out.append(await uow.bots.get(bot_id))
    return out


async def test_a_bots_answer_wakes_the_teammates_it_names_within_the_limits(
    uow_factory: Any, organization_id: Any
) -> None:
    lead, writer, extra = await _bots(uow_factory, organization_id, "Lead", "Writer", "Extra")
    service = GroupService(uow_factory)
    with pytest.raises(GroupError, match="2 to 6"):
        await service.create(organization_id, "Solo", [lead.id])
    group = await service.create(organization_id, "Launch", [lead.id, writer.id])
    assert group.lead_bot_id == lead.id

    handed = await service.post_reply(
        group.id, lead, "Found 5 leads. @Writer draft the emails; @Extra is not here.",
        run_id=uuid.uuid4(), step=3, hops=0,
    )
    assert [(d.recipient_name, d.queued) for d in handed] == [("Writer", True)]
    async with uow_factory() as uow:
        (wake,) = await uow.groups.queued_wakes()
        said = await uow.groups.recent(group.id, 10)
    assert (wake.bot_id, wake.kind, wake.hops, wake.from_bot_id) == (writer.id, "group", 1, lead.id)
    assert said[-1].author_name == "Lead" and said[-1].payload["hops"] == 0

    too_deep = await service.post_reply(
        group.id, writer, "@Lead over to you", run_id=uuid.uuid4(), step=0, hops=MAX_HOPS
    )
    assert [d.queued for d in too_deep] == [False]
    async with uow_factory() as uow:
        last = (await uow.groups.recent(group.id, 10))[-1]
    assert last.author_kind == "system" and "too many" in last.content


async def test_a_message_between_bots_is_answered_once_and_a_handoff_not_at_all(
    uow_factory: Any, organization_id: Any
) -> None:
    alice, bob = await _bots(uow_factory, organization_id, "Alice", "Bob")
    service = GroupService(uow_factory)
    with pytest.raises(GroupError, match="no other bot called 'Carol'"):
        await service.message_bot(alice, "Carol", "hi", run_id=uuid.uuid4(), step=0, hops=1)

    sent = await service.message_bot(
        alice, "bob", "What did the vendor quote?", run_id=uuid.uuid4(), step=2, hops=1
    )
    async with uow_factory() as uow:
        (wake,) = await uow.groups.queued_wakes()
        (inbox,) = await uow.bots.messages(bob.id, after_seq=0)
    assert sent.recipient_name == "Bob" and wake.expects_reply and wake.hops == 1
    assert inbox.payload["from_bot_name"] == "Alice" and inbox.payload["peer"] is True

    assert await service.relay_reply(wake, bob, "They quoted $40.", run_id=uuid.uuid4())
    async with uow_factory() as uow:
        back = [w for w in await uow.groups.queued_wakes() if w.bot_id == alice.id]
        (answer,) = await uow.bots.messages(alice.id, after_seq=0)
    assert answer.content == "They quoted $40." and answer.payload["from_bot_name"] == "Bob"
    assert [(w.expects_reply, w.hops) for w in back] == [(False, 2)]
    assert not await service.relay_reply(back[0], alice, "thanks", run_id=uuid.uuid4()), (
        "a reply to a reply goes nowhere"
    )

    handed = await service.message_bot(
        alice, "Bob", "You own the renewal now.", run_id=uuid.uuid4(), step=3, hops=1,
        handoff=True,
    )
    async with uow_factory() as uow:
        handoff = next(w for w in await uow.groups.queued_wakes() if w.handoff)
    assert handed.queued and not handoff.expects_reply


async def test_a_bot_runs_out_of_messages_for_the_day(
    uow_factory: Any, organization_id: Any
) -> None:
    alice, _ = await _bots(uow_factory, organization_id, "Alice", "Bob")
    async with uow_factory.transaction() as uow:
        for i in range(DAILY_BOT_MESSAGES):
            await uow.groups.add_wake(uuid.uuid4(), alice.id, kind="message", hops=1,
                                      from_bot_id=alice.id, status="skipped")
    with pytest.raises(GroupError, match="the most you may"):
        await GroupService(uow_factory).message_bot(
            alice, "Bob", "one more", run_id=uuid.uuid4(), step=0, hops=1
        )


@dataclass
class FakeManager:
    bots: dict[uuid.UUID, Any]
    busy_ids: set[uuid.UUID] = field(default_factory=set)
    started: list[Any] = field(default_factory=list)

    async def get(self, bot_id: uuid.UUID) -> Any:
        return self.bots[bot_id]

    async def busy(self, bot: Any) -> str | None:
        return "working" if bot.id in self.busy_ids else None

    async def start_wake(self, wake: Any) -> Sent:
        self.started.append(wake)
        return Sent(message_id=uuid.uuid4(), run_id=uuid.uuid4(), admitted=True,
                    refusal_reason=None)


async def test_the_runner_starts_one_delivery_per_free_bot_and_drops_stale_ones(
    uow_factory: Any, organization_id: Any
) -> None:
    alice, bob = await _bots(uow_factory, organization_id, "Alice", "Bob")
    async with uow_factory.transaction() as uow:
        for i in range(2):
            await uow.groups.add_wake(uuid.uuid4(), alice.id, kind="message", hops=1)
        await uow.groups.add_wake(uuid.uuid4(), bob.id, kind="message", hops=1)
    manager = FakeManager({alice.id: alice, bob.id: bob}, busy_ids={bob.id})
    tick = await WakeRunner(uow_factory, manager).tick()  # type: ignore[arg-type]
    assert tick.started == 1 and tick.waiting == ["Bob: working"]
    assert [w.bot_id for w in manager.started] == [alice.id]

    later = dt.datetime.now(dt.UTC) + dt.timedelta(hours=7)
    stale = await WakeRunner(uow_factory, manager).tick(later)  # type: ignore[arg-type]
    assert stale.skipped == 1, "Bob's delivery waited too long"


async def test_a_persons_group_message_starts_the_lead_with_the_group_on_its_input(
    uow_factory: Any, organization_id: Any, settings: Any
) -> None:
    from runtime.runtime.bootstrap import Registrar
    from runtime.runtime.bots import BotManager
    from runtime.runtime.run_service import RunService

    await Registrar(uow_factory).ensure_organization(organization_id, "groups")
    manager = BotManager(uow_factory, RunService(uow_factory, settings=settings))
    lead = await manager.create(organization_id, name="Lead")
    other = await manager.create(organization_id, name="Other")
    group = await GroupService(uow_factory).create(organization_id, "Ops", [lead.id, other.id])

    message_id, sent = await manager.post_group(group, "What's the plan?")
    (run,) = sent
    assert run.admitted
    async with uow_factory() as uow:
        spec = await uow.runs.get_spec(run.run_id)
        note = (await uow.bots.messages(lead.id, after_seq=0))[-1]
        quiet = await uow.bots.messages(other.id, after_seq=0)
    assert spec is not None
    given = spec.spec["input"]
    assert given["group_id"] == str(group.id) and given["group_message_id"] == str(message_id)
    assert "Working on your message in the group “Ops”" in note.content
    assert quiet == [], "the other member was not named"


# --- the graph -----------------------------------------------------------------------------


@dataclass
class _Wake:
    id: uuid.UUID
    kind: str
    from_bot_id: uuid.UUID | None
    expects_reply: bool
    handoff: bool = False
    hops: int = 1
    message_id: uuid.UUID | None = None


@dataclass
class FakeGroups:
    said: list[Said] = field(default_factory=list)
    posted: list[tuple[str, int]] = field(default_factory=list)
    relayed: list[str] = field(default_factory=list)
    messaged: list[tuple[str, str, bool, int]] = field(default_factory=list)
    wakes: dict[uuid.UUID, _Wake] = field(default_factory=dict)
    name: str = "Launch"

    async def peers(self, bot: Any) -> list[Any]:
        return [_Bot(id=uuid.uuid4(), name="Writer", label="Copy")]

    async def wake(self, wake_id: uuid.UUID) -> _Wake | None:
        return self.wakes.get(wake_id)

    async def conversation(self, group_id: Any, *, thread_root: Any = None) -> list[Said]:
        return list(self.said)

    async def get(self, group_id: Any) -> Any:
        @dataclass
        class G:
            name: str
            lead_bot_id: Any

        return G(self.name, None)

    async def roster(self, group_id: Any) -> list[tuple[Any, str, str]]:
        return [(uuid.uuid4(), "Writer", "Copy")]

    async def post_reply(self, group_id, bot, text, *, run_id, step, hops, thread_root=None):  # type: ignore[no-untyped-def]
        from runtime.org.groups import Delivered

        self.posted.append((text, hops))
        return [Delivered(uuid.uuid4(), "Writer", True)] if "@Writer" in text else []

    async def relay_reply(self, wake, bot, text, *, run_id):  # type: ignore[no-untyped-def]
        self.relayed.append(text)
        return True

    async def message_bot(self, sender, name, text, *, run_id, step, hops, handoff=False):  # type: ignore[no-untyped-def]
        from runtime.org.groups import Delivered

        if name.lower() == "nobody":
            raise GroupError("there is no other bot called 'nobody'")
        self.messaged.append((name, text, handoff, hops))
        return Delivered(uuid.uuid4(), name.title(), True)


async def _invoke(node: _Node, **extra: Any) -> dict[str, Any]:
    graph = get_graph("bot_agent@1")().compile()
    result = await graph.ainvoke(
        {"input": {"bot_id": str(node.org.bots.bot.id), **extra}},
        config={"recursion_limit": 60, "configurable": {GRAPH_KEY: node}},
    )
    return dict(result.get("output", {}))


async def test_a_group_turn_reads_the_group_and_answers_there() -> None:
    bot = _Bot(id=uuid.uuid4(), name="Lead")
    groups = FakeGroups(said=[
        Said("user", "Plan the launch email.", {"author_name": "you"}),
        Said("bot", "I can draft it.", {"author_bot_id": str(uuid.uuid4()), "author_name": "Writer"}),
    ])
    bots = FakeBots(bot)
    reply = {"thought": "Split it", "action": "reply",
             "text": "Here's the plan. @Writer please draft the email."}
    model = ScriptedModel([reply])
    await _invoke(_Node(_Ctx(), FakePageGateway(), model, _Org(bots, groups=groups)),
                  group_id=str(uuid.uuid4()), hops=0)
    assert "Writer (teammate bot): I can draft it." in model.prompts[0]
    assert "group chat “Launch”" in model.prompts[0]
    assert groups.posted == [("Here's the plan. @Writer please draft the email.", 0)]
    (said,) = bots.said("bot")
    assert said.payload["handed_to"] == ["Writer"] and said.payload["group_id"]


async def test_a_bots_message_is_answered_back_and_a_handoff_is_owned() -> None:
    bot = _Bot(id=uuid.uuid4(), name="Bob")
    sender = _Bot(id=uuid.uuid4(), name="Alice")
    asked, handed = uuid.uuid4(), uuid.uuid4()
    groups = FakeGroups(wakes={
        asked: _Wake(asked, "message", sender.id, expects_reply=True),
        handed: _Wake(handed, "message", sender.id, expects_reply=False, handoff=True),
    })
    bots = FakeBots(bot)
    bots.helpers_.append(sender)  # so the fake can name the sender
    reply = {"thought": "Answer", "action": "reply", "text": "$40."}
    model = ScriptedModel([reply])
    await _invoke(_Node(_Ctx(), FakePageGateway(), model, _Org(bots, groups=groups)),
                  wake_id=str(asked), hops=1)
    assert "from Alice, another of your person's bots" in model.prompts[0]
    assert groups.relayed == ["$40."]

    model = ScriptedModel([reply])
    await _invoke(_Node(_Ctx(), FakePageGateway(), model, _Org(bots, groups=groups)),
                  wake_id=str(handed), hops=1)
    assert "handed you the task" in model.prompts[0] and groups.relayed == ["$40."]


async def test_message_bot_sends_without_waiting_and_names_who_exists() -> None:
    bot = _Bot(id=uuid.uuid4(), name="Alice")
    groups = FakeGroups()
    send = {"thought": "Ask Writer", "action": "message_bot", "bot": "writer",
            "text": "Draft the renewal email for Acme.", "handoff": True}
    wrong = {"thought": "x", "action": "message_bot", "bot": "nobody", "text": "hi"}
    done = {"thought": "Done", "action": "reply", "text": "Writer has it."}
    model = ScriptedModel([send, wrong, done])
    await _invoke(_Node(_Ctx(), FakePageGateway(), model, _Org(FakeBots(bot), groups=groups)),
                  hops=0)
    assert groups.messaged == [("writer", "Draft the renewal email for Acme.", True, 1)]
    assert "handed the task to Writer; it owns it now" in model.prompts[1]
    assert "no other bot called 'nobody'" in model.prompts[2]
    assert "Your person's other bots" in model.system[0] and "Writer (Copy)" in model.system[0]


# --- the API -------------------------------------------------------------------------------


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


async def test_the_person_runs_a_group_and_reacts(api: httpx.AsyncClient) -> None:
    lead = (await api.post("/v1/bots", json={"name": "Lead"})).json()
    writer = (await api.post("/v1/bots", json={"name": "Writer"})).json()
    made = await api.post("/v1/groups", json={"name": "Launch", "members": [lead["id"], writer["id"]],
                                              "lead": writer["id"]})
    assert made.status_code == 201, made.text
    group = made.json()
    assert group["lead_bot_id"] == writer["id"] and len(group["members"]) == 2

    to_lead = (await api.post(f"/v1/groups/{group['id']}/messages", json={"text": "Status?"})).json()
    assert len(to_lead["runs"]) == 1 and to_lead["runs"][0]["admitted"]
    both = (await api.post(f"/v1/groups/{group['id']}/messages",
                           json={"text": "@everyone kick-off"})).json()
    assert len(both["runs"]) == 2
    thread = (await api.post(f"/v1/groups/{group['id']}/messages",
                             json={"text": "@Lead more on this", "thread_root": to_lead["message_id"]})).json()
    assert len(thread["runs"]) == 1

    listed = (await api.get(f"/v1/groups/{group['id']}/messages")).json()
    assert [m["content"] for m in listed["messages"]] == ["Status?", "@everyone kick-off",
                                                          "@Lead more on this"]
    assert listed["messages"][2]["thread_root"] == to_lead["message_id"]
    assert set(listed["working"]) == {lead["id"], writer["id"]}

    reacted = await api.post(
        f"/v1/groups/{group['id']}/messages/{to_lead['message_id']}/reactions", json={"emoji": "👍"}
    )
    assert reacted.json()["reactions"] == ["👍"]
    assert (await api.post(
        f"/v1/groups/{group['id']}/messages/{to_lead['message_id']}/reactions",
        json={"emoji": "🦄"})).status_code == 422
    again = (await api.get(f"/v1/groups/{group['id']}/messages")).json()["messages"][0]
    assert again["reactions"] == ["👍"]

    sent = (await api.post(f"/v1/bots/{lead['id']}/messages", json={"text": "hi"})).json()
    on = await api.post(f"/v1/bots/{lead['id']}/messages/{sent['message_id']}/reactions",
                        json={"emoji": "❤️"})
    assert on.json()["reactions"] == ["❤️"]
    page = (await api.get(f"/v1/bots/{lead['id']}/messages")).json()
    hearted = next(m for m in page["messages"] if m["id"] == sent["message_id"])
    assert hearted["reactions"] == ["❤️"]

    assert [g["name"] for g in (await api.get("/v1/groups")).json()["groups"]] == ["Launch"]
    assert (await api.delete(f"/v1/groups/{group['id']}")).status_code == 200
    assert (await api.get("/v1/groups")).json()["groups"] == []
