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

**`fill_credentials` is an `act`, and the only one that opens the vault.** Its
action names vault entries and the fields they go into — ids and element numbers, so
that is all the journal, the audit row and the run's checkpoint ever hold. The values
are opened here (`runtime.gateway.vault.Vault.open`, which also decides the site from
the entries themselves), sent to the computer's `/fill`, and dropped. It rides on
`browser.act@1` rather than being a third tool because it has exactly `act`'s
consequence — it types and may submit — and a new tool would mean republishing every
bot's actor to grant it.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from pydantic import BaseModel, Field

from runtime.domain.bots import MAX_UPLOAD_FILES
from runtime.domain.enums import BlastRadius, RecoveryPolicy
from runtime.domain.errors import TransientFault
from runtime.domain.files import FileError, name_of, team_of
from runtime.domain.policies import Policy
from runtime.domain.vault import PASSWORD_KINDS, lookup
from runtime.gateway.builtin.profiles import (
    allow_header,
    org_policy,
    profile_of,
    record_block,
    refusal,
    run_bot,
)
from runtime.gateway.tools import EffectCapabilities, ToolContext, ToolDef, ToolRegistry
from runtime.gateway.vault import Vault, VaultRefusedError, VaultUnavailableError
from runtime.org.files import TeamDrive
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.settings import Settings

TIMEOUT_S = 60.0
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
"""All the files of one upload together. A team drive file is at most 10 MB."""


class ObserveArgs(BaseModel):
    screen_id: str = Field(min_length=1, max_length=64)
    label: str = Field(default="", max_length=120)
    screenshot: bool = False
    """Also capture the viewport as a masked JPEG, for a bot's `look`. The result is
    then well over the gateway's inline limit, so it is externalised to the artifact
    store and journalled by reference like any large result — the screenshot is kept
    at rest exactly as the page text of every observation already is."""


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
    screenshot: str = Field(default="", repr=False)
    """Base64 JPEG of the viewport, masked, when one was asked for."""


class ScreenRefusedError(Exception):
    """A run asked for a screen that is not its own bot's."""


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
        screenshot=str(body.get("screenshot", "")),
    )


def build(
    settings: Settings, uow_factory: UnitOfWorkFactory | None = None
) -> tuple[tuple[ToolDef, Any], tuple[ToolDef, Any]]:
    base = settings.computer_url.rstrip("/")
    vault_holder: list[Vault] = []

    def vault() -> Vault:
        # Built on first use, so a worker with no encryption key runs every other
        # browser action and refuses a fill with the reason, rather than not starting.
        if not vault_holder:
            if uow_factory is None:
                raise VaultUnavailableError("this gateway was built without a vault")
            vault_holder.append(Vault.from_settings(uow_factory, settings))
        return vault_holder[0]

    async def profile(ctx: ToolContext | None, screen_id: str) -> str:
        """The run's bot's browser profile — and a bot drives only its own screen."""
        bot = await run_bot(uow_factory, ctx)
        if bot is not None and str(bot.id) != screen_id:
            raise ScreenRefusedError("a bot can only use its own screen")
        return profile_of(bot)

    fill = _filler(base, vault, profile)

    async def _post(
        path: str, payload: dict[str, Any], profile: str, policy: Policy
    ) -> httpx.Response:
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
                return await client.post(
                    f"{base}{path}",
                    json=payload,
                    params={"profile": profile},
                    headers=allow_header(policy),
                )
        except httpx.TransportError as exc:
            raise TransientFault(
                f"the computer at {base} is not reachable ({type(exc).__name__}); "
                "start it with `python -m runtime.computer.main`"
            ) from exc

    async def observe(ctx: ToolContext, args: Any) -> BrowserResult:
        typed: ObserveArgs = args
        try:
            where = await profile(ctx, typed.screen_id)
        except ScreenRefusedError as exc:
            return BrowserResult(ok=False, error=str(exc))
        response = await _post(
            f"/screens/{typed.screen_id}/observe",
            {"label": typed.label, "screenshot": typed.screenshot},
            where,
            await org_policy(uow_factory, ctx),
        )
        if response.status_code >= 400:
            return BrowserResult(ok=False, error=_detail(response))
        return _result(response.json())

    async def upload(ctx: ToolContext, typed: ActArgs) -> BrowserResult:
        """Files for the page: a team drive file's bytes (read here, from the run's own
        bot's drive), or a /workspace path the computer opens in the bot's profile.
        Only paths are in the tool's arguments — the bytes never reach the journal."""
        try:
            where = await profile(ctx, typed.screen_id)
        except ScreenRefusedError as exc:
            return BrowserResult(ok=False, error=str(exc))
        paths = [str(p).strip() for p in typed.action.get("paths") or [] if str(p).strip()]
        if not paths:
            return BrowserResult(ok=False, error="upload needs the file's path")
        bot = await run_bot(uow_factory, ctx)
        files: list[dict[str, str]] = []
        workspace: list[str] = []
        total = 0
        for path in paths[:MAX_UPLOAD_FILES]:
            if path == "/workspace" or path.startswith("/workspace/"):
                workspace.append(path)
                continue
            if bot is None or uow_factory is None:
                return BrowserResult(ok=False, error="this run has no team drive to upload from")
            try:
                row, data = await TeamDrive(uow_factory).blob_at(team_of(bot), path)
            except FileError as exc:
                return BrowserResult(ok=False, error=str(exc).splitlines()[0])
            total += len(data)
            if total > MAX_UPLOAD_BYTES:
                return BrowserResult(
                    ok=False,
                    error=f"those files are over {MAX_UPLOAD_BYTES // 1_048_576} MB together",
                )
            files.append({"name": name_of(row.path), "data": base64.b64encode(data).decode()})
        response = await _post(
            f"/screens/{typed.screen_id}/upload",
            {
                "element": typed.action.get("element"),
                "files": files,
                "workspace": workspace,
                "label": typed.label,
            },
            where,
            await org_policy(uow_factory, ctx),
        )
        files.clear()
        if response.status_code == 409:
            return BrowserResult(ok=False, error=_detail(response), controller="human")
        if response.status_code >= 400:
            return BrowserResult(ok=False, error=_detail(response))
        return _result(response.json())

    async def act(ctx: ToolContext, args: Any) -> BrowserResult:
        typed: ActArgs = args
        if typed.action.get("type") == "fill_credentials":
            return await fill(ctx, typed)
        if typed.action.get("type") == "upload":
            return await upload(ctx, typed)
        try:
            where = await profile(ctx, typed.screen_id)
        except ScreenRefusedError as exc:
            return BrowserResult(ok=False, error=str(exc))
        policy = await org_policy(uow_factory, ctx)
        if typed.action.get("type") == "navigate":
            refused = refusal(policy, str(typed.action.get("url", "")))
            if refused:
                await record_block(
                    uow_factory, ctx, str(typed.action.get("url", "")), "browser.act@1"
                )
                return BrowserResult(ok=False, error=refused)
        response = await _post(
            f"/screens/{typed.screen_id}/act",
            {"action": typed.action, "label": typed.label},
            where,
            policy,
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


async def _default_profile(ctx: ToolContext | None, screen_id: str) -> str:
    return ""


def _filler(
    base: str,
    vault: Callable[[], Vault],
    profile: Callable[[ToolContext | None, str], Awaitable[str]] = _default_profile,
) -> Callable[[ToolContext | None, ActArgs], Awaitable[BrowserResult]]:
    async def fill(ctx: ToolContext | None, typed: ActArgs) -> BrowserResult:
        action = typed.action
        if ctx is None:
            return BrowserResult(ok=False, error="a fill needs a run context")
        try:
            where = await profile(ctx, typed.screen_id)
            entries = [uuid.UUID(str(e)) for e in action.get("entries", [])]
            site, values = await vault().open(
                uuid.UUID(ctx.organization_id),
                entries,
                bot_id=uuid.UUID(typed.screen_id),
                profile=where,
            )
        except (VaultUnavailableError, VaultRefusedError, ScreenRefusedError, ValueError) as exc:
            return BrowserResult(ok=False, error=str(exc).splitlines()[0])
        fields: list[dict[str, Any]] = []
        missing: list[str] = []
        for spec in action.get("fields", []):
            elements = [int(e) for e in spec.get("elements", [])]
            if not elements:
                continue
            kind = str(spec.get("kind", "text"))
            value = lookup(kind, values, str(spec.get("key", "")))
            if value is None:
                missing.append(kind)
                continue
            fields.append(
                {"elements": elements, "value": value, "password": kind in PASSWORD_KINDS}
            )
        values.clear()
        if missing:
            return BrowserResult(
                ok=False, error=f"the vault has no {', '.join(missing)} for {site}"
            )
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
                response = await client.post(
                    f"{base}/screens/{typed.screen_id}/fill",
                    params={"profile": where},
                    json={
                        "expect_host": site,
                        "fields": fields,
                        "submit": bool(action.get("submit", True)),
                        "label": typed.label,
                    },
                )
        except httpx.TransportError as exc:
            raise TransientFault(
                f"the computer at {base} is not reachable ({type(exc).__name__})"
            ) from exc
        finally:
            fields.clear()
        if response.status_code == 409:
            return BrowserResult(ok=False, error=_detail(response), controller="human")
        if response.status_code >= 400:
            # A validation error from the computer echoes its input, and its input
            # was the values. Only our own messages, which are strings, are relayed.
            detail = _detail(response, safe_only=True)
            return BrowserResult(ok=False, error=detail)
        return _result(response.json())

    return fill


def _detail(response: httpx.Response, *, safe_only: bool = False) -> str:
    try:
        body = response.json()
    except ValueError:
        return f"computer answered {response.status_code}"
    detail = body.get("detail") if isinstance(body, dict) else None
    if safe_only and not isinstance(detail, str):
        return f"the computer refused the fill ({response.status_code})"
    return str(detail or f"computer answered {response.status_code}")


def register(
    registry: ToolRegistry, settings: Settings, uow_factory: UnitOfWorkFactory | None = None
) -> None:
    for definition, fn in build(settings, uow_factory):
        registry.register(definition, fn)
