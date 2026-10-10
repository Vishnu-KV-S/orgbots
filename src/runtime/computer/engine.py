"""Where a profile's browser comes from.

`Computer` owns screens, actions and takeover; an **engine** owns the browser under
them: how it is started for a profile, how a screen's page is opened in it, and how it
is stopped. Two engines, one interface:

- **`PlaywrightChromium`** — Playwright's own launch: a test harness's browser, headless
  by default, a fixed 1280x800 viewport. What the tests and a machine without Docker use.
- **`DesktopChrome`** — the bots' computer: Google Chrome as a person runs it, in windows
  on the computer's display (`DISPLAY`), with its own profile directory, and nothing on
  its command line but what this module puts there — none of the switches a test harness
  adds (`--enable-automation`, `--headless`, a fake screen size, a software GPU), and
  `navigator.webdriver` false, as it is in a person's Chrome. Each
  screen is its own window, so every bot's page is the visible tab of a window and keeps
  rendering, the way it would on a person's desktop. Runs in `docker/computer`.

`engine_from_env()` picks one: `COMPUTER_BROWSER=desktop` or `playwright` (the default).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shlex
from pathlib import Path
from typing import Any

from playwright.async_api import BrowserContext, Page, Playwright, async_playwright
from playwright.async_api import Error as PlaywrightError

PLAYWRIGHT_VIEWPORT = {"width": 1280, "height": 800}
STALE_LOCKS = ("SingletonLock", "SingletonSocket", "SingletonCookie")


class Engine:
    """A browser per profile. Subclasses say how one is started."""

    name = "engine"

    def __init__(self) -> None:
        self._pw: Playwright | None = None
        self._lock = asyncio.Lock()

    async def _playwright(self) -> Playwright:
        async with self._lock:
            if self._pw is None:
                self._pw = await async_playwright().start()
            return self._pw

    async def open(self, profile_dir: Path, *, accept_downloads: bool) -> BrowserContext:
        raise NotImplementedError

    async def new_page(self, context: BrowserContext) -> Page:
        return await context.new_page()

    async def close(self, context: BrowserContext) -> None:
        with contextlib.suppress(PlaywrightError):
            await context.close()

    async def stop(self) -> None:
        async with self._lock:
            if self._pw is not None:
                await self._pw.stop()
                self._pw = None

    def describe(self) -> dict[str, Any]:
        return {"engine": self.name}


class PlaywrightChromium(Engine):
    name = "playwright"

    def __init__(self, *, headless: bool = True, executable: str | None = None) -> None:
        super().__init__()
        self._headless = headless
        self._executable = executable

    async def open(self, profile_dir: Path, *, accept_downloads: bool) -> BrowserContext:
        pw = await self._playwright()
        kwargs: dict[str, Any] = {
            "headless": self._headless,
            "viewport": PLAYWRIGHT_VIEWPORT,
            "args": ["--no-first-run", "--no-default-browser-check"],
            "accept_downloads": accept_downloads,
        }
        if self._executable:
            kwargs["executable_path"] = self._executable
        return await pw.chromium.launch_persistent_context(str(profile_dir), **kwargs)


class DesktopChrome(Engine):
    name = "desktop"

    # Not one of these is visible to a page. They keep every bot's window painting and
    # its timers running while another window is on top of it — on a person's desktop
    # the window would be in front when it mattered; here several bots work at once.
    # The last one is the exception, by intent: being driven over
    # `--remote-debugging-pipe` makes Chrome report `navigator.webdriver === true` to
    # every page, which Cloudflare and every other bot check reads first — and a page
    # that decides it is talking to a robot shows a challenge instead of itself.
    BASE_FLAGS = (
        "--no-first-run",
        "--no-default-browser-check",
        "--start-maximized",
        "--hide-crash-restore-bubble",
        "--password-store=basic",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-background-timer-throttling",
        "--disable-blink-features=AutomationControlled",
    )

    def __init__(
        self,
        *,
        executable: str = "google-chrome",
        display: str | None = None,
        flags: tuple[str, ...] = (),
    ) -> None:
        super().__init__()
        self._executable = executable
        self._display = display
        self._flags = flags

    async def open(self, profile_dir: Path, *, accept_downloads: bool) -> BrowserContext:
        pw = await self._playwright()
        _clear_stale_locks(profile_dir)
        env: dict[str, str | float | bool] = {**os.environ}
        if self._display:
            env["DISPLAY"] = self._display
        context = await pw.chromium.launch_persistent_context(
            str(profile_dir),
            executable_path=self._executable,
            headless=False,
            no_viewport=True,
            # Exactly these switches and no others: Playwright's defaults are a test
            # harness's (`--enable-automation`, `--disable-extensions`, a fixed colour
            # profile...), and a page can tell.
            ignore_default_args=True,
            args=[
                "--remote-debugging-pipe",
                f"--user-data-dir={profile_dir}",
                *self.BASE_FLAGS,
                *self._flags,
                "about:blank",
            ],
            accept_downloads=accept_downloads,
            env=env,
        )
        return context

    async def new_page(self, context: BrowserContext) -> Page:
        """A new window, not a tab: a background tab is hidden, and a hidden page stops
        painting and throttles its timers — and some sites notice."""
        opener = next((p for p in context.pages if not p.is_closed()), None)
        if opener is None:
            return await context.new_page()
        session = await context.new_cdp_session(opener)
        try:
            async with context.expect_page() as made:
                await session.send("Target.createTarget", {"url": "about:blank", "newWindow": True})
            return await made.value
        finally:
            with contextlib.suppress(PlaywrightError):
                await session.detach()

    def describe(self) -> dict[str, Any]:
        return {"engine": self.name, "display": self._display}


def _clear_stale_locks(profile_dir: Path) -> None:
    """A container that was stopped hard leaves Chrome's single-instance lock behind,
    naming a host and process that no longer exist; Chrome would refuse the profile.
    Only this engine starts Chrome on a profile, so a lock here is always stale."""
    for name in STALE_LOCKS:
        with contextlib.suppress(FileNotFoundError):
            (profile_dir / name).unlink()


def engine_from_env() -> Engine:
    kind = os.environ.get("COMPUTER_BROWSER", "playwright").strip().lower()
    if kind == "desktop":
        return DesktopChrome(
            executable=os.environ.get("COMPUTER_CHROME_PATH", "google-chrome"),
            display=os.environ.get("DISPLAY"),
            flags=tuple(shlex.split(os.environ.get("COMPUTER_CHROME_FLAGS", ""))),
        )
    if kind != "playwright":
        raise ValueError(f"COMPUTER_BROWSER must be 'desktop' or 'playwright', not {kind!r}")
    return PlaywrightChromium(
        headless=os.environ.get("RUNTIME_COMPUTER_HEADLESS", "true").lower() != "false",
        executable=os.environ.get("COMPUTER_CHROMIUM_PATH") or None,
    )
