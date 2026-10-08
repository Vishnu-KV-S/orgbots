"""Bots' vision: `look`, the image path through the model gateway, and the masked
screenshot.

No database and no real model: the provider is driven with a recording client, and
the graph runs on the in-memory fakes from `test_bots.py`. One test drives a real
headless Chrome through `Computer.bot_screenshot`, to check that a field the vault
filled is masked in what a vision model would see; it skips without a browser.
"""

from __future__ import annotations

import base64
import json
import os
import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest

from runtime.domain.enums import WorkClass
from runtime.domain.errors import ModelCallNotAllowed
from runtime.gateway.models import (
    IMAGE_TOKENS,
    ImageInput,
    ModelRequest,
    ModelResponse,
    _estimate_cents,
)
from runtime.gateway.providers.anthropic_provider import (
    AnthropicProvider,
    ProviderCapabilities,
)
from runtime.org.bots import bot_actor_spec
from tests.test_bots import FakeBots, FakePageGateway, _Bot, _Ctx, _Node, _Org, _turn

SHOT = base64.b64encode(b"\xff\xd8\xff\xe0 not really a jpeg").decode()

# --- the provider -------------------------------------------------------------------


@dataclass
class _Usage:
    input_tokens: int = 1400
    output_tokens: int = 60
    cache_read_input_tokens: int = 0


@dataclass
class _Text:
    text: str
    type: str = "text"


@dataclass
class _Message:
    content: list[Any]
    stop_reason: str = "end_turn"
    usage: _Usage = field(default_factory=_Usage)
    stop_details: Any = None


class _Endpoint:
    def __init__(self, sent: list[tuple[str, dict[str, Any]]], name: str) -> None:
        self._sent = sent
        self._name = name

    async def create(self, **params: Any) -> _Message:
        self._sent.append((self._name, params))
        return _Message([_Text('{"answer": "a bar chart", "elements": [], "captcha": false}')])


class _Client:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.messages = _Endpoint(self.sent, "messages")
        self.beta = type("Beta", (), {"messages": _Endpoint(self.sent, "beta")})()


VISION = bot_actor_spec("bot-x").model_profiles.for_work_class(WorkClass.PERCEPTION)
DEEPSEEK = ProviderCapabilities(
    schema_format=False,
    prompt_caching=False,
    vision=True,
    vision_models=frozenset({"deepseek-flash"}),
)
REQUEST = ModelRequest(
    prompt="What does the chart show?",
    images=(ImageInput(media_type="image/jpeg", data=SHOT),),
    metadata={"work_class": "perception"},
)


async def test_an_image_goes_before_the_question_as_a_base64_block() -> None:
    client = _Client()
    provider = AnthropicProvider(name="deepseek", client=client, capabilities=DEEPSEEK)
    await provider.complete(VISION, REQUEST)

    ((endpoint, params),) = client.sent
    assert endpoint == "messages" and params["model"] == "deepseek-flash"
    (turn,) = params["messages"]
    image, text = turn["content"]
    assert image == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg", "data": SHOT},
    }
    assert text == {"type": "text", "text": "What does the chart show?"}
    assert params["output_config"]["effort"] == "low"


async def test_an_image_for_a_model_that_cannot_read_one_is_refused_not_dropped() -> None:
    provider = AnthropicProvider(name="deepseek", client=_Client(), capabilities=DEEPSEEK)
    # The bot's decisions run on deepseek-v4-pro, which takes no images.
    pro = VISION.model_copy(update={"model": "deepseek-v4-pro"})
    with pytest.raises(ModelCallNotAllowed, match="cannot read images"):
        await provider.complete(pro, REQUEST)
    text_only = AnthropicProvider(
        name="other", client=_Client(), capabilities=ProviderCapabilities(vision=False)
    )
    with pytest.raises(ModelCallNotAllowed, match="cannot read images"):
        await text_only.complete(VISION, REQUEST)


def test_an_image_is_reserved_for_and_never_shown_in_a_repr() -> None:
    plain = ModelRequest(prompt=REQUEST.prompt)
    # A cent a token, so the image's share does not round away.
    pricey = VISION.model_copy(update={"input_cents_per_mtok": 1_000_000})
    gap = _estimate_cents(pricey, REQUEST) - _estimate_cents(pricey, plain)
    assert gap == IMAGE_TOKENS
    assert SHOT not in repr(REQUEST)
    with pytest.raises(ValueError, match="over 5 MB"):
        ImageInput(media_type="image/png", data="A" * (7 * 1024 * 1024))


def test_vision_stays_with_deepseek_on_the_model_that_reads_images() -> None:
    from runtime.gateway.providers import VISION_MODELS, build_providers
    from runtime.org.bots import VISION_MODEL
    from runtime.settings import Settings

    providers = build_providers(Settings(deepseek_api_key="sk-x"))
    assert "anthropic" not in providers
    assert providers["deepseek"].capabilities.vision_models == VISION_MODELS  # type: ignore[attr-defined]
    # Two copies of one fact (org may not import the gateway); they must agree.
    assert VISION_MODEL in VISION_MODELS


def test_every_bot_has_a_vision_profile() -> None:
    assert (VISION.provider, VISION.model) == ("deepseek", "deepseek-flash")


# --- the graph ----------------------------------------------------------------------


class SeeingPage(FakePageGateway):
    """The fake site, plus a screenshot when one is asked for."""

    async def execute(self, ctx, call):  # type: ignore[no-untyped-def]
        result = await super().execute(ctx, call)
        if call.tool == "browser.observe@1" and call.args.get("screenshot"):
            result.value["screenshot"] = SHOT
        return result


class SeeingModel:
    """Scripted steps for WORK, scripted answers for PERCEPTION."""

    def __init__(self, steps: list[dict[str, Any]], looks: list[Any]) -> None:
        self._steps = list(steps)
        self._looks = list(looks)
        self.prompts: list[str] = []
        self.looked: list[ModelRequest] = []

    async def complete(self, ctx, req, *, work_class, call_site):  # type: ignore[no-untyped-def]
        if work_class is WorkClass.PERCEPTION:
            self.looked.append(req)
            answer = self._looks.pop(0)
            if isinstance(answer, Exception):
                raise answer
            text = json.dumps(answer)
        else:
            self.prompts.append(req.prompt)
            text = json.dumps(self._steps.pop(0))
        return ModelResponse(
            text=text,
            provider="fake",
            model="scripted",
            input_tokens=1,
            output_tokens=1,
            cost_cents=0,
        )


async def test_a_look_answers_the_bot_and_the_image_goes_nowhere_else() -> None:
    bot = _Bot(id=uuid.uuid4())
    bots = FakeBots(bot)
    model = SeeingModel(
        [
            {
                "thought": "The chart is an image",
                "action": "look",
                "text": "What does the chart show for March?",
            },
            {"thought": "Report it", "action": "reply", "text": "March was 42."},
        ],
        [{"answer": "March is the tallest bar, labelled 42.", "elements": [1], "captcha": False}],
    )
    out = await _turn(_Node(_Ctx(), SeeingPage(), model, _Org(bots)), bot.id)

    assert out["status"] == "replied"
    (looked,) = model.looked
    assert [img.media_type for img in looked.images] == ["image/jpeg"]
    assert "What does the chart show for March?" in looked.prompt
    assert "URL:" in looked.prompt, "the vision model gets the listing, to name [numbers]"
    # The answer reaches the bot's next decision…
    assert "March is the tallest bar, labelled 42." in model.prompts[1]
    assert "[1]" in model.prompts[1]
    # …and the screenshot reaches nothing the run keeps or shows.
    kept = json.dumps([(m.role, m.content, m.payload) for m in bots.log.values()])
    assert SHOT not in kept and SHOT not in "".join(model.prompts)
    (line,) = [m for m in bots.said("activity") if m.payload["action"]["type"] == "look"]
    assert line.payload["note"] == "March is the tallest bar, labelled 42."


async def test_a_captcha_is_handed_to_the_person_and_the_turn_ends() -> None:
    bot = _Bot(id=uuid.uuid4())
    bots = FakeBots(bot)
    model = SeeingModel(
        [{"thought": "Something is in the way", "action": "look", "text": "What is on screen?"}],
        [{"answer": "A reCAPTCHA checkbox.", "elements": [], "captcha": True}],
    )
    out = await _turn(_Node(_Ctx(), SeeingPage(), model, _Org(bots)), bot.id)

    assert out["status"] == "human_needed"
    assert len(model.prompts) == 1, "no further step after a CAPTCHA"
    assert "Take control of my screen" in bots.said("system")[-1].content
    assert bots.turns_ended == 1


async def test_without_vision_the_bot_is_told_and_carries_on() -> None:
    bot = _Bot(id=uuid.uuid4())
    bots = FakeBots(bot)
    model = SeeingModel(
        [
            {"thought": "Look", "action": "look", "text": "Is the banner red?"},
            {"thought": "Fine", "action": "reply", "text": "I can't see colours here."},
        ],
        [ModelCallNotAllowed("provider 'anthropic' is not configured")],
    )
    out = await _turn(_Node(_Ctx(), SeeingPage(), model, _Org(bots)), bot.id)
    assert out["status"] == "replied"
    assert "look is not available" in model.prompts[1]
    assert "not configured" in model.prompts[1]


def test_look_needs_a_question() -> None:
    from runtime.domain.bots import BotStep

    with pytest.raises(ValueError, match="needs `text`"):
        BotStep(thought="t", action="look")


# --- the computer's masked screenshot -------------------------------------------------


def _chromium() -> bool:
    """A browser Playwright can actually launch: one named in `COMPUTER_CHROMIUM_PATH`,
    or a downloaded build with its executable present (a half-finished download leaves
    the folder without one)."""
    if os.environ.get("COMPUTER_CHROMIUM_PATH"):
        return True
    cache = os.path.expanduser("~/.cache/ms-playwright")
    if not os.path.isdir(cache):
        return False
    return any(
        os.path.isfile(os.path.join(cache, d, sub, exe))
        for d in os.listdir(cache)
        if d.startswith("chromium")
        for sub in ("chrome-linux", "chrome-linux64")
        for exe in ("chrome", "headless_shell")
    )


PIXEL_AT = """
async ([b64, x, y]) => {
  const img = new Image();
  img.src = 'data:image/jpeg;base64,' + b64;
  await img.decode();
  const c = document.createElement('canvas');
  c.width = img.width; c.height = img.height;
  const g = c.getContext('2d');
  g.drawImage(img, 0, 0);
  return Array.from(g.getImageData(x, y, 1, 1).data).slice(0, 3);
}
"""


@pytest.mark.skipif(not _chromium(), reason="no Chrome for Playwright")
async def test_the_bot_screenshot_masks_what_the_vault_filled(tmp_path: Any) -> None:
    import http.server
    import threading

    from runtime.computer.browser import Computer

    site = tmp_path / "site"
    site.mkdir()
    (site / "index.html").write_text(
        "<title>Login</title><body style='background:#fff'>"
        "<input id=e type=email name=email style='width:400px;height:60px;font-size:40px;"
        "background:#fff;border:0'>"
    )
    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(  # noqa: E731
        *a, directory=str(site), **k
    )
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    computer = Computer(tmp_path / "profile", headless=True)
    try:
        screen = await computer.screen("v")
        await computer.act(
            screen, {"type": "navigate", "url": f"http://127.0.0.1:{server.server_address[1]}/"}
        )
        snap = await computer.observe(screen)
        (email,) = [e["id"] for e in snap["elements"] if e.get("name") == "email"]
        await computer.fill(
            screen,
            expect_host="127.0.0.1",
            fields=[{"elements": [email], "value": "ada@example.com", "password": False}],
            submit=False,
        )
        box = await screen.page.locator("#e").bounding_box()
        assert box is not None
        x, y = int(box["x"] + 20), int(box["y"] + box["height"] / 2)

        masked = base64.b64encode(await computer.bot_screenshot(screen)).decode()
        person = base64.b64encode(await computer.screenshot(screen)).decode()
        bot_view = await screen.page.evaluate(PIXEL_AT, [masked, x, y])
        person_view = await screen.page.evaluate(PIXEL_AT, [person, 2, 2])
        # The bot's image is grey where the email is; the person's is the white page.
        assert all(100 <= channel <= 155 for channel in bot_view), bot_view
        assert all(channel >= 230 for channel in person_view), person_view
    finally:
        await computer.stop()
        server.shutdown()
