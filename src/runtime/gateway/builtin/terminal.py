"""`terminal.run@1`, `workspace.read@1`, `workspace.write@1` — a bot's shell and its files.

Like the browser tools, these are HTTP calls to the computer (`runtime.computer`), and
everything that governs them happens here first: permission, kill switch, ceilings,
budget, the effect journal, scrubbing and audit. Whether a command needed the person's
approval was decided one level up, by the bot's gate (`domain.bots.needs_approval`),
before the call was made — a `local` command reaches this tool only once it was allowed.

`terminal.run` is **REVERSIBLE / `manual`**, honestly so: a command can change files in
the workspace or, run locally, anything the person's user can, and no shell offers an
idempotency key. A replay after a crash mid-command leaves an INTENT row for a person
rather than running it twice; there are no retries.

`workspace.read` is READ and replay-safe; a file over the gateway's inline limit comes
back as an artifact reference like any large result, so the bytes never sit in a run's
state. `workspace.write` writes the same bytes to the same path when replayed, which
is the same file — but it is the bot changing something, so it is journalled and not
retried either.
"""

from __future__ import annotations

import base64
from typing import Any

import httpx
from pydantic import BaseModel, Field

from runtime.domain.enums import BlastRadius, RecoveryPolicy
from runtime.domain.errors import TransientFault
from runtime.gateway.builtin.profiles import profile_of, run_bot
from runtime.gateway.tools import EffectCapabilities, ToolContext, ToolDef, ToolRegistry
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.settings import Settings

MAX_COMMAND_S = 300
TRANSFER_BYTES = 10 * 1024 * 1024


class RunArgs(BaseModel):
    screen_id: str = Field(min_length=1, max_length=64)
    command: str = Field(min_length=1, max_length=20_000)
    timeout_s: float = Field(default=60.0, ge=1, le=MAX_COMMAND_S)
    local: bool = False


class RunResult(BaseModel):
    ok: bool
    error: str | None = None
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    truncated: bool = False
    timed_out: bool = False
    seconds: float = 0.0
    mode: str = "sandbox"


class ReadArgs(BaseModel):
    path: str = Field(min_length=1, max_length=400)


class ReadResult(BaseModel):
    ok: bool
    error: str | None = None
    path: str = ""
    bytes: int = 0
    data: str = Field(default="", repr=False)
    """Base64."""


class WriteArgs(BaseModel):
    path: str = Field(min_length=1, max_length=400)
    data: str = Field(max_length=(TRANSFER_BYTES * 4) // 3 + 16, repr=False)
    """Base64."""


class WriteResult(BaseModel):
    ok: bool
    error: str | None = None
    path: str = ""
    bytes: int = 0


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return f"computer answered {response.status_code}"
    detail = body.get("detail") if isinstance(body, dict) else None
    return str(detail or f"computer answered {response.status_code}")


def build(
    settings: Settings, uow_factory: UnitOfWorkFactory | None = None
) -> list[tuple[ToolDef, Any]]:
    base = settings.computer_url.rstrip("/")

    async def _call(method: str, path: str, *, wait_s: float, **kwargs: Any) -> httpx.Response:
        try:
            async with httpx.AsyncClient(timeout=wait_s) as client:
                return await client.request(method, f"{base}{path}", **kwargs)
        except httpx.TransportError as exc:
            raise TransientFault(
                f"the computer at {base} is not reachable ({type(exc).__name__}); "
                "start it with `python -m runtime.computer.main`"
            ) from exc

    async def run(ctx: ToolContext, args: Any) -> RunResult:
        typed: RunArgs = args
        bot = await run_bot(uow_factory, ctx)
        mode = "local" if typed.local else "sandbox"
        if bot is not None and str(bot.id) != typed.screen_id:
            return RunResult(ok=False, error="a bot can only use its own terminal", mode=mode)
        if typed.local and bot is not None and bot.owner_member_id is not None:
            # "This computer" is the machine the runtime runs on — shared by every member.
            # A member's bot runs commands in its own sandbox and workspace instead.
            return RunResult(
                ok=False,
                error="running commands on the server is off for organizations with members; "
                "use the sandbox",
                mode=mode,
            )
        response = await _call(
            "POST",
            "/terminal/run",
            wait_s=typed.timeout_s + 15,
            json={**typed.model_dump(), "profile": profile_of(bot)},
        )
        if response.status_code >= 400:
            return RunResult(
                ok=False, error=_detail(response), mode="local" if typed.local else "sandbox"
            )
        return RunResult.model_validate(response.json())

    async def read(ctx: ToolContext, args: Any) -> ReadResult:
        typed: ReadArgs = args
        where = profile_of(await run_bot(uow_factory, ctx))
        response = await _call(
            "GET", "/workspace/file", wait_s=60, params={"path": typed.path, "profile": where}
        )
        if response.status_code >= 400:
            return ReadResult(ok=False, error=_detail(response), path=typed.path)
        return ReadResult(
            ok=True,
            path=response.headers.get("x-path", typed.path),
            bytes=len(response.content),
            data=base64.b64encode(response.content).decode("ascii"),
        )

    async def write(ctx: ToolContext, args: Any) -> WriteResult:
        typed: WriteArgs = args
        where = profile_of(await run_bot(uow_factory, ctx))
        response = await _call(
            "POST",
            "/workspace/file",
            wait_s=60,
            json=typed.model_dump(),
            params={"profile": where},
        )
        if response.status_code >= 400:
            return WriteResult(ok=False, error=_detail(response), path=typed.path)
        body = response.json()
        return WriteResult(ok=True, path=str(body.get("path", "")), bytes=int(body.get("bytes", 0)))

    return [
        (
            ToolDef(
                name="terminal.run",
                version=1,
                args_model=RunArgs,
                result_model=RunResult,
                capabilities=EffectCapabilities(
                    mutates_external_state=True, max_blast_radius=BlastRadius.REVERSIBLE
                ),
                recovery_policy=RecoveryPolicy.MANUAL,
                timeout_s=MAX_COMMAND_S + 30,
                max_retries=0,
            ),
            run,
        ),
        (
            ToolDef(
                name="workspace.read",
                version=1,
                args_model=ReadArgs,
                result_model=ReadResult,
                capabilities=EffectCapabilities(
                    mutates_external_state=False, max_blast_radius=BlastRadius.READ
                ),
                recovery_policy=RecoveryPolicy.REPLAY_SAFE,
                timeout_s=75,
            ),
            read,
        ),
        (
            ToolDef(
                name="workspace.write",
                version=1,
                args_model=WriteArgs,
                result_model=WriteResult,
                capabilities=EffectCapabilities(
                    mutates_external_state=True, max_blast_radius=BlastRadius.REVERSIBLE
                ),
                recovery_policy=RecoveryPolicy.MANUAL,
                timeout_s=75,
                max_retries=0,
            ),
            write,
        ),
    ]


def register(
    registry: ToolRegistry, settings: Settings, uow_factory: UnitOfWorkFactory | None = None
) -> None:
    for definition, fn in build(settings, uow_factory):
        registry.register(definition, fn)
