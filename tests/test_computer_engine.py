"""Where the computer's browser comes from (`runtime.computer.engine`).

The bots' computer runs a real Google Chrome on a Linux desktop (`docker/computer`): it
must be started with none of the switches a test harness adds, give every screen its own
window so each bot's page is visible and keeps painting, and size screens by their
windows rather than by a fixed viewport. These run that engine on this machine
(headless, since there is no desktop here) against the system Chrome, and skip without
one. `test_the_container_*` run against the container itself, when it is up.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import httpx
import pytest

from runtime.computer.browser import Computer
from runtime.computer.engine import (
    DesktopChrome,
    PlaywrightChromium,
    _clear_stale_locks,
    engine_from_env,
)

CHROME = os.environ.get("COMPUTER_CHROME_PATH") or shutil.which("google-chrome")
needs_chrome = pytest.mark.skipif(not CHROME, reason="no Google Chrome on this machine")

HARNESS_SWITCHES = (
    "--enable-automation",
    "--disable-extensions",
    "--disable-component-update",
    "--disable-default-apps",
    "--hide-scrollbars",
    "--mute-audio",
    "--force-color-profile",
)
"""Switches Playwright's own launch adds, every one of which a page can see the effect of."""


def test_the_engine_is_chosen_by_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("COMPUTER_BROWSER", raising=False)
    assert isinstance(engine_from_env(), PlaywrightChromium)

    monkeypatch.setenv("COMPUTER_BROWSER", "desktop")
    monkeypatch.setenv("DISPLAY", ":7")
    monkeypatch.setenv("COMPUTER_CHROME_FLAGS", "--no-sandbox --lang=en-GB")
    desktop = engine_from_env()
    assert isinstance(desktop, DesktopChrome)
    assert desktop.describe() == {"engine": "desktop", "display": ":7"}
    assert desktop._flags == ("--no-sandbox", "--lang=en-GB")

    monkeypatch.setenv("COMPUTER_BROWSER", "firefox")
    with pytest.raises(ValueError, match="desktop"):
        engine_from_env()


def test_a_stale_profile_lock_is_cleared(tmp_path: Path) -> None:
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        (tmp_path / name).symlink_to("computer-12345")
    (tmp_path / "Cookies").write_text("kept")
    _clear_stale_locks(tmp_path)
    assert [p.name for p in tmp_path.iterdir()] == ["Cookies"]


async def _window(page: Any) -> int:
    session = await page.context.new_cdp_session(page)
    try:
        return int((await session.send("Browser.getWindowForTarget"))["windowId"])
    finally:
        await session.detach()


def _browser_argv(profile_dir: Path) -> list[str]:
    """The command line of the Chrome browser process using `profile_dir`, read from
    /proc: Chrome answers `Browser.getBrowserCommandLine` only when `--enable-automation`
    is set, which is the point. Chrome rewrites its own process title, so the arguments
    arrive as one string; the test's paths have no spaces in them."""
    wanted = f"--user-data-dir={profile_dir}"
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            argv = (proc / "cmdline").read_text().replace("\0", " ").split()
        except OSError:
            continue
        if wanted in argv and not any(a.startswith("--type=") for a in argv):
            return argv[1:]
    raise AssertionError(f"no Chrome is running on {profile_dir}")


@needs_chrome
async def test_desktop_chrome_starts_clean_and_gives_each_screen_its_own_window(
    tmp_path: Path,
) -> None:
    assert CHROME is not None
    computer = Computer(
        tmp_path / "profile",
        engine=DesktopChrome(executable=CHROME, flags=("--headless=new",)),
    )
    try:
        a = await computer.screen("a")
        b = await computer.screen("b")
        assert await _window(a.page) != await _window(b.page)

        # The screen is as big as its window, and the live view is sent at that size.
        width, height = await a.page.evaluate("[innerWidth, innerHeight]")
        assert a.size == (width, height)
        frames = computer.frames(a)
        try:
            jpeg = await anext(frames)
        finally:
            await frames.aclose()
        assert jpeg[:2] == b"\xff\xd8"

        argv = _browser_argv(tmp_path / "profile")
        for switch in HARNESS_SWITCHES:
            assert not any(arg.startswith(switch) for arg in argv), switch
        assert f"--user-data-dir={tmp_path / 'profile'}" in argv

        # A browser that goes away is started again for the next screen asked for.
        await a.page.context.close()
        again = await computer.screen("a")
        assert not again.page.is_closed()
    finally:
        await computer.stop()


# --- the container ----------------------------------------------------------------------

CONTAINER = os.environ.get("COMPUTER_CONTAINER_URL", "")
needs_container = pytest.mark.skipif(
    not CONTAINER, reason="set COMPUTER_CONTAINER_URL to the running computer container"
)


@needs_container
async def test_the_container_runs_chrome_on_its_desktop() -> None:
    async with httpx.AsyncClient(base_url=CONTAINER, timeout=60) as client:
        health = (await client.get("/healthz")).json()
        assert health["engine"] == "desktop"
        assert health["desktop_url"]
        view = (
            await client.post(
                "/screens/engine-test/act",
                json={"action": {"type": "navigate", "url": "https://example.com/"}},
            )
        ).json()
        assert view["ok"], view
        await client.delete("/screens/engine-test")


@needs_chrome
async def test_opening_a_bots_computer_brings_its_window_to_the_front(tmp_path: Path) -> None:
    assert CHROME is not None
    from runtime.computer.app import create_app

    app = create_app(
        tmp_path / "profile",
        workspace=tmp_path / "workspace",
        engine=DesktopChrome(executable=CHROME, flags=("--headless=new",)),
        desktop_url="http://127.0.0.1:6080/vnc.html",
        desktop_size="1440x900",
    )
    computer = app.state.computer
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://computer"
        ) as client:
            shown = await client.post("/screens/bot-a/front")
            assert shown.status_code == 200, shown.text
            assert shown.json() == {"screen_id": "bot-a", "url": "about:blank"}
            health = (await client.get("/healthz")).json()
        assert health["desktop_size"] == "1440x900"
        assert health["engine"] == "desktop"
        assert [s.screen_id for s in computer.screens()] == ["bot-a"]
    finally:
        await computer.stop()
