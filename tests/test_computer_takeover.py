"""A person at a bot's screen, against a real browser.

Taking control has to feel like using the computer, because the pages a person takes
control for — a CAPTCHA, a slider, a 2FA prompt — look at how they are being used. So
the page must see what a hand does: the pointer travelling before it presses, a press
and a release that are two moments apart, drags, double and right clicks, keys held
down with modifiers, and the person's own clipboard. And the person must see the page
as it moves, not a still every second.

Skips without a Chrome Playwright can launch (`COMPUTER_CHROMIUM_PATH`).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from runtime.computer.browser import Computer, ComputerError, Screen


def _chromium() -> bool:
    if os.environ.get("COMPUTER_CHROMIUM_PATH"):
        return True
    cache = os.path.expanduser("~/.cache/ms-playwright")
    return os.path.isdir(cache) and any(d.startswith("chromium") for d in os.listdir(cache))


pytestmark = pytest.mark.skipif(not _chromium(), reason="no Chrome for Playwright")

PAGE = """
<body style="margin:0;background:#fff">
<input id=box style="position:absolute;left:100px;top:100px;width:300px;height:40px">
<div id=drag style="position:absolute;left:100px;top:300px;width:80px;height:80px;
  background:#c00"></div>
<script>
window.log = [];
for (const type of ['mousemove', 'mousedown', 'mouseup', 'click', 'dblclick',
                    'contextmenu', 'keydown', 'keyup', 'wheel']) {
  document.addEventListener(type, (e) => log.push({
    type, x: e.clientX, y: e.clientY, button: e.button, key: e.key, shift: e.shiftKey,
    ctrl: e.ctrlKey, trusted: e.isTrusted, t: performance.now(),
  }), true);
}
document.addEventListener('contextmenu', (e) => e.preventDefault());
let grab = null;
drag.addEventListener('mousedown', (e) => {
  grab = {dx: e.clientX - drag.offsetLeft, dy: e.clientY - drag.offsetTop};
});
document.addEventListener('mousemove', (e) => {
  if (grab) {
    drag.style.left = (e.clientX - grab.dx) + 'px';
    drag.style.top = (e.clientY - grab.dy) + 'px';
  }
});
document.addEventListener('mouseup', () => { grab = null; });
</script>
"""


@pytest.fixture
async def held(tmp_path: Path) -> AsyncIterator[tuple[Computer, Screen]]:
    """A screen with the test page on it, in the person's hands."""
    computer = Computer(tmp_path / "profile", headless=True)
    try:
        screen = await computer.screen("person")
        await screen.page.set_content(PAGE)
        await computer.set_controller(screen, "human")
        yield computer, screen
    finally:
        await computer.stop()


async def _log(screen: Screen, *types: str) -> list[dict[str, Any]]:
    log: list[dict[str, Any]] = await screen.page.evaluate("window.log")
    return [e for e in log if not types or e["type"] in types]


def _path(
    x0: float, y0: float, x1: float, y1: float, steps: int, t0: float, dt: float
) -> list[dict[str, Any]]:
    return [
        {
            "kind": "move",
            "x": x0 + (x1 - x0) * i / steps,
            "y": y0 + (y1 - y0) * i / steps,
            "t": t0 + i * dt,
        }
        for i in range(1, steps + 1)
    ]


async def test_the_pointer_travels_then_presses_and_releases_at_the_persons_pace(
    held: tuple[Computer, Screen],
) -> None:
    computer, screen = held
    events = [
        *_path(600, 600, 250, 120, 12, t0=1_000, dt=20),
        {"kind": "down", "x": 250, "y": 120, "button": "left", "clicks": 1, "t": 1_250},
        {"kind": "up", "x": 250, "y": 120, "button": "left", "clicks": 1, "t": 1_350},
    ]
    started = time.monotonic()
    await computer.human_inputs(screen, events)
    took = time.monotonic() - started

    log = await _log(screen, "mousemove", "mousedown", "mouseup", "click")
    kinds = [e["type"] for e in log]
    assert kinds.count("mousemove") >= 10, "the pointer must travel, not teleport"
    assert kinds[-3:] == ["mousedown", "mouseup", "click"]
    down, up = log[-3], log[-2]
    assert (down["x"], down["y"]) == (250, 120)
    # The press and release are as far apart as the person made them.
    assert up["t"] - down["t"] >= 80
    assert took >= 0.3, took
    assert all(e["trusted"] for e in log)
    assert await screen.page.evaluate("document.activeElement.id") == "box"


async def test_a_drag_holds_the_button_down_across_the_moves(
    held: tuple[Computer, Screen],
) -> None:
    computer, screen = held
    await computer.human_inputs(
        screen,
        [
            {"kind": "move", "x": 140, "y": 340},
            {"kind": "down", "x": 140, "y": 340, "button": "left"},
            *_path(140, 340, 440, 440, 10, t0=0, dt=10),
            {"kind": "up", "x": 440, "y": 440, "button": "left"},
        ],
    )
    left, top = await screen.page.evaluate("[drag.offsetLeft, drag.offsetTop]")
    assert (left, top) == (400, 400)


async def test_double_and_right_clicks_are_what_the_page_gets(
    held: tuple[Computer, Screen],
) -> None:
    computer, screen = held
    at = {"x": 700, "y": 500}
    await computer.human_inputs(
        screen,
        [
            {"kind": "down", **at, "button": "left", "clicks": 1},
            {"kind": "up", **at, "button": "left", "clicks": 1},
            {"kind": "down", **at, "button": "left", "clicks": 2},
            {"kind": "up", **at, "button": "left", "clicks": 2},
            {"kind": "down", **at, "button": "right", "clicks": 1},
            {"kind": "up", **at, "button": "right", "clicks": 1},
        ],
    )
    assert len(await _log(screen, "dblclick")) == 1
    (menu,) = await _log(screen, "contextmenu")
    assert menu["button"] == 2


async def test_keys_go_down_and_up_with_modifiers_held(
    held: tuple[Computer, Screen],
) -> None:
    computer, screen = held

    def key(k: str) -> list[dict[str, Any]]:
        return [{"kind": "keydown", "key": k}, {"kind": "keyup", "key": k}]

    await computer.human_inputs(
        screen,
        [
            {"kind": "down", "x": 200, "y": 120},
            {"kind": "up", "x": 200, "y": 120},
            {"kind": "keydown", "key": "Shift"},
            *key("H"),
            {"kind": "keyup", "key": "Shift"},
            *key("i"),
            *key(" "),
            *key("é"),
        ],
    )
    assert await screen.page.input_value("#box") == "Hi é"
    (h,) = [e for e in await _log(screen, "keydown") if e["key"] == "H"]
    assert h["shift"] is True

    # Select all, then type over it: a shortcut is a held Control, as on a keyboard.
    await computer.human_inputs(
        screen,
        [
            {"kind": "keydown", "key": "Control"},
            *key("a"),
            {"kind": "keyup", "key": "Control"},
            *key("x"),
        ],
    )
    assert await screen.page.input_value("#box") == "x"

    await computer.human_inputs(screen, [{"kind": "paste", "text": " from the clipboard"}])
    assert await screen.page.input_value("#box") == "x from the clipboard"


async def test_a_burst_faster_than_frames_is_folded_and_ends_where_the_hand_did(
    held: tuple[Computer, Screen],
) -> None:
    computer, screen = held
    await screen.page.evaluate("document.body.style.height = '4000px'")
    burst = _path(0, 0, 600, 300, 60, t0=10_000, dt=2)
    wheel = [
        {"kind": "wheel", "x": 600, "y": 300, "dx": 0, "dy": 10, "t": 10_120 + i * 2}
        for i in range(30)
    ]
    started = time.monotonic()
    await computer.human_inputs(screen, [*burst, *wheel])
    took = time.monotonic() - started

    moves = await _log(screen, "mousemove")
    # 60 moves in 120ms is a frame's worth or so of motion each 16ms, not 60 frames.
    assert len(moves) <= 15, len(moves)
    assert (moves[-1]["x"], moves[-1]["y"]) == (600, 300)
    assert len(await _log(screen, "wheel")) <= 6
    await screen.page.wait_for_function("window.scrollY >= 300")
    assert took < 0.6, took


async def test_the_wheel_scrolls_where_the_pointer_is(held: tuple[Computer, Screen]) -> None:
    computer, screen = held
    await screen.page.evaluate("document.body.style.height = '4000px'")
    await computer.human_inputs(screen, [{"kind": "wheel", "x": 640, "y": 400, "dx": 0, "dy": 300}])
    await screen.page.wait_for_function("window.scrollY >= 300")
    (wheel,) = await _log(screen, "wheel")
    assert (wheel["x"], wheel["y"]) == (640, 400)


async def test_handing_back_lets_go_of_whatever_the_person_held(
    held: tuple[Computer, Screen],
) -> None:
    computer, screen = held
    await computer.human_inputs(
        screen,
        [
            {"kind": "down", "x": 200, "y": 120},
            {"kind": "up", "x": 200, "y": 120},
            {"kind": "keydown", "key": "Shift"},
            {"kind": "down", "x": 140, "y": 340, "button": "left"},
        ],
    )
    await computer.set_controller(screen, "bot")

    assert [e["key"] for e in await _log(screen, "keyup")] == ["Shift"]
    assert (await _log(screen, "mouseup"))[-1]["button"] == 0
    # A bot typing now types what it means, not capitals.
    await screen.page.click("#box")
    await screen.page.keyboard.type("ok")
    assert await screen.page.input_value("#box") == "ok"

    with pytest.raises(ComputerError, match="take control"):
        await computer.human_inputs(screen, [{"kind": "move", "x": 1, "y": 1}])


async def test_late_input_is_caught_up_and_a_jump_in_the_clock_is_not_waited_out(
    held: tuple[Computer, Screen],
) -> None:
    computer, screen = held
    await computer.human_inputs(screen, [{"kind": "move", "x": 10, "y": 10, "t": 5_000}])
    await asyncio.sleep(0.4)
    # Made 300ms after the first, arriving 400ms after it: already late, so at once.
    started = time.monotonic()
    await computer.human_inputs(screen, [{"kind": "move", "x": 20, "y": 20, "t": 5_300}])
    assert time.monotonic() - started < 0.15
    # Ten minutes on the person's clock but moments on ours: not a ten-minute wait.
    started = time.monotonic()
    await computer.human_inputs(screen, [{"kind": "move", "x": 30, "y": 30, "t": 605_300}])
    assert time.monotonic() - started < 0.15


async def test_the_live_view_sends_a_frame_at_once_and_again_when_the_page_changes(
    held: tuple[Computer, Screen],
) -> None:
    computer, screen = held
    frames = computer.frames(screen)
    try:
        first = await asyncio.wait_for(anext(frames), 5)
        assert first[:2] == b"\xff\xd8", "a JPEG"
        await screen.page.evaluate("document.body.style.background = '#06c'")
        changed = first
        deadline = time.monotonic() + 5
        while changed == first and time.monotonic() < deadline:
            changed = await asyncio.wait_for(anext(frames), 5)
        assert changed != first, "a repaint must reach the watcher"
        assert screen.cast is not None and screen.cast.viewers == 1
    finally:
        await frames.aclose()
    assert screen.cast.viewers == 0, "the screencast stops when nobody watches"


# --- over HTTP -------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def test_the_computer_streams_mjpeg_and_takes_batches_only_from_the_holder(
    tmp_path: Path,
) -> None:
    import uvicorn

    from runtime.computer.app import create_app

    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(tmp_path / "profile"), host="127.0.0.1", port=port, log_level="warning"
        )
    )
    serving = asyncio.create_task(server.serve())
    base = f"http://127.0.0.1:{port}"
    try:
        async with httpx.AsyncClient(base_url=base, timeout=20) as client:
            for _ in range(100):
                with contextlib.suppress(httpx.TransportError):
                    if (await client.get("/healthz")).status_code == 200:
                        break
                await asyncio.sleep(0.1)

            async with client.stream("GET", "/screens/s/stream") as response:
                assert response.headers["content-type"].startswith("multipart/x-mixed-replace")
                buffer = b""
                async for chunk in response.aiter_bytes():
                    buffer += chunk
                    head, _, rest = buffer.partition(b"\r\n\r\n")
                    if rest:
                        lines = head.decode().split("\r\n")
                        length = int(
                            next(
                                v
                                for k, _, v in (h.partition(": ") for h in lines)
                                if k == "Content-Length"
                            )
                        )
                        if len(rest) >= length:
                            break
                assert lines[0] == "--frame"
                assert rest[:2] == b"\xff\xd8"

            move = {"events": [{"kind": "move", "x": 5, "y": 5, "t": 1.0}]}
            refused = await client.post("/screens/s/inputs", json=move)
            assert refused.status_code == 409
            assert (
                await client.post("/screens/s/control", json={"controller": "human"})
            ).status_code == 200
            done = await client.post("/screens/s/inputs", json=move)
            assert done.status_code == 200, done.text
            assert done.json()["ok"] is True
    finally:
        server.should_exit = True
        await serving
