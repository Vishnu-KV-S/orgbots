"""The shared browser and its screens.

One persistent Chromium context — so a login done once, by a person or by a bot, is a
login every bot has — and one page per screen. A screen is keyed by the bot's id.

**Control is per screen and it is exclusive.** While a person holds a screen (to type a
password, solve a CAPTCHA, finish a 2FA prompt) every bot action on it is refused with
`HumanInControlError`, and the bot's run reports that and stops rather than fighting the
person for the mouse. Handing control back is an explicit act.

**Actions are paced like a person's.** The pointer travels to an element before it
clicks, keys are typed one at a time, and there is a short settle after each action.
Partly so sites that watch for robotic input behave normally, and partly so that a
person watching the screen can follow what is happening.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)
from playwright.async_api import (
    Error as PlaywrightError,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeout,
)

from runtime.computer.snapshot import MAX_ELEMENTS, MAX_TEXT_CHARS, SNAPSHOT_JS

VIEWPORT = {"width": 1280, "height": 800}
ACTION_TIMEOUT_MS = 15_000
NAV_TIMEOUT_MS = 30_000
HOME_URL = "about:blank"


class ComputerError(Exception):
    """An action that could not be carried out. The message is shown to the bot."""


class HumanInControlError(ComputerError):
    pass


class UnknownElementError(ComputerError):
    pass


@dataclass
class Screen:
    screen_id: str
    page: Page
    controller: str = "bot"
    """`bot` or `human`."""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    mouse: tuple[float, float] = (VIEWPORT["width"] / 2, VIEWPORT["height"] / 2)
    last_action: str = ""
    last_active: float = field(default_factory=time.time)
    label: str = ""


class Computer:
    def __init__(self, profile_dir: Path, *, headless: bool = True) -> None:
        self._profile_dir = profile_dir
        self._headless = headless
        self._pw: Playwright | None = None
        self._context: BrowserContext | None = None
        self._browser: Browser | None = None
        self._screens: dict[str, Screen] = {}
        self._start_lock = asyncio.Lock()
        self.started_at: float | None = None

    # --- lifecycle -------------------------------------------------------------------

    async def start(self) -> None:
        async with self._start_lock:
            if self._context is not None:
                return
            self._profile_dir.mkdir(parents=True, exist_ok=True)
            self._pw = await async_playwright().start()
            kwargs: dict[str, Any] = {
                "headless": self._headless,
                "viewport": VIEWPORT,
                "args": ["--no-first-run", "--no-default-browser-check"],
            }
            executable = os.environ.get("COMPUTER_CHROMIUM_PATH")
            if executable:
                kwargs["executable_path"] = executable
            self._context = await self._pw.chromium.launch_persistent_context(
                str(self._profile_dir), **kwargs
            )
            self._context.set_default_timeout(ACTION_TIMEOUT_MS)
            self._context.set_default_navigation_timeout(NAV_TIMEOUT_MS)
            # The persistent context opens with one blank page; it is nobody's screen.
            self.started_at = time.time()

    async def stop(self) -> None:
        async with self._start_lock:
            self._screens.clear()
            if self._context is not None:
                await self._context.close()
                self._context = None
            if self._pw is not None:
                await self._pw.stop()
                self._pw = None

    async def reset(self) -> None:
        """Recover: restart the browser. Logins survive — they live in the profile."""
        await self.stop()
        await self.start()

    @property
    def running(self) -> bool:
        return self._context is not None

    # --- screens ---------------------------------------------------------------------

    async def screen(self, screen_id: str, *, label: str = "") -> Screen:
        await self.start()
        existing = self._screens.get(screen_id)
        if existing is not None and not existing.page.is_closed():
            if label:
                existing.label = label
            return existing
        assert self._context is not None
        page = await self._context.new_page()
        await page.goto(HOME_URL)
        screen = Screen(screen_id=screen_id, page=page, label=label)
        self._screens[screen_id] = screen
        return screen

    def screens(self) -> list[Screen]:
        return [s for s in self._screens.values() if not s.page.is_closed()]

    async def close_screen(self, screen_id: str) -> None:
        screen = self._screens.pop(screen_id, None)
        if screen is not None and not screen.page.is_closed():
            await screen.page.close()

    def set_controller(self, screen: Screen, controller: str) -> None:
        if controller not in ("bot", "human"):
            raise ComputerError(f"unknown controller {controller!r}")
        screen.controller = controller

    # --- reading ---------------------------------------------------------------------

    async def observe(self, screen: Screen) -> dict[str, Any]:
        page = screen.page
        with contextlib.suppress(PlaywrightTimeout):
            await page.wait_for_load_state("domcontentloaded", timeout=5_000)
        try:
            snap: dict[str, Any] = await page.evaluate(
                SNAPSHOT_JS, {"maxElements": MAX_ELEMENTS, "maxText": MAX_TEXT_CHARS}
            )
        except PlaywrightError as exc:
            # A navigation in flight destroys the execution context. One retry after
            # the page settles is the whole cure.
            await asyncio.sleep(0.8)
            try:
                snap = await page.evaluate(
                    SNAPSHOT_JS, {"maxElements": MAX_ELEMENTS, "maxText": MAX_TEXT_CHARS}
                )
            except PlaywrightError:
                raise ComputerError(f"could not read the page: {exc}") from exc
        snap["controller"] = screen.controller
        return snap

    async def screenshot(self, screen: Screen, *, quality: int = 70) -> bytes:
        return await screen.page.screenshot(type="jpeg", quality=quality)

    # --- bot actions -----------------------------------------------------------------

    async def act(self, screen: Screen, action: dict[str, Any]) -> dict[str, Any]:
        """Perform one bot action and return a fresh observation."""
        if screen.controller != "bot":
            raise HumanInControlError(
                "a person has taken control of this screen; wait for them to hand it back"
            )
        async with screen.lock:
            kind = str(action.get("type", ""))
            handler = _ACTIONS.get(kind)
            if handler is None:
                raise ComputerError(f"unknown action {kind!r}; known: {sorted(_ACTIONS)}")
            await handler(self, screen, action)
            screen.last_action = kind
            screen.last_active = time.time()
            await _settle(screen.page)
        return await self.observe(screen)

    async def _element(self, screen: Screen, action: dict[str, Any]) -> Any:
        raw = action.get("element")
        if raw is None:
            raise UnknownElementError(f"{action.get('type')} needs an element number")
        handle = await screen.page.query_selector(f'[data-bid="{int(raw)}"]')
        if handle is None:
            raise UnknownElementError(
                f"element [{raw}] is not on the page any more; observe again and use a "
                "number from the latest listing"
            )
        return handle

    async def _move_to(self, screen: Screen, handle: Any) -> tuple[float, float]:
        await handle.scroll_into_view_if_needed(timeout=5_000)
        box = await handle.bounding_box()
        if box is None:
            raise ComputerError("that element has no position on screen (hidden?)")
        x = box["x"] + box["width"] * random.uniform(0.35, 0.65)  # noqa: S311 - pacing, not crypto
        y = box["y"] + box["height"] * random.uniform(0.35, 0.65)  # noqa: S311
        await screen.page.mouse.move(x, y, steps=random.randint(8, 18))  # noqa: S311
        screen.mouse = (x, y)
        await asyncio.sleep(random.uniform(0.05, 0.2))  # noqa: S311
        return x, y

    # --- human takeover --------------------------------------------------------------

    async def human_input(self, screen: Screen, event: dict[str, Any]) -> None:
        """Input from a person who holds the screen. Coordinates are viewport pixels."""
        if screen.controller != "human":
            raise ComputerError("take control of the screen first")
        page = screen.page
        kind = event.get("kind")
        async with screen.lock:
            if kind == "click":
                x, y = float(event["x"]), float(event["y"])
                await page.mouse.click(x, y)
                screen.mouse = (x, y)
            elif kind == "type":
                await page.keyboard.type(str(event.get("text", "")), delay=20)
            elif kind == "key":
                await page.keyboard.press(str(event["key"]))
            elif kind == "scroll":
                await page.mouse.wheel(0, float(event.get("dy", 400)))
            elif kind == "navigate":
                await page.goto(_normalise_url(str(event["url"])))
            elif kind == "back":
                await page.go_back()
            elif kind == "forward":
                await page.go_forward()
            elif kind == "reload":
                await page.reload()
            else:
                raise ComputerError(f"unknown input {kind!r}")
            screen.last_active = time.time()


# --- action implementations -----------------------------------------------------------


async def _navigate(c: Computer, s: Screen, a: dict[str, Any]) -> None:
    url = _normalise_url(str(a.get("url", "")))
    try:
        await s.page.goto(url, wait_until="domcontentloaded")
    except PlaywrightTimeout as exc:
        raise ComputerError(f"{url} did not load within {NAV_TIMEOUT_MS // 1000}s") from exc
    except PlaywrightError as exc:
        raise ComputerError(f"could not open {url}: {exc.message.splitlines()[0]}") from exc


async def _click(c: Computer, s: Screen, a: dict[str, Any]) -> None:
    handle = await c._element(s, a)
    x, y = await c._move_to(s, handle)
    await s.page.mouse.click(x, y, delay=random.randint(40, 120))  # noqa: S311


async def _type(c: Computer, s: Screen, a: dict[str, Any]) -> None:
    handle = await c._element(s, a)
    x, y = await c._move_to(s, handle)
    await s.page.mouse.click(x, y)
    if a.get("clear", True):
        await handle.evaluate(
            "el => { if ('value' in el) { el.value = ''; "
            "el.dispatchEvent(new Event('input', {bubbles: true})); } "
            "else if (el.isContentEditable) { el.textContent = ''; } }"
        )
    text = str(a.get("text", ""))
    await s.page.keyboard.type(text, delay=random.randint(25, 70))  # noqa: S311
    if a.get("submit"):
        await asyncio.sleep(0.2)
        await s.page.keyboard.press("Enter")


async def _press(c: Computer, s: Screen, a: dict[str, Any]) -> None:
    key = str(a.get("key", "")).strip()
    if not key:
        raise ComputerError("press needs a key, e.g. Enter, Tab, Escape, Control+A")
    await s.page.keyboard.press(key)


async def _select(c: Computer, s: Screen, a: dict[str, Any]) -> None:
    handle = await c._element(s, a)
    await c._move_to(s, handle)
    option = str(a.get("option", a.get("text", "")))
    try:
        await handle.select_option(label=option)
    except PlaywrightError:
        await handle.select_option(value=option)


async def _scroll(c: Computer, s: Screen, a: dict[str, Any]) -> None:
    direction = str(a.get("direction", "down"))
    amount = float(a.get("amount", 0) or VIEWPORT["height"] * 0.8)
    dy = -amount if direction == "up" else amount
    # A few wheel ticks rather than one jump, the way a hand scrolls.
    for _ in range(4):
        await s.page.mouse.wheel(0, dy / 4)
        await asyncio.sleep(0.06)


async def _hover(c: Computer, s: Screen, a: dict[str, Any]) -> None:
    handle = await c._element(s, a)
    await c._move_to(s, handle)


async def _back(c: Computer, s: Screen, a: dict[str, Any]) -> None:
    await s.page.go_back()


async def _forward(c: Computer, s: Screen, a: dict[str, Any]) -> None:
    await s.page.go_forward()


async def _reload(c: Computer, s: Screen, a: dict[str, Any]) -> None:
    await s.page.reload()


async def _wait(c: Computer, s: Screen, a: dict[str, Any]) -> None:
    await asyncio.sleep(min(10.0, max(0.0, float(a.get("seconds", 1)))))


_ACTIONS = {
    "navigate": _navigate,
    "click": _click,
    "type": _type,
    "press": _press,
    "select": _select,
    "scroll": _scroll,
    "hover": _hover,
    "back": _back,
    "forward": _forward,
    "reload": _reload,
    "wait": _wait,
}

ACTION_TYPES = frozenset(_ACTIONS)


async def _settle(page: Page) -> None:
    with contextlib.suppress(PlaywrightTimeout):
        await page.wait_for_load_state("domcontentloaded", timeout=5_000)
    await asyncio.sleep(0.4)


def _normalise_url(url: str) -> str:
    url = url.strip()
    if not url:
        raise ComputerError("navigate needs a url")
    lowered = url.lower()
    if lowered.startswith(("file:", "chrome:", "chrome-extension:", "view-source:", "javascript:")):
        # The computer shares a host with the runtime; a bot that could open file://
        # could read the .env next to it.
        raise ComputerError(f"{url.split(':', 1)[0]}: URLs are not allowed")
    if lowered.startswith(("http://", "https://", "about:")):
        return url
    if " " in url or "." not in url:
        # Not a URL — treat it as a search, which is what a person typing into the
        # address bar would get.
        from urllib.parse import quote_plus

        return f"https://duckduckgo.com/?q={quote_plus(url)}"
    return f"https://{url}"
