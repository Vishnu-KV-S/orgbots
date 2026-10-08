"""`browser.observe@1` and `browser.act@1` — a bot's hands and eyes.

Both are HTTP calls to the computer (`runtime.computer`), which is an external system
to the runtime exactly the way a search API is: it holds no database connection and
cannot see a fence. So everything that governs a bot's browsing happens here, in the
gateway, before the request leaves — permission, kill switch, ceilings, budget, the
effect journal, scrubbing and audit — and a person stopping a bot with the kill switch
stops its next click.

**Two tools, because they have different consequences.**

`observe` reads the page. READ, `replay_safe`: re-reading after a crash costs a
request and nothing else.

`act` clicks, types and navigates, and is **REVERSIBLE / `manual`** — honestly so. A
click can submit a form, and no browser offers an idempotency key or a marker to
probe, so a replay after a crash mid-click cannot know whether the click landed. The
journal therefore leaves an INTENT row for a person to look at rather than clicking
twice. It is not IRREVERSIBLE because most clicks are not, and the per-action gate
for the ones that are lives one level up, in the bot's approval rules
(`runtime.domain.bots.needs_approval`), where the person can say "always allow" for a
site — a decision the blast-radius floor cannot express.

The screen id is the bot's id, passed by the graph. The computer refuses an action
with 409 while a person holds the screen; that surfaces here as a failed result the
bot reads, not as an exception that fails the run.
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel, Field

from runtime.domain.enums import BlastRadius, RecoveryPolicy
from runtime.domain.errors import TransientFault
from runtime.gateway.tools import EffectCapabilities, ToolContext, ToolDef, ToolRegistry
from runtime.settings import Settings

TIMEOUT_S = 60.0


class ObserveArgs(BaseModel):
    screen_id: str = Field(min_length=1, max_length=64)
    label: str = Field(default="", max_length=120)


class ActArgs(BaseModel):
    screen_id: str = Field(min_length=1, max_length=64)
    action: dict[str, Any]
    label: str = Field(default="", max_length=120)


class BrowserResult(BaseModel):
    ok: bool
    error: str | None = None
    url: str = ""
    title: str = ""
    controller: str = "bot"
    rendered: str = ""
    """The page as the model reads it — see `runtime.computer.snapshot.render`."""
    elements: list[dict[str, Any]] = Field(default_factory=list)


def _result(body: dict[str, Any], *, ok: bool = True, error: str | None = None) -> BrowserResult:
    snap = body.get("snapshot") or {}
    return BrowserResult(
        ok=bool(body.get("ok", ok)),
        error=body.get("error", error),
        url=str(body.get("url", "")),
        title=str(body.get("title", "")),
        controller=str(body.get("controller", "bot")),
        rendered=str(body.get("rendered", "")),
        elements=list(snap.get("elements", [])),
    )


def build(settings: Settings) -> tuple[tuple[ToolDef, Any], tuple[ToolDef, Any]]:
    base = settings.computer_url.rstrip("/")

    async def _post(path: str, payload: dict[str, Any]) -> httpx.Response:
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
                return await client.post(f"{base}{path}", json=payload)
        except httpx.TransportError as exc:
            raise TransientFault(
                f"the computer at {base} is not reachable ({type(exc).__name__}); "
                "start it with `python -m runtime.computer.main`"
            ) from exc

    async def observe(ctx: ToolContext, args: Any) -> BrowserResult:
        _ = ctx
        typed: ObserveArgs = args
        response = await _post(f"/screens/{typed.screen_id}/observe", {"label": typed.label})
        if response.status_code >= 400:
            return BrowserResult(ok=False, error=_detail(response))
        return _result(response.json())

    async def act(ctx: ToolContext, args: Any) -> BrowserResult:
        _ = ctx
        typed: ActArgs = args
        response = await _post(
            f"/screens/{typed.screen_id}/act", {"action": typed.action, "label": typed.label}
        )
        if response.status_code == 409:
            return BrowserResult(ok=False, error=_detail(response), controller="human")
        if response.status_code >= 400:
            return BrowserResult(ok=False, error=_detail(response))
        return _result(response.json())

    observe_def = ToolDef(
        name="browser.observe",
        version=1,
        args_model=ObserveArgs,
        result_model=BrowserResult,
        capabilities=EffectCapabilities(
            mutates_external_state=False, max_blast_radius=BlastRadius.READ
        ),
        recovery_policy=RecoveryPolicy.REPLAY_SAFE,
        timeout_s=TIMEOUT_S + 5,
    )
    act_def = ToolDef(
        name="browser.act",
        version=1,
        args_model=ActArgs,
        result_model=BrowserResult,
        capabilities=EffectCapabilities(
            mutates_external_state=True, max_blast_radius=BlastRadius.REVERSIBLE
        ),
        recovery_policy=RecoveryPolicy.MANUAL,
        timeout_s=TIMEOUT_S + 5,
        # A retry after a timeout is a second click on a button whose first click may
        # have landed. The bot sees the failure and observes instead.
        max_retries=0,
    )
    return (observe_def, observe), (act_def, act)


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return f"computer answered {response.status_code}"
    detail = body.get("detail") if isinstance(body, dict) else None
    return str(detail or f"computer answered {response.status_code}")


def register(registry: ToolRegistry, settings: Settings) -> None:
    for definition, fn in build(settings):
        registry.register(definition, fn)
