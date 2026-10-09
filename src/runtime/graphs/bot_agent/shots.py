"""Screenshots for the person — the bot's screen, at the moments that need it.

A bot's chat says what it did; a picture shows it. The runtime takes one, masked, when
the person is asked to decide something on the strength of the page — an approval, a
sign-in form, a CAPTCHA — and when the bot chooses to illustrate a reply or a question
(`BotStep.screenshot`). A `look` keeps the screenshot it already took.

The image goes from the tool result straight to `BotService.save_screenshot` and the
message carries only its id: nothing here reaches state, the step log or a prompt.

A picture is a courtesy, never a reason to fail a turn: the computer being away is
answered with no picture. A kill switch or a ceiling still raises — those stop the
run whatever it was doing.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from typing import Any

from runtime.domain.errors import TransientFault
from runtime.gateway.tools import ToolCall
from runtime.graphs.common.state import whole


async def keep(
    node: Any, bot: Any, b64: str, *, n: int, kind: str, page_url: str = ""
) -> uuid.UUID | None:
    """Store a screenshot already in hand (a `look`'s)."""
    try:
        data = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        return None
    if not data:
        return None
    sid: uuid.UUID = await node.org.bots.save_screenshot(
        bot.id, run_id=node.ctx.run_id, step=n, kind=kind, data=data, page_url=page_url
    )
    return sid


async def capture(node: Any, bot: Any, *, n: int, kind: str) -> uuid.UUID | None:
    """Take a masked screenshot of the bot's screen now and keep it. None if there is
    no picture to be had."""
    try:
        seen = await node.gateway.execute(
            node.ctx,
            ToolCall(
                tool="browser.observe@1",
                args={"screen_id": str(bot.id), "label": bot.name, "screenshot": True},
            ),
        )
    except TransientFault:
        return None
    value = await whole(seen.value, node.artifacts)
    b64 = str(value.get("screenshot") or "")
    if not value.get("ok", True) or not b64:
        return None
    return await keep(node, bot, b64, n=n, kind=kind, page_url=str(value.get("url", "")))
