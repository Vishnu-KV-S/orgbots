"""The shared browser and its screens.

A persistent Chromium context per **profile** — so a login done once, by a person or
by a bot, is a login every bot in that profile has — and one page per screen. A screen
is keyed by the bot's id. Without members there is one profile, `""`, in the profile
directory; with members each member's bots share theirs and each team bot has its own
(`domain.members.computer_profile`), in a sibling directory, so nobody's bot is signed
in as somebody else. A screen asked for in a different profile than it has (its bot
was shared, or made private) is closed and opened again in the new one.

**Control is per screen and it is exclusive.** While a person holds a screen (to type a
password, solve a CAPTCHA, finish a 2FA prompt) every bot action on it is refused with
`HumanInControlError`, and the bot's run reports that and stops rather than fighting the
person for the mouse. Handing control back is an explicit act.

**Actions are paced like a person's.** The pointer travels to an element before it
clicks, keys are typed one at a time, and there is a short settle after each action.
Partly so sites that watch for robotic input behave normally, and partly so that a
person watching the screen can follow what is happening.

**A person can teach by doing** (`start_recording`). While a screen is recorded, every
input the person makes through `human_input` is written down as a step — what was
clicked (its role and label, read off the page), what was typed and into what, keys,
scrolls and pages — for a bot to turn into a skill. Typing into a password, code or
card box is recorded as `•••`: the value is never kept, here or anywhere after.
Recording stops at `RECORDING_STEPS` steps or `RECORDING_SECONDS`.

**`fill` is how a login gets typed, and it is the last line of the vault's checks.**
It types values the gateway opened from the vault — values no bot has seen — and it
refuses unless the page is on the site the values belong to, checked immediately
before each field because a page can navigate on its own. A password goes only into a
password box, so a page cannot talk a bot into aiming one at a text field it will
read back. Values are never logged and never in an error message.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import random
import re
import time
from collections.abc import Callable
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
SEALED_SELECTOR = (
    "input[type=password], [data-vault-filled], input[autocomplete~=one-time-code], "
    "input[autocomplete^=cc-]"
)
"""Fields whose contents a bot never reads — the same set `snapshot.SNAPSHOT_JS` seals."""
RECORDING_STEPS = 200
RECORDING_SECONDS = 600
"""A demonstration's limits — the same as `domain.skills`, which this process may not
import (`runtime.computer` imports nothing from the runtime)."""

_SECRET_LABEL = re.compile(
    r"pass(word)?|secret|token|otp|2fa|cvv|cvc|card.?number|security code|\bpin\b", re.I
)

DESCRIBE_JS = """
([x, y, focused]) => {
  let el = focused ? document.activeElement : document.elementFromPoint(x, y);
  if (!el || el === document.body || el === document.documentElement) return null;
  const target = el.closest(
    'a,button,input,select,textarea,summary,label,[role=button],[role=link],[role=tab],' +
    '[role=menuitem],[role=checkbox],[role=option],[role=switch],[contenteditable=true]'
  ) || el;
  const clean = (s) => (s || '').replace(/\\s+/g, ' ').trim().slice(0, 80);
  const labelled = target.labels && target.labels.length ? target.labels[0].innerText : '';
  const label = clean(target.getAttribute('aria-label')) || clean(labelled) ||
    clean(target.getAttribute('placeholder')) || clean(target.innerText) ||
    clean(target.getAttribute('title')) || clean(target.getAttribute('alt')) ||
    clean(target.getAttribute('name')) || clean(target.value && target.type === 'submit'
      ? target.value : '');
  const type = (target.getAttribute('type') || '').toLowerCase();
  const auto = (target.getAttribute('autocomplete') || '').toLowerCase();
  const sealed = type === 'password' || target.hasAttribute('data-vault-filled') ||
    auto.includes('one-time-code') || auto.startsWith('cc-');
  return {
    tag: target.tagName.toLowerCase(),
    role: target.getAttribute('role') || '',
    label, type,
    href: target.tagName === 'A' ? (target.href || '').slice(0, 300) : '',
    sealed,
  };
}
"""
"""What a person's click or typing landed on, for a recording: role and label, never a
field's value. `sealed` is the same set the snapshot seals."""

ACTION_TIMEOUT_MS = 15_000
NAV_TIMEOUT_MS = 30_000
HOME_URL = "about:blank"


class ComputerError(Exception):
    """An action that could not be carried out. The message is shown to the bot."""


class HumanInControlError(ComputerError):
    pass


class UnknownElementError(ComputerError):
    pass


PROFILE = re.compile(r"^(|[mt]-[0-9a-f]{32})$")
"""A profile key: `""`, a member's (`m-…`) or a team bot's (`t-…`). It becomes a
directory name, so nothing else is accepted."""


def check_profile(profile: str) -> str:
    if not PROFILE.match(profile):
        raise ComputerError(f"{profile!r} is not a browser profile")
    return profile


_NO_NETWORK = ("about:", "data:", "blob:", "javascript:")


def url_allowed(url: str, hosts: tuple[str, ...] | None) -> bool:
    """`hosts` None is an open network. Otherwise a host covers its subdomains — the
    same rule as `domain.policies`, which this process may not import."""
    if hosts is None or url.startswith(_NO_NETWORK):
        return True
    scheme, _, rest = url.partition("://")
    if scheme not in ("http", "https", "ws", "wss"):
        return False
    host = rest.split("/", 1)[0].rsplit("@", 1)[-1]
    host = host.rsplit(":", 1)[0] if not host.startswith("[") else host
    host = host.lower().rstrip(".")
    return any(host == h or host.endswith("." + h) for h in hosts)


@dataclass
class Screen:
    screen_id: str
    page: Page
    profile: str = ""
    controller: str = "bot"
    """`bot` or `human`."""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    mouse: tuple[float, float] = (VIEWPORT["width"] / 2, VIEWPORT["height"] / 2)
    last_action: str = ""
    last_active: float = field(default_factory=time.time)
    label: str = ""
    recording: Recording | None = None
    downloads: list[dict[str, Any]] = field(default_factory=list)
    downloading: int = 0
    """Files this screen's page downloaded into the workspace, newest last. A download
    not yet `seen` is reported with the next observation, so the bot learns where it
    went."""


@dataclass
class Recording:
    """A demonstration in progress: the steps so far, and when it began."""

    started_at: float = field(default_factory=time.time)
    steps: list[dict[str, Any]] = field(default_factory=list)
    full: bool = False

    def expired(self) -> bool:
        return self.full or time.time() - self.started_at > RECORDING_SECONDS

    def add(self, step: dict[str, Any]) -> None:
        if self.expired():
            return
        step["at"] = round(time.time() - self.started_at, 1)
        last = self.steps[-1] if self.steps else None
        # Typing arrives a few characters at a time and scrolling a notch at a time; one
        # field's typing and one run of scrolling are one step each.
        same_page = last is not None and last.get("url") == step.get("url")
        if (
            same_page
            and last is not None
            and last["kind"] == step["kind"] == "type"
            and last.get("target") == step.get("target")
        ):
            if not (step.get("target") or {}).get("secret"):
                last["text"] = (str(last.get("text", "")) + str(step.get("text", "")))[:400]
            return
        if same_page and last is not None and last["kind"] == step["kind"] == "scroll":
            last["dy"] = float(last.get("dy", 0)) + float(step.get("dy", 0))
            return
        self.steps.append(step)
        if len(self.steps) >= RECORDING_STEPS:
            self.full = True


class Computer:
    def __init__(
        self,
        profile_dir: Path,
        *,
        headless: bool = True,
        download_path: Callable[[str, str], Path] | None = None,
        shown: Callable[[Path, str], str] | None = None,
    ) -> None:
        self._profile_dir = profile_dir
        self._headless = headless
        self._download_path = download_path
        self._shown = shown or (lambda path, profile: str(path))
        self._pw: Playwright | None = None
        self._contexts: dict[str, BrowserContext] = {}
        self._allow: dict[str, tuple[str, ...] | None] = {}
        """Per profile: the hosts its browser may reach, or None for any. Set by the
        runtime with every call (`set_allow`), from the organization's policy."""
        self._guarded: set[str] = set()
        self._browser: Browser | None = None
        self._screens: dict[str, Screen] = {}
        self._start_lock = asyncio.Lock()
        self.started_at: float | None = None

    def profile_path(self, profile: str) -> Path:
        """The default profile is the profile directory; any other is a sibling of it."""
        check_profile(profile)
        if not profile:
            return self._profile_dir
        return self._profile_dir.with_name(f"{self._profile_dir.name}-{profile}")

    # --- lifecycle -------------------------------------------------------------------

    async def start(self) -> None:
        await self._context_for("")

    async def _context_for(self, profile: str) -> BrowserContext:
        async with self._start_lock:
            existing = self._contexts.get(profile)
            if existing is not None:
                return existing
            path = self.profile_path(profile)
            path.mkdir(parents=True, exist_ok=True)
            if self._pw is None:
                self._pw = await async_playwright().start()
            kwargs: dict[str, Any] = {
                "headless": self._headless,
                "viewport": VIEWPORT,
                "args": ["--no-first-run", "--no-default-browser-check"],
                "accept_downloads": self._download_path is not None,
            }
            executable = os.environ.get("COMPUTER_CHROMIUM_PATH")
            if executable:
                kwargs["executable_path"] = executable
            context = await self._pw.chromium.launch_persistent_context(str(path), **kwargs)
            context.set_default_timeout(ACTION_TIMEOUT_MS)
            context.set_default_navigation_timeout(NAV_TIMEOUT_MS)
            # The persistent context opens with one blank page; it is nobody's screen.
            self._contexts[profile] = context
            await self._guard(profile, context)
            if self.started_at is None:
                self.started_at = time.time()
            return context

    async def set_allow(self, profile: str, hosts: tuple[str, ...] | None) -> None:
        """The organization's network policy for a profile: only `hosts` (and their
        subdomains) from now on, or anywhere with None. Every request the profile's
        browser makes — pages, redirects, frames, images, scripts — is held to it."""
        check_profile(profile)
        if self._allow.get(profile, None) == hosts and profile in self._allow:
            return
        self._allow[profile] = hosts
        context = self._contexts.get(profile)
        if context is not None:
            await self._guard(profile, context)
        # A page opened before the policy said no is still on its screen, readable; a
        # screen showing a host that is no longer allowed goes blank.
        for screen in self.screens():
            if screen.profile == profile and not url_allowed(screen.page.url, hosts):
                with contextlib.suppress(PlaywrightError):
                    await screen.page.goto(HOME_URL)

    async def _guard(self, profile: str, context: BrowserContext) -> None:
        wanted = self._allow.get(profile) is not None
        if wanted and profile not in self._guarded:

            async def check(route: Any) -> None:
                if url_allowed(route.request.url, self._allow.get(profile)):
                    await route.continue_()
                else:
                    await route.abort("blockedbyclient")

            await context.route("**/*", check)
            self._guarded.add(profile)
        elif not wanted and profile in self._guarded:
            await context.unroute("**/*")
            self._guarded.discard(profile)

    async def stop(self) -> None:
        async with self._start_lock:
            self._screens.clear()
            for context in self._contexts.values():
                with contextlib.suppress(PlaywrightError):
                    await context.close()
            self._contexts.clear()
            self._guarded.clear()
            if self._pw is not None:
                await self._pw.stop()
                self._pw = None
            self.started_at = None

    async def reset(self) -> None:
        """Recover: restart the browser. Logins survive — they live in the profiles."""
        await self.stop()
        await self.start()

    @property
    def running(self) -> bool:
        return bool(self._contexts)

    @property
    def profiles(self) -> list[str]:
        return sorted(self._contexts)

    # --- screens ---------------------------------------------------------------------

    async def screen(self, screen_id: str, *, label: str = "", profile: str = "") -> Screen:
        check_profile(profile)
        existing = self._screens.get(screen_id)
        if existing is not None and not existing.page.is_closed():
            if existing.profile == profile:
                if label:
                    existing.label = label
                return existing
            # The bot was shared or made private: its screen moves to the new profile.
            await self.close_screen(screen_id)
        context = await self._context_for(profile)
        page = await context.new_page()
        await page.goto(HOME_URL)
        screen = Screen(screen_id=screen_id, page=page, profile=profile, label=label)
        if self._download_path is not None:
            page.on("download", lambda d: asyncio.ensure_future(self._save_download(screen, d)))
        self._screens[screen_id] = screen
        return screen

    async def _save_download(self, screen: Screen, download: Any) -> None:
        """Keep a download in the workspace, under its own name (numbered if taken)."""
        assert self._download_path is not None
        screen.downloading += 1
        entry: dict[str, Any] = {"name": str(download.suggested_filename), "seen": False}
        try:
            target = self._download_path(download.suggested_filename or "download", screen.profile)
            await download.save_as(str(target))
            entry.update(
                name=target.name,
                path=self._shown(target, screen.profile),
                bytes=target.stat().st_size,
                url=str(download.url).split("?", 1)[0][:300],
            )
        except Exception as exc:  # a failed download is reported, not raised into Playwright
            entry["error"] = str(exc)[:200]
        finally:
            entry["at"] = time.time()
            screen.downloads.append(entry)
            del screen.downloads[:-20]
            screen.downloading -= 1

    def unseen_downloads(self, screen: Screen, *, consume: bool = True) -> list[dict[str, Any]]:
        """Downloads the bot has not been told about. Only an observation consumes them:
        the bot's next decision is made from an observation, never from an act's result,
        so a download reported only by an act would never reach it."""
        fresh = [d for d in screen.downloads if not d.get("seen")]
        if consume:
            for d in fresh:
                d["seen"] = True
        return fresh

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

    # --- demonstrations --------------------------------------------------------------

    async def start_recording(self, screen: Screen) -> Recording:
        """Begin recording a person's inputs on this screen. They hold it for the length
        of the recording: a bot acting on it would be recorded as the person."""
        screen.controller = "human"
        screen.recording = Recording()
        screen.recording.add(
            {"kind": "start", "url": _shown_url(screen.page.url), "title": await _title(screen)}
        )
        return screen.recording

    def stop_recording(self, screen: Screen) -> list[dict[str, Any]]:
        """End the recording and return its steps. The screen stays with the person
        until they, or the API on their behalf, hand it back."""
        recording, screen.recording = screen.recording, None
        return list(recording.steps) if recording is not None else []

    async def _describe(self, screen: Screen, x: float, y: float, *, focused: bool) -> Any:
        try:
            target = await screen.page.evaluate(DESCRIBE_JS, [x, y, focused])
        except PlaywrightError:
            return None
        if target:
            target["secret"] = bool(target.pop("sealed", False)) or bool(
                _SECRET_LABEL.search(f"{target.get('label', '')} {target.get('type', '')}")
            )
        return target

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
        """What a *person* sees — the watch-and-take-control view. Unmasked: they are
        looking at their own screen, and a person who typed a value may see it."""
        return await screen.page.screenshot(type="jpeg", quality=quality)

    async def bot_screenshot(self, screen: Screen, *, quality: int = 60) -> bytes:
        """What a *bot's* vision model sees. Everything the page snapshot seals is
        masked here too — password and code boxes, card fields, anything the vault
        filled — so a screenshot cannot show a model the email address the text view
        reports only as `(filled)`. Viewport only, never the full page: the bot
        scrolls for the rest, and a full-page capture of a long feed is megabytes."""
        page = screen.page
        return await page.screenshot(
            type="jpeg",
            quality=quality,
            mask=[page.locator(SEALED_SELECTOR)],
            mask_color="#7f7f7f",
        )

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

    # --- the vault's fill ------------------------------------------------------------

    async def fill(
        self,
        screen: Screen,
        *,
        expect_host: str,
        fields: list[dict[str, Any]],
        submit: bool,
    ) -> dict[str, Any]:
        """Type sign-in values into the form, then (optionally) submit with Enter.

        `fields` are `{elements, value, password}`. Several elements are one code split
        across boxes: a code as long as there are boxes goes one character to a box,
        anything else is typed into the first and left to the page's own paste logic.
        """
        if screen.controller != "bot":
            raise HumanInControlError(
                "a person has taken control of this screen; wait for them to hand it back"
            )
        async with screen.lock:
            last: Any = None
            for spec in fields:
                elements = [int(e) for e in spec.get("elements", [])]
                value = str(spec.get("value", ""))
                if not elements or not value:
                    continue
                _check_site(screen.page.url, expect_host)
                handles = [
                    await self._element(screen, {"type": "fill", "element": e}) for e in elements
                ]
                for number, handle in zip(elements, handles, strict=True):
                    await _check_fillable(handle, number, password=bool(spec.get("password")))
                pieces = list(value) if len(handles) > 1 and len(value) == len(handles) else [value]
                for handle, piece in zip(handles, pieces, strict=False):
                    await self._type_secret(screen, handle, piece)
                    last = handle
            if last is None:
                raise ComputerError("there was nothing to fill on this page")
            if submit:
                _check_site(screen.page.url, expect_host)
                await asyncio.sleep(random.uniform(0.2, 0.5))  # noqa: S311
                await last.press("Enter")
            screen.last_action = "fill"
            screen.last_active = time.time()
            await _settle(screen.page)
        return await self.observe(screen)

    async def _type_secret(self, screen: Screen, handle: Any, value: str) -> None:
        x, y = await self._move_to(screen, handle)
        await screen.page.mouse.click(x, y)
        await handle.evaluate(
            "el => { el.value = ''; el.dispatchEvent(new Event('input', {bubbles: true})); "
            "el.setAttribute('data-vault-filled', '1'); }"
        )
        try:
            await screen.page.keyboard.type(value, delay=random.randint(25, 70))  # noqa: S311
        except PlaywrightError as exc:
            # Never the exception text: it is about the keystrokes.
            raise ComputerError("typing into the form failed") from exc

    # --- human takeover --------------------------------------------------------------

    async def human_input(self, screen: Screen, event: dict[str, Any]) -> None:
        """Input from a person who holds the screen. Coordinates are viewport pixels."""
        if screen.controller != "human":
            raise ComputerError("take control of the screen first")
        page = screen.page
        kind = event.get("kind")
        recording = screen.recording
        step: dict[str, Any] | None = None
        async with screen.lock:
            if kind == "click":
                x, y = float(event["x"]), float(event["y"])
                if recording is not None:
                    step = {
                        "kind": "click",
                        "target": await self._describe(screen, x, y, focused=False),
                    }
                await page.mouse.click(x, y)
                screen.mouse = (x, y)
            elif kind == "type":
                typed = str(event.get("text", ""))
                if recording is not None:
                    target = await self._describe(screen, 0, 0, focused=True) or {}
                    step = {
                        "kind": "type",
                        "target": target,
                        "text": "•••" if target.get("secret") else typed[:400],
                    }
                await page.keyboard.type(typed, delay=20)
            elif kind == "key":
                await page.keyboard.press(str(event["key"]))
                step = {"kind": "key", "key": str(event["key"])[:40]}
            elif kind == "scroll":
                await page.mouse.wheel(0, float(event.get("dy", 400)))
                step = {"kind": "scroll", "dy": float(event.get("dy", 400))}
            elif kind == "navigate":
                await page.goto(_normalise_url(str(event["url"])))
                step = {"kind": "navigate"}
            elif kind == "back":
                await page.go_back()
                step = {"kind": "back"}
            elif kind == "forward":
                await page.go_forward()
                step = {"kind": "forward"}
            elif kind == "reload":
                await page.reload()
                step = {"kind": "reload"}
            else:
                raise ComputerError(f"unknown input {kind!r}")
            screen.last_active = time.time()
            if recording is not None and step is not None:
                # The page the step was made on — for a click that navigated, the click's
                # own page is the one before, which the previous step already says.
                step["url"] = _shown_url(page.url)
                recording.add(step)


# --- action implementations -----------------------------------------------------------


async def _navigate(c: Computer, s: Screen, a: dict[str, Any]) -> None:
    url = _normalise_url(str(a.get("url", "")))
    try:
        await s.page.goto(url, wait_until="domcontentloaded")
    except PlaywrightTimeout as exc:
        raise ComputerError(f"{url} did not load within {NAV_TIMEOUT_MS // 1000}s") from exc
    except PlaywrightError as exc:
        if "Download is starting" in exc.message:
            # The address is a file, not a page: the browser downloads it (into the
            # workspace, reported with the next observation) and stays where it was.
            return
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


def _site(host: str) -> str:
    """Mirrors `runtime.domain.vault.site_of` — the computer imports nothing from the
    runtime, so the one rule both sides must agree on is written twice and tested
    against each other."""
    host = (host or "").strip().lower().rstrip(".")
    return host[4:] if host.startswith("www.") else host


def _check_site(page_url: str, expect_host: str) -> None:
    from urllib.parse import urlparse

    try:
        actual = urlparse(page_url).hostname or ""
    except ValueError:
        actual = ""
    if not expect_host or _site(actual) != _site(expect_host):
        raise ComputerError(
            f"the page is on {actual or 'no site'}, not {expect_host}; nothing was filled"
        )
    if urlparse(page_url).scheme != "https" and _site(actual) not in ("localhost", "127.0.0.1"):
        raise ComputerError(f"{actual} is not using https; sign-in details are not sent to it")


_FILLABLE = frozenset({"", "text", "email", "tel", "password", "number"})


async def _check_fillable(handle: Any, number: int, *, password: bool) -> None:
    info = await handle.evaluate(
        "el => ({tag: el.tagName.toLowerCase(), type: (el.getAttribute('type') || '')"
        ".toLowerCase(), disabled: !!el.disabled, readonly: !!el.readOnly})"
    )
    if info["tag"] != "input" or info["type"] not in _FILLABLE:
        raise ComputerError(f"element [{number}] is not a text box")
    if info["disabled"] or info["readonly"]:
        raise ComputerError(f"element [{number}] cannot be typed into")
    if password and info["type"] != "password":
        raise ComputerError(
            f"element [{number}] is not a password box; a password only goes into one"
        )


def _shown_url(url: str) -> str:
    """A URL as a recording keeps it: no query string or fragment, which is where
    tokens, session ids and search terms live."""
    return url.split("#", 1)[0].split("?", 1)[0][:300]


async def _title(screen: Screen) -> str:
    try:
        return (await screen.page.title())[:120]
    except PlaywrightError:
        return ""


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
