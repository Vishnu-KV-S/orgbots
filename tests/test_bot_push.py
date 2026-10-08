"""Push notifications: the outbox a bot writes, the notifier, and real Web Push.

A local HTTP server stands in for a browser's push service, and the test plays the
browser: it makes the subscription's keys and decrypts what arrives, so the encryption
and the VAPID signature are checked for real, not mocked. Postgres is
`runtime_features_test`, never the dev database.
"""

from __future__ import annotations

import base64
import datetime as dt
import http.server
import json
import os
import threading
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from sqlalchemy import text

from runtime.api.app import create_app
from runtime.api.bots import ORG_HEADER
from runtime.graphs.registry import GRAPH_KEY, get_graph
from tests.test_bots import FakeBots, FakePageGateway, ScriptedModel, _Bot, _Ctx, _Node, _Org


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


@pytest.fixture
def keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUNTIME_CREDENTIAL_KEYS", "k1:" + base64.b64encode(os.urandom(32)).decode())


@dataclass
class PushService:
    """A push service that keeps what it receives and answers with `status`."""

    url: str
    received: list[tuple[dict[str, str], bytes]] = field(default_factory=list)
    status: int = 201


@pytest.fixture
def push_service() -> Iterator[PushService]:
    service = PushService(url="")

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("content-length", 0)))
            service.received.append(({k.lower(): v for k, v in self.headers.items()}, body))
            self.send_response(service.status)
            self.end_headers()

        def log_message(self, *args: Any) -> None:
            return None

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    service.url = f"http://127.0.0.1:{server.server_address[1]}/push/abc"
    try:
        yield service
    finally:
        server.shutdown()


def _browser() -> tuple[ec.EllipticCurvePrivateKey, str, bytes, str]:
    """A browser's subscription keys: its private key, p256dh, auth secret, auth."""
    private = ec.generate_private_key(ec.SECP256R1())
    public = private.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    auth = os.urandom(16)
    return private, _b64(public), auth, _b64(auth)


async def _org(uow_factory: Any, organization_id: Any) -> None:
    from runtime.runtime.bootstrap import Registrar

    await Registrar(uow_factory).ensure_organization(organization_id, "push")


async def test_a_push_arrives_encrypted_to_the_browser_and_signed_by_the_server(
    uow_factory: Any, organization_id: Any, settings: Any, keys: None, push_service: PushService
) -> None:
    import http_ece

    from runtime.gateway.push import PushSender
    from runtime.runtime.notifier import Notifier

    await _org(uow_factory, organization_id)
    private, p256dh, auth_secret, auth = _browser()
    async with uow_factory.transaction() as uow:
        await uow.push.subscribe(organization_id, push_service.url, p256dh, auth, "test")
        await uow.push.notify(
            uuid.uuid4(),
            organization_id,
            bot_id=uuid.uuid4(),
            kind="approval",
            title="Scout needs your approval",
            body="Place the order for $40?",
            url="/?bot=x",
        )
    sender = PushSender(uow_factory, settings)
    public = await sender.public_key()
    assert public == await PushSender(uow_factory, settings).public_key(), "made once"
    async with uow_factory() as uow:
        stored = await uow.push.key()
    assert stored is not None and b"-----" not in stored.ciphertext

    tick = await Notifier(uow_factory, sender).tick()
    assert (tick.sent, tick.pushed) == (1, 1)
    ((headers, body),) = push_service.received
    assert headers["content-encoding"] == "aes128gcm"
    assert headers["authorization"].startswith("vapid t=") and public in headers["authorization"]
    plain = http_ece.decrypt(
        body, private_key=private, auth_secret=auth_secret, version="aes128gcm"
    )
    payload = json.loads(plain)
    assert payload["title"] == "Scout needs your approval" and payload["url"] == "/?bot=x"
    assert (await Notifier(uow_factory, sender).tick()).sent == 0, "sent once"


async def test_a_device_the_push_service_has_forgotten_is_unsubscribed(
    uow_factory: Any, organization_id: Any, settings: Any, keys: None, push_service: PushService
) -> None:
    from runtime.gateway.push import PushSender
    from runtime.runtime.notifier import Notifier

    await _org(uow_factory, organization_id)
    _, p256dh, _, auth = _browser()
    push_service.status = 410
    async with uow_factory.transaction() as uow:
        await uow.push.subscribe(organization_id, push_service.url, p256dh, auth, "old phone")
        await uow.push.notify(
            uuid.uuid4(),
            organization_id,
            bot_id=None,
            kind="reply",
            title="Scout",
            body="Done.",
            url="/",
        )
    tick = await Notifier(uow_factory, PushSender(uow_factory, settings)).tick()
    assert tick.gone == 1
    async with uow_factory() as uow:
        assert await uow.push.subscriptions(organization_id) == []


async def test_an_old_notification_is_stamped_not_sent(
    uow_factory: Any, organization_id: Any, settings: Any, keys: None, push_service: PushService
) -> None:
    from runtime.gateway.push import PushSender
    from runtime.runtime.notifier import Notifier

    await _org(uow_factory, organization_id)
    _, p256dh, _, auth = _browser()
    async with uow_factory.transaction() as uow:
        await uow.push.subscribe(organization_id, push_service.url, p256dh, auth, "x")
        await uow.push.notify(
            uuid.uuid4(),
            organization_id,
            bot_id=None,
            kind="reply",
            title="Scout",
            body="Yesterday's news",
            url="/",
        )
    later = dt.datetime.now(dt.UTC) + dt.timedelta(days=1)
    tick = await Notifier(uow_factory, PushSender(uow_factory, settings)).tick(later)
    assert tick.sent == 1 and tick.pushed == 0 and push_service.received == []


# --- what writes a notification -----------------------------------------------------------


async def test_cards_write_notifications_in_the_same_breath(
    uow_factory: Any, organization_id: Any
) -> None:
    from runtime.domain.vault import CredentialField
    from runtime.org.bots import BotService

    await _org(uow_factory, organization_id)
    bot_id = uuid.uuid4()
    async with uow_factory.transaction() as uow:
        await uow.bots.create(bot_id, organization_id, actor_name="bot-p-1", name="Scout")
        bot = await uow.bots.get(bot_id)
    service = BotService(uow_factory)
    run = uuid.uuid4()
    await service.park(
        bot_id,
        run_id=run,
        step=2,
        action={"type": "click"},
        display={},
        reason="spends money",
        thought="Order the mug",
    )
    await service.request_credentials(
        bot,
        run_id=run,
        step=3,
        host="shop.test",
        page_url="https://shop.test/login",
        purpose="sign_in",
        fields=[CredentialField("f0", "password", "Password", (1,))],
        ask={"f0"},
        thought="Need the password",
        saved=[],
        retry=False,
        reason="",
    )
    await service.notify(bot, "reply", "Ordered.", run_id=run, step=4)
    await service.notify(bot, "reply", "Ordered.", run_id=run, step=4)
    async with uow_factory() as uow:
        sent = await uow.push.unsent()
    assert [(n.kind, n.title) for n in sent] == [
        ("approval", "Scout needs your approval"),
        ("sign_in", "Scout needs you to sign in"),
        ("reply", "Scout"),
    ], "one each, even when a step is replayed"
    assert sent[1].body == "shop.test — sign in" and sent[0].url == f"/?bot={bot_id}"


async def test_a_reply_notifies_but_a_helpers_answer_to_its_parent_does_not() -> None:
    reply = {"thought": "Done", "action": "reply", "text": "All done."}
    bots = FakeBots(_Bot(id=uuid.uuid4()))
    graph = get_graph("bot_agent@1")().compile()
    node = _Node(_Ctx(), FakePageGateway(), ScriptedModel([reply]), _Org(bots))
    await graph.ainvoke(
        {"input": {"bot_id": str(bots.bot.id)}},
        config={"recursion_limit": 20, "configurable": {GRAPH_KEY: node}},
    )
    assert bots.notified == [("reply", "All done.", "")]

    helper = FakeBots(_Bot(id=uuid.uuid4()))
    node = _Node(_Ctx(), FakePageGateway(), ScriptedModel([reply]), _Org(helper))
    await graph.ainvoke(
        {
            "input": {
                "bot_id": str(helper.bot.id),
                "_delegation": {"objective": "x"},
                "from_bot_name": "Lead",
            }
        },
        config={"recursion_limit": 20, "configurable": {GRAPH_KEY: node}},
    )
    assert helper.notified == []


# --- the API ---------------------------------------------------------------------------------


@pytest_asyncio.fixture
async def api(
    settings: Any, uow_factory: Any, organization_id: Any, keys: None
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


async def test_a_device_subscribes_and_can_ask_for_a_test(
    api: httpx.AsyncClient, uow_factory: Any, organization_id: Any
) -> None:
    key = (await api.get("/v1/push")).json()["public_key"]
    assert len(base64.urlsafe_b64decode(key + "==")) == 65, "an uncompressed P-256 point"
    _, p256dh, _, auth = _browser()
    endpoint = "https://push.example.test/abc"
    made = await api.post(
        "/v1/push/subscribe",
        json={"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}, "device": "Pixel 9"},
    )
    assert made.status_code == 201
    insecure = await api.post(
        "/v1/push/subscribe",
        json={"endpoint": "http://x.test/a", "keys": {"p256dh": p256dh, "auth": auth}},
    )
    assert insecure.status_code == 422
    assert (await api.post("/v1/push/test")).status_code == 202
    async with uow_factory() as uow:
        assert [s.endpoint for s in await uow.push.subscriptions(organization_id)] == [endpoint]
        assert [n.kind for n in await uow.push.unsent()] == ["test"]
    async with uow_factory() as uow:
        device = await uow.session.scalar(
            text("SELECT user_agent FROM push_subscriptions WHERE endpoint = :e"), {"e": endpoint}
        )
    assert device == "Pixel 9", "the browser's own, not the UI proxy's"

    other = await api.post(
        "/v1/push/unsubscribe",
        json={"endpoint": endpoint},
        headers={ORG_HEADER: str(uuid.uuid4())},
    )
    assert other.status_code in (200, 404)
    async with uow_factory() as uow:
        assert len(await uow.push.subscriptions(organization_id)) == 1, "not another org's to drop"
    await api.post("/v1/push/unsubscribe", json={"endpoint": endpoint})
    async with uow_factory() as uow:
        assert await uow.push.subscriptions(organization_id) == []
