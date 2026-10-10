"""What a bot sees of a page, and what it is told when its steps do nothing.

Pinned from a real failure on Instagram: asked to follow an account's followers, a bot
clicked "1.4M followers" four times and reported that nothing happened. The followers
dialog *had* opened — at the end of `<body>`, past where a listing in document order
stopped — so the bot never saw it, was never told its clicks had changed nothing, and,
once it found the dialog, could not tell one "Follow" button from the next. An approved
"click [84]" was then replayed after the page had been re-read and renumbered.
"""

from __future__ import annotations

import os
import shutil
import uuid
from typing import Any

import pytest

from runtime.computer.snapshot import MAX_ELEMENTS, MAX_TEXT_CHARS, SNAPSHOT_JS, render
from runtime.domain.bots import BotStep
from tests.test_bots import FakeBots, ScriptedModel, _Bot, _Ctx, _Node, _Org, _Result, _turn

CHROME = (
    os.environ.get("COMPUTER_CHROME_PATH")
    or shutil.which("google-chrome")
    or next(
        (
            p
            for p in ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",)
            if os.path.exists(p)
        ),
        None,
    )
)


def test_a_dialog_is_listed_alone_and_lookalike_buttons_say_whose_they_are() -> None:
    text = render(
        {
            "url": "https://site.test/coursera/followers/",
            "title": "t",
            "dialog": {"name": "Followers"},
            "elements": [
                {"id": 1, "tag": "button", "label": "Close", "in_view": True},
                {
                    "id": 2,
                    "tag": "button",
                    "label": "Follow",
                    "near": "udemy Udemy",
                    "in_view": True,
                },
                {
                    "id": 3,
                    "tag": "button",
                    "label": "Follow",
                    "near": "adri.zip Adri",
                    "in_view": False,
                    "where": "hidden",
                },
                {
                    "id": 4,
                    "tag": "a",
                    "label": "1.4M followers",
                    "in_view": False,
                    "where": "above",
                },
            ],
            "text": "Followers udemy Udemy Follow",
        }
    )
    assert "A dialog 'Followers' is open over the page" in text
    assert "[2] button 'Follow' (beside 'udemy Udemy')" in text
    assert "[3] button 'Follow' (beside 'adri.zip Adri') (out of sight" in text
    assert "[4] a '1.4M followers' (above, scrolled past)" in text
    assert "Dialog text:" in text


class StillPage:
    """A page a click does not change — the bot clicks a link that opens nothing."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def execute(self, ctx, call):  # type: ignore[no-untyped-def]
        self.calls.append({"tool": call.tool, **call.args})
        return _Result(
            {
                "ok": True,
                "url": "https://site.test/p",
                "title": "t",
                "controller": "bot",
                "rendered": "URL: https://site.test/p",
                "elements": [{"id": 1, "tag": "a", "label": "1.4M followers", "in_view": True}],
            }
        )


async def test_a_click_that_changes_nothing_is_said_and_repeating_it_is_warned_about() -> None:
    bot = _Bot(id=uuid.uuid4())
    click = {"thought": "Open followers", "action": "click", "element": 1}
    model = ScriptedModel(
        [click, click, {"thought": "Stuck", "action": "reply", "text": "It won't open."}]
    )
    out = await _turn(_Node(_Ctx(), StillPage(), model, _Org(FakeBots(bot))), bot.id)

    assert out["status"] == "replied"
    assert "nothing on the page changed" in model.prompts[1]
    assert "WARNING" not in model.prompts[1]
    assert "WARNING: your last 2 steps were the same action" in model.prompts[2]


class ListPage:
    """A followers list whose rows move between reads, like a list that loads more."""

    def __init__(self, rows: list[str]) -> None:
        self.rows = rows
        self.calls: list[dict[str, Any]] = []

    async def execute(self, ctx, call):  # type: ignore[no-untyped-def]
        self.calls.append({"tool": call.tool, **call.args})
        return _Result(
            {
                "ok": True,
                "url": "https://site.test/followers",
                "title": "t",
                "controller": "bot",
                "rendered": "URL: https://site.test/followers",
                "elements": [
                    {"id": i + 1, "tag": "button", "label": "Follow", "near": name, "in_view": True}
                    for i, name in enumerate(self.rows)
                ],
            }
        )


async def _park_follow(page: ListPage, bots: FakeBots, bot: _Bot) -> Any:
    model = ScriptedModel(
        [{"thought": "Follow bcl", "action": "click", "element": 2, "sensitive": True}]
    )
    out = await _turn(_Node(_Ctx(), page, model, _Org(bots)), bot.id)
    assert out["status"] == "awaiting_approval"
    (pending,) = bots.pendings.values()
    assert pending.action["target"] == {"tag": "button", "label": "Follow", "near": "bcl.global"}
    pending.status = "allowed"
    return pending


async def test_an_approved_click_follows_its_element_when_the_list_has_moved() -> None:
    bot = _Bot(id=uuid.uuid4())
    bots = FakeBots(bot)
    page = ListPage(["udemy", "bcl.global", "adri.zip"])
    pending = await _park_follow(page, bots, bot)

    page.rows = ["new.one", "udemy", "bcl.global", "adri.zip"]
    model = ScriptedModel([{"thought": "Done", "action": "reply", "text": "Followed."}])
    await _turn(_Node(_Ctx(), page, model, _Org(bots)), bot.id, resume_pending_id=str(pending.id))

    clicked = [c for c in page.calls if c["tool"] == "browser.act@1"][-1]["action"]
    assert clicked == {"type": "click", "element": 3}, "bcl.global's button, not [2]"


async def test_an_approved_click_is_not_made_when_its_element_is_gone() -> None:
    bot = _Bot(id=uuid.uuid4())
    bots = FakeBots(bot)
    page = ListPage(["udemy", "bcl.global", "adri.zip"])
    pending = await _park_follow(page, bots, bot)

    page.rows = ["udemy", "someone.else", "adri.zip"]
    model = ScriptedModel([{"thought": "Gone", "action": "reply", "text": "It moved."}])
    await _turn(_Node(_Ctx(), page, model, _Org(bots)), bot.id, resume_pending_id=str(pending.id))

    assert not [c for c in page.calls if c["tool"] == "browser.act@1"]
    assert "NOT DONE: the page changed" in model.prompts[0]


def test_wait_can_pace_repeated_actions_but_not_hang_a_call() -> None:
    assert BotStep(thought="t", action="wait", seconds=30).seconds == 30
    with pytest.raises(ValueError):
        BotStep(thought="t", action="wait", seconds=31)


@pytest.mark.skipif(not CHROME, reason="no Google Chrome on this machine")
async def test_a_real_page_lists_its_dialog_not_the_page_behind_it() -> None:
    from playwright.async_api import async_playwright

    behind = "".join(f"<a href='/p/{i}'>Post {i} {'words ' * 10}</a><br>" for i in range(220))
    rows = "".join(
        f"<div><div><span>user{i}</span><span>Name {i}</span></div><button>Follow</button></div>"
        for i in range(30)
    )
    html = (
        f"<body><a href='/followers/'>1.4M followers</a>{behind}"
        "<div role=dialog style='position:fixed;inset:0;background:#0008'>"
        "<div role=dialog aria-label=Followers style='position:absolute;left:300px;"
        "top:100px;width:500px;height:500px;background:#fff'><button aria-label=Close>x"
        f"</button><div style='height:400px;overflow:auto'>{rows}</div></div></div></body>"
    )
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=CHROME)
        try:
            page = await browser.new_page(viewport={"width": 1280, "height": 800})
            await page.set_content(html)
            args = {"maxElements": MAX_ELEMENTS, "maxText": MAX_TEXT_CHARS}
            snap = await page.evaluate(SNAPSHOT_JS, args)
            assert snap["dialog"] == {"name": "Followers"}
            labels = [e["label"] for e in snap["elements"]]
            assert labels[0] == "Close" and labels.count("Follow") == 30
            assert snap["elements"][1]["near"] == "user0 Name 0"
            assert any(e.get("where") == "hidden" for e in snap["elements"])

            await page.evaluate(
                "document.querySelectorAll('[role=dialog]').forEach(d => d.remove())"
            )
            await page.evaluate("window.scrollTo(0, 650)")
            snap = await page.evaluate(SNAPSHOT_JS, args)
            assert snap["dialog"] is None
            header = snap["elements"][0]
            assert header["label"] == "1.4M followers" and header["where"] == "above"
        finally:
            await browser.close()


@pytest.mark.skipif(not CHROME, reason="no Google Chrome on this machine")
async def test_a_long_page_is_read_from_where_the_bot_has_scrolled() -> None:
    """A chat: a sidebar first in the document, then a thread far longer than the text
    budget. Read from the top it was the sidebar and the first messages every time."""
    from playwright.async_api import async_playwright

    side = "".join(f"<div>Old chat {i}</div>" for i in range(40))
    gap = "<div style='height:300px'></div>"  # a thread's spacing: a few messages a screen
    thread = "".join(
        f"<p>Message {i}: {'filler words ' * 30}<b>bold</b> end.</p>{gap}" for i in range(120)
    )
    html = (
        "<body style='margin:0'><nav style='position:fixed;left:0;top:0;width:200px;"
        f"height:100vh;overflow:auto'>{side}</nav>"
        f"<main style='margin-left:220px'>{thread}<p>THE ANSWER IS 42</p></main></body>"
    )
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=CHROME)
        try:
            page = await browser.new_page(viewport={"width": 1280, "height": 800})
            await page.set_content(html)
            args = {"maxElements": MAX_ELEMENTS, "maxText": MAX_TEXT_CHARS}
            top = await page.evaluate(SNAPSHOT_JS, args)
            assert "Message 0:" in top["text"] and "THE ANSWER" not in top["text"]
            assert top["text_truncated"] and not top["text_above"]

            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            end = await page.evaluate(SNAPSHOT_JS, args)
            assert "THE ANSWER IS 42" in end["text"]
            assert "Message 0:" not in end["text"] and end["text_above"]
            assert not end["text_truncated"]
            # A paragraph reads as one line, its inline parts spaced as written.
            assert "filler words bold end." in end["text"]
            assert "Old chat 0" in end["text"]  # what is on screen beside it stays
            assert "scroll up to read it again" in render(end)
        finally:
            await browser.close()


@pytest.mark.skipif(not CHROME, reason="no Google Chrome on this machine")
async def test_an_open_menu_is_listed_first_with_its_toggles_and_radios() -> None:
    """claude.ai's shape: a sidebar of chats long enough to fill the element budget, and
    a menu put at the end of <body> whose items are radio and checkbox items."""
    from playwright.async_api import async_playwright

    chats = "".join(f"<a href='/chat/{i}'>Chat {i}</a><br>" for i in range(300))
    html = (
        f"<body><nav>{chats}</nav><nav role=menu><a role=menuitem href=/x>Home</a></nav>"
        "<button aria-label='Add files, connectors, and more' aria-haspopup=menu "
        "aria-expanded=true>+</button>"
        "<div style='position:fixed;top:100px;left:300px'><div role=menu>"
        "<div role=menuitem tabindex=-1>Add files</div>"
        "<div role=menuitemcheckbox aria-checked=false tabindex=-1>Research</div>"
        "<div role=menuitemradio aria-checked=true tabindex=-1>Medium</div>"
        "<div role=menuitemradio aria-checked=false tabindex=-1>High</div>"
        "</div></div></body>"
    )
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=CHROME)
        try:
            page = await browser.new_page(viewport={"width": 1280, "height": 800})
            await page.set_content(html)
            args = {"maxElements": MAX_ELEMENTS, "maxText": MAX_TEXT_CHARS}
            snap = await page.evaluate(SNAPSHOT_JS, args)
            labels = [e["label"] for e in snap["elements"]]
            assert labels[:4] == ["Add files", "Research", "Medium", "High"]
            listing = render(snap)
            assert "A menu is open" in listing
            assert "menuitemcheckbox) 'Research' (off)" in listing
            assert "menuitemradio) 'Medium' (checked)" in listing

            await page.set_content(
                "<nav role=menu><a role=menuitem href=/x>Home</a></nav><button "
                "aria-label='Add files' aria-haspopup=menu aria-expanded=true>+</button>"
            )
            snap = await page.evaluate(SNAPSHOT_JS, args)
            assert not snap["menu"]  # a nav bar that is role=menu is not an open menu
            plus = next(e for e in snap["elements"] if e["label"].startswith("Add files"))
            assert "(open)" in render({**snap, "elements": [plus]})
        finally:
            await browser.close()
