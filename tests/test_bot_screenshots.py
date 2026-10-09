"""Screenshots in the chat: kept at the moments that matter, carried by id, never by
value, and bounded per bot.

The graph half runs on the in-memory fakes from `test_bots.py` and `test_vault.py`
with a page that hands back a screenshot when one is asked for. The storage half needs
Postgres (`runtime_test`, never the dev database).
"""

from __future__ import annotations

import base64
import json
import uuid
from typing import Any

from sqlalchemy import text

from runtime.org.bots import KEEP_SCREENSHOTS, BotService
from tests.test_bot_vision import SeeingModel, SeeingPage
from tests.test_bots import FakeBots, _Bot, _Ctx, _Node, _Org, _turn
from tests.test_vault import LoginSite, VaultBots

SHOT_BYTES = b"\xff\xd8\xff\xe0 a picture of a cart"
SHOT = base64.b64encode(SHOT_BYTES).decode()


class _Counting(SeeingPage):
    def __init__(self) -> None:
        super().__init__()
        self.pictures = 0

    async def execute(self, ctx, call):  # type: ignore[no-untyped-def]
        result = await super().execute(ctx, call)
        if call.tool == "browser.observe@1" and call.args.get("screenshot"):
            self.pictures += 1
            result.value["screenshot"] = SHOT
        return result


def _kept(bots: FakeBots) -> str:
    return json.dumps([(m.role, m.content, m.payload) for m in bots.log.values()])


async def test_a_reply_carries_a_picture_only_when_the_bot_asks_for_one() -> None:
    bot = _Bot(id=uuid.uuid4())
    bots = FakeBots(bot)
    page = _Counting()
    model = SeeingModel(
        [
            {
                "thought": "Show them",
                "action": "reply",
                "text": "Here is the cart.",
                "screenshot": True,
            }
        ],
        [],
    )
    await _turn(_Node(_Ctx(), page, model, _Org(bots)), bot.id)
    (reply,) = bots.said("bot")
    sid = uuid.UUID(reply.payload["screenshot_id"])
    assert bots.screenshots[sid] == ("reply", SHOT_BYTES)
    assert SHOT not in _kept(bots), "the message carries an id, never the picture"

    plain = FakeBots(_Bot(id=uuid.uuid4()))
    quiet = _Counting()
    model2 = SeeingModel([{"thought": "Chat", "action": "reply", "text": "Hi!"}], [])
    await _turn(_Node(_Ctx(), quiet, model2, _Org(plain)), plain.bot.id)
    assert quiet.pictures == 0 and "screenshot_id" not in plain.said("bot")[0].payload


async def test_an_approval_card_shows_the_page_it_is_about() -> None:
    bot = _Bot(id=uuid.uuid4())
    bots = FakeBots(bot)
    model = SeeingModel(
        [
            {"thought": "Open", "action": "navigate", "url": "https://shop.test"},
            {"thought": "Buy", "action": "click", "element": 1, "sensitive": True},
        ],
        [],
    )
    out = await _turn(_Node(_Ctx(), _Counting(), model, _Org(bots)), bot.id)
    assert out["status"] == "awaiting_approval"
    (card,) = bots.said("approval")
    assert bots.screenshots[uuid.UUID(card.payload["screenshot_id"])][0] == "approval"


async def test_a_look_keeps_the_picture_it_looked_at() -> None:
    bot = _Bot(id=uuid.uuid4())
    bots = FakeBots(bot)
    page = _Counting()
    model = SeeingModel(
        [
            {"thought": "Look", "action": "look", "text": "What colour is the banner?"},
            {"thought": "Done", "action": "reply", "text": "Red."},
        ],
        [{"answer": "Red.", "elements": [], "captcha": False}],
    )
    await _turn(_Node(_Ctx(), page, model, _Org(bots)), bot.id)
    assert page.pictures == 1, "the look's own screenshot is the one kept, not a second"
    (line,) = [m for m in bots.said("activity") if m.payload["action"]["type"] == "look"]
    assert bots.screenshots[uuid.UUID(line.payload["screenshot_id"])][0] == "look"


async def test_a_sign_in_card_shows_the_login_page() -> None:
    class PicturedLogin(LoginSite):
        async def execute(self, ctx, call):  # type: ignore[no-untyped-def]
            result = await super().execute(ctx, call)
            if call.tool == "browser.observe@1" and call.args.get("screenshot"):
                result.value["screenshot"] = SHOT
            return result

    bot = _Bot(id=uuid.uuid4())
    bots = VaultBots(bot)
    site = PicturedLogin()
    site.url = "https://site.test/login"
    model = SeeingModel([{"thought": "Sign in", "action": "sign_in"}], [])
    await _turn(_Node(_Ctx(), site, model, _Org(bots)), bot.id)
    (card,) = bots.said("credentials")
    assert bots.screenshots[uuid.UUID(card.payload["screenshot_id"])][0] == "credentials"


# --- Postgres -----------------------------------------------------------------------


async def test_screenshots_are_kept_per_bot_bounded_and_scoped(
    uow_factory: Any, organization_id: Any
) -> None:
    from runtime.runtime.bootstrap import Registrar

    await Registrar(uow_factory).ensure_organization(organization_id, "shots")
    a, b = uuid.uuid4(), uuid.uuid4()
    async with uow_factory.transaction() as uow:
        for bot_id in (a, b):
            await uow.bots.create(
                bot_id, organization_id, actor_name=f"bot-s-{bot_id.hex[:6]}", name="S"
            )
    service = BotService(uow_factory)
    run = uuid.uuid4()
    first = await service.save_screenshot(a, run_id=run, step=0, kind="reply", data=SHOT_BYTES)
    again = await service.save_screenshot(a, run_id=run, step=0, kind="reply", data=b"other")
    assert first == again, "a replayed step stores nothing twice"

    for step in range(1, KEEP_SCREENSHOTS + 5):
        await service.save_screenshot(a, run_id=run, step=step, kind="look", data=b"x")
    async with uow_factory() as uow:
        count = (
            await uow.session.execute(
                text("SELECT count(*) FROM bot_screenshots WHERE bot_id = :b"),
                {"b": a},
            )
        ).scalar_one()
        assert count == KEEP_SCREENSHOTS
        assert await uow.screenshots.get(a, first) is None, "the oldest aged out"
        latest = uuid.uuid5(uuid.NAMESPACE_URL, f"botshot:{run}:{KEEP_SCREENSHOTS + 4}:look")
        assert await uow.screenshots.get(a, latest) is not None
        assert await uow.screenshots.get(b, latest) is None, "another bot's id is a miss"
    async with uow_factory.transaction() as uow:
        await uow.screenshots.delete_for_bot(a)
        assert await uow.screenshots.get(a, latest) is None
