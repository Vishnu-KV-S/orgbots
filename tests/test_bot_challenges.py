"""Human-verification checks: the bots' Chrome does not announce itself as automated,
and a check that stands in front of a page goes to the bot's person.

Pinned from Cloudflare's "Performing security verification" page, which every site
behind it showed the bots: their Chrome reported `navigator.webdriver === true` (being
driven over `--remote-debugging-pipe` sets it), and a bot reading the page as text had
no way to know it was looking at a check rather than at the site, so it poked at it.

The page tests run a real Chrome (headless) and skip without one; the graph test runs
on the in-memory fakes from `test_bots.py`.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest

from runtime.computer.browser import Computer
from runtime.computer.engine import DesktopChrome, PlaywrightChromium
from runtime.computer.snapshot import MAX_ELEMENTS, MAX_TEXT_CHARS, SNAPSHOT_JS, render
from runtime.gateway.builtin.browser import _result
from tests.test_bot_seeing import CHROME
from tests.test_bot_vision import SeeingPage
from tests.test_bots import FakeBots, ScriptedModel, _Bot, _Ctx, _Node, _Org, _turn

needs_chrome = pytest.mark.skipif(not CHROME, reason="no Google Chrome on this machine")
ARGS = {"maxElements": MAX_ELEMENTS, "maxText": MAX_TEXT_CHARS}

RECAPTCHA = (
    "<iframe src='https://www.google.com/recaptcha/api2/anchor?k=x&size={size}' "
    "width=304 height=78></iframe>"
    "<textarea name=g-recaptcha-response style='display:none'>{token}</textarea>"
)
PAGES = {
    "a site": ("<title>Shop</title><a href='/cart'>Cart</a>", None),
    "Cloudflare's interstitial": (
        "<title>Just a moment...</title><script>window._cf_chl_opt = {}</script>"
        "<h1>Performing security verification</h1>",
        "cloudflare",
    ),
    "a reCAPTCHA checkbox": (RECAPTCHA.format(size="normal", token=""), "recaptcha"),
    "an answered reCAPTCHA": (RECAPTCHA.format(size="normal", token="03AF..."), None),
    "reCAPTCHA v3's badge": (RECAPTCHA.format(size="invisible", token=""), None),
    "an hCaptcha challenge not yet opened": (
        "<iframe src='https://newassets.hcaptcha.com/captcha/v1/x/static/hcaptcha.html"
        "#frame=challenge' style='display:none'></iframe>",
        None,
    ),
}


def test_the_listing_says_a_check_is_for_a_person() -> None:
    text = render({"url": "https://shop.test/", "challenge": "cloudflare", "elements": []})
    assert "human-verification check (cloudflare)" in text
    assert "A person has to answer it" in text
    assert "verification" not in render({"url": "https://shop.test/", "elements": []})


def test_the_gateway_passes_the_check_on() -> None:
    body = {"ok": True, "snapshot": {"challenge": "recaptcha", "elements": []}}
    assert _result(body).challenge == "recaptcha"
    assert _result({"ok": True, "snapshot": {"challenge": None}}).challenge == ""


@needs_chrome
async def test_a_real_page_names_the_check_in_front_of_it() -> None:
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=CHROME)
        try:
            page = await browser.new_page()
            # Each page loaded afresh (a new window object), and the widgets' frames
            # without going to Google or anyone else for them.
            served = {"html": ""}

            async def serve(route: Any) -> None:
                top = route.request.url.startswith("https://site.test/")
                await route.fulfill(body=served["html"] if top else "", content_type="text/html")

            await page.route("**/*", serve)
            for name, (html, expected) in PAGES.items():
                served["html"] = html
                await page.goto(f"https://site.test/{len(name)}")
                snap = await page.evaluate(SNAPSHOT_JS, ARGS)
                assert snap["challenge"] == expected, name
        finally:
            await browser.close()


@needs_chrome
async def test_a_check_that_passes_by_itself_is_waited_out(tmp_path: Path) -> None:
    computer = Computer(tmp_path / "profile", engine=PlaywrightChromium(executable=CHROME))
    try:
        screen = await computer.screen("bot")
        await screen.page.set_content(
            "<title>Just a moment...</title><script>window._cf_chl_opt = {};"
            "setTimeout(() => { delete window._cf_chl_opt; document.title = 'Shop'; }, 1500)"
            "</script>"
        )
        snap = await computer.observe(screen)
        assert snap["challenge"] is None
        assert snap["title"] == "Shop"
    finally:
        await computer.stop()


@needs_chrome
async def test_the_bots_chrome_does_not_say_it_is_automated(tmp_path: Path) -> None:
    assert CHROME is not None
    computer = Computer(
        tmp_path / "profile",
        engine=DesktopChrome(executable=CHROME, flags=("--headless=new",)),
    )
    try:
        screen = await computer.screen("bot")
        assert await screen.page.evaluate("navigator.webdriver") is False
    finally:
        await computer.stop()


class CheckedSite(SeeingPage):
    """The fake site, behind a check that does not pass by itself."""

    def _page(self) -> dict[str, Any]:
        return {**super()._page(), "challenge": "cloudflare"}


async def test_a_check_goes_to_the_person_before_the_model_is_asked_anything() -> None:
    bot = _Bot(id=uuid.uuid4())
    bots = FakeBots(bot)
    model = ScriptedModel([])
    out = await _turn(_Node(_Ctx(), CheckedSite(), model, _Org(bots)), bot.id)

    assert out == {"status": "human_needed", "steps": 0, "reason": "captcha"}
    assert model.prompts == [], "no model call spent on a page only a person can pass"
    (handoff,) = bots.said("system")
    assert "Take control of my screen" in handoff.content
    assert handoff.payload["captcha"] is True
    assert [kind for kind, _ in bots.screenshots.values()] == ["captcha"]
    assert bots.turns_ended == 1
