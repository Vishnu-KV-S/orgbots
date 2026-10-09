"""Auto Review in a turn: one model call that allows, holds or refuses a risky step.

    review_prompt(request, plan, thought, action) → SUMMARIZATION model → BotReview@1

The call goes through the model gateway like every other — budgeted, metered, refused
by the kill switch — on the bot's cheap profile, because it is a short judgement made
on a handful of steps a turn. Every verdict is written into the conversation as an
activity line with its reason, so the person can see what was checked and why it went
the way it did.

**When the reviewer cannot answer** — vision-style capability gaps, no credential, an
unreadable answer — the step is not waved through: the person switched Auto Review on
to have a second check, and a check that did not happen is reported as "ask", so the
step waits for them instead.
"""

from __future__ import annotations

from typing import Any

from runtime.domain.enums import WorkClass
from runtime.domain.errors import (
    MissingCredentials,
    ModelCallNotAllowed,
    ProviderUnavailable,
    SpecError,
)
from runtime.domain.review import BOT_REVIEW, REVIEW_SYSTEM, ReviewResult, review_prompt
from runtime.domain.schemas import SCHEMAS
from runtime.gateway.models import ModelRequest
from runtime.graphs.common.structured import _as_object

REVIEW_MAX_OUTPUT_TOKENS = 400


def wants_review(bot: Any) -> bool:
    return bool(getattr(bot, "auto_review", False))


async def auto_review(
    node: Any,
    *,
    kind: str,
    action: str,
    thought: str,
    request: str,
    plan: list[str],
    steps: list[str],
    say: Any,
    where: str = "",
    context: str = "",
) -> ReviewResult:
    schema = SCHEMAS.get(BOT_REVIEW)
    try:
        response = await node.models.complete(
            node.ctx,
            ModelRequest(
                prompt=review_prompt(
                    request=request,
                    plan=plan,
                    thought=thought,
                    action=action,
                    kind=kind,
                    where=where,
                    recent=steps,
                    context=context,
                ),
                system=REVIEW_SYSTEM,
                max_output_tokens=REVIEW_MAX_OUTPUT_TOKENS,
                metadata={"json_schema": schema.json_schema, "schema_ref": BOT_REVIEW},
            ),
            work_class=WorkClass.SUMMARIZATION,
            call_site="bot_agent.review",
        )
        payload = _as_object(response.text)
        result = (
            ReviewResult.model_validate(payload)
            if payload is not None and not schema.check(payload)
            else ReviewResult(
                verdict="ask",
                reason="Auto Review's answer could not be read, so this waits for you.",
            )
        )
    except (ModelCallNotAllowed, ProviderUnavailable, MissingCredentials, SpecError) as exc:
        result = ReviewResult(
            verdict="ask",
            reason=f"Auto Review could not run ({str(exc).splitlines()[0][:160]}), so this "
            "waits for you.",
        )
    await say(
        "review",
        "activity",
        result.reason,
        {
            "action": {"type": "review", "verdict": result.verdict, "text": action[:300]},
            "ok": result.verdict != "deny",
            "kind": kind,
        },
    )
    return result
