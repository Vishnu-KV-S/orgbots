"""`look` — a bot asks a vision model about its own screen.

    observe (with a masked screenshot) → one PERCEPTION model call → the answer as text

The screenshot comes through `browser.observe@1` like every other read of the screen,
so it is governed, journalled and kill-switched the same way; being large, it arrives
as an artifact reference and is loaded here, inside the node, with `whole()`. It is
never put into state or the step log — an image in state would be re-serialised into
every checkpoint after it.

The answer is the bot's to use: it goes into the turn's `answers`, beside what helpers
and recall return, and the step log records only that the bot looked.

**Not `call_structured`.** That helper refuses every work class outside the M1
vocabulary, on purpose, and `PERCEPTION` is outside it. A look is a single call — the
schema rides in the system prompt, since DeepSeek does not enforce one server-side —
so it calls the model gateway directly and checks the answer against `BotLook@1`
itself, tolerating a fenced block the way `call_structured` does.

**When vision is not available** — an actor published before the `PERCEPTION` profile
existed, a model that does not read images, no credential — the bot is told so and
carries on with the text view. A missing *capability* is not a reason to fail a turn;
a kill switch, a ceiling or the budget still is, and those still raise.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from runtime.domain.bots import LOG_KEEP
from runtime.domain.enums import WorkClass
from runtime.domain.errors import (
    MissingCredentials,
    ModelCallNotAllowed,
    ProviderUnavailable,
    SpecError,
)
from runtime.domain.schemas import SCHEMAS
from runtime.domain.vision import BOT_LOOK, LOOK_SYSTEM, LookResult, look_prompt
from runtime.gateway.models import ImageInput, ModelRequest
from runtime.gateway.tools import ToolCall
from runtime.graphs.bot_agent.shots import keep
from runtime.graphs.common.state import whole
from runtime.graphs.common.structured import _as_object

LOOK_MAX_OUTPUT_TOKENS = 4_000

CAPTCHA_HANDOFF = (
    "This page wants a CAPTCHA, which I don't solve. Take control of my screen "
    "(🖥 Computer), complete it, hand the screen back and tell me to continue."
)
"""Said when a look sees a CAPTCHA, and when the page listing names one (`graph`)."""

UNAVAILABLE = (
    "look is not available (vision is not set up: {reason}). Work from the page "
    "listing, or ask the person."
)


async def look(
    node: Any,
    bot: Any,
    question: str,
    *,
    thought: str,
    n: int,
    steps: list[str],
    answers: list[str],
    line: Any,
    say: Any,
    end: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """One `look`. Answers and continues, or ends the turn on a CAPTCHA."""
    seen = await node.gateway.execute(
        node.ctx,
        ToolCall(
            tool="browser.observe@1",
            args={"screen_id": str(bot.id), "label": bot.name, "screenshot": True},
        ),
    )
    page = await whole(seen.value, node.artifacts)
    shot = str(page.get("screenshot") or "")
    action = {"type": "look", "text": question}

    async def failed(reason: str) -> dict[str, Any]:
        await say("look", "activity", thought, {"action": action, "ok": False, "error": reason})
        steps.append(line(n, f"look failed: {reason}"))
        return {"n": n + 1, "log": steps[-LOG_KEEP:], "answers": answers[-3:], "done": False}

    if not page.get("ok", True) or not shot:
        return await failed(str(page.get("error") or "the computer returned no screenshot"))
    # The same picture goes to the chat, so the person sees what the bot asked about.
    sid = await keep(node, bot, shot, n=n, kind="look", page_url=str(page.get("url", "")))
    pictured = {"screenshot_id": str(sid)} if sid else {}

    schema = SCHEMAS.get(BOT_LOOK)
    try:
        response = await node.models.complete(
            node.ctx,
            ModelRequest(
                prompt=look_prompt(question, str(page.get("rendered", ""))),
                system=LOOK_SYSTEM,
                images=(ImageInput(media_type="image/jpeg", data=shot),),
                max_output_tokens=LOOK_MAX_OUTPUT_TOKENS,
                metadata={"json_schema": schema.json_schema, "schema_ref": BOT_LOOK},
            ),
            work_class=WorkClass.PERCEPTION,
            call_site="bot_agent.look",
        )
    except (ModelCallNotAllowed, ProviderUnavailable, MissingCredentials, SpecError) as exc:
        return await failed(UNAVAILABLE.format(reason=str(exc).splitlines()[0][:200]))

    payload = _as_object(response.text)
    if payload is None or schema.check(payload):
        return await failed("the vision model's answer could not be read")
    result = LookResult.model_validate(payload)

    if result.captcha:
        # Reported, never solved: a person takes the screen. Said as a system line so
        # it reads as the runtime's request, not the bot's opinion.
        await say(
            "look",
            "activity",
            thought,
            {"action": action, "ok": True, "note": "The screen shows a CAPTCHA.", **pictured},
        )
        await say("captcha", "system", CAPTCHA_HANDOFF, {"captcha": True, **pictured})
        await node.org.bots.end_turn(bot.id, needs_attention=True)
        return end({"status": "human_needed", "steps": n, "reason": "captcha"})

    where = f" (elements {', '.join(f'[{e}]' for e in result.elements)})" if result.elements else ""
    await say(
        "look",
        "activity",
        thought,
        {
            "action": action,
            "ok": True,
            "note": result.answer,
            "elements": result.elements,
            **pictured,
        },
    )
    answers.append(f"You looked at the screen and asked: {question[:200]}\n{result.answer}{where}")
    steps.append(line(n, f"looked at the screen: {question[:100]}"))
    return {"n": n + 1, "log": steps[-LOG_KEEP:], "answers": answers[-3:], "done": False}


GLANCE_QUESTION = (
    "Describe this screen for an agent deciding its next step. Say: what app or page it "
    "is; what is open on top (a menu, submenu, dialog or popup — list its items and which "
    "is selected or ticked); the main controls and their state (toggles on or off, the "
    "options chosen); any progress indicator, error or message; and anything that is "
    "waiting for the user to answer it. Give the [numbers] from the listing for the "
    "controls you mention, and say which visible controls have no number."
)


async def glance(node: Any, bot: Any) -> str | None:
    """The screen, described by the vision model, for the step about to be decided.

    The model that decides reads text only (`deepseek-v4-pro` takes no images), so on
    its own it never sees what the listing leaves out: a menu row with no number, a
    canvas, a toggle drawn as a picture, a progress bar. A stuck step gets this before it
    decides (`graph`), unasked — a look the bot did not have to think of. Quiet: nothing
    is said in the chat, and no step is spent. None when vision is not available, or
    the answer could not be read; the step then decides from the listing alone.
    """
    try:
        seen = await node.gateway.execute(
            node.ctx,
            ToolCall(
                tool="browser.observe@1",
                args={"screen_id": str(bot.id), "label": bot.name, "screenshot": True},
            ),
        )
        page = await whole(seen.value, node.artifacts)
        shot = str(page.get("screenshot") or "")
        if not page.get("ok", True) or not shot:
            return None
        schema = SCHEMAS.get(BOT_LOOK)
        response = await node.models.complete(
            node.ctx,
            ModelRequest(
                prompt=look_prompt(GLANCE_QUESTION, str(page.get("rendered", ""))),
                system=LOOK_SYSTEM,
                images=(ImageInput(media_type="image/jpeg", data=shot),),
                max_output_tokens=LOOK_MAX_OUTPUT_TOKENS,
                metadata={"json_schema": schema.json_schema, "schema_ref": BOT_LOOK},
            ),
            work_class=WorkClass.PERCEPTION,
            call_site="bot_agent.glance",
        )
    except (ModelCallNotAllowed, ProviderUnavailable, MissingCredentials, SpecError):
        return None
    payload = _as_object(response.text)
    if payload is None or schema.check(payload):
        return None
    result = LookResult.model_validate(payload)
    if result.captcha:
        return "The screen shows a CAPTCHA or human-verification check: your person answers it."
    return result.answer
