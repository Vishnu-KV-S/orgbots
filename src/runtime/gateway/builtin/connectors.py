"""`connector.call@1` — call a tool on an MCP server the organization connected.

The bot names a connector and a tool; this looks the connector up in the calling run's
own organization, opens its token with the credential cipher, makes the call over
`runtime.gateway.mcp`, and returns the result as text — the token goes from ciphertext
to a request header and is dropped, so a run's state, the journal, the audit row and
the model never hold it.

It reads its connector's row itself, the one exception `ToolContext` makes beside the
vault's fill and for the same reason: what it opens is a secret, and the only way to
keep a secret out of a run is to never put it in the call's arguments.

**REVERSIBLE / `manual`, no retries.** An MCP tool can be anything from a search to a
payment; the runtime cannot tell which from outside, so a replay after a crash
mid-call leaves an INTENT row for a person rather than calling twice. Whether a call
needed approval was the bot's gate's decision (read-only tools run, others ask unless
allowed), made before this tool was called.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import BaseModel, Field

from runtime.domain.enums import BlastRadius, RecoveryPolicy
from runtime.gateway.builtin.profiles import org_policy, record_block, refusal
from runtime.gateway.mcp import Auth, Connect, MCPError, connect, result_text
from runtime.gateway.tools import EffectCapabilities, ToolContext, ToolDef, ToolRegistry
from runtime.gateway.vault import VaultUnavailableError, load_cipher
from runtime.persistence.repositories.connectors import ConnectorRow
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.settings import Settings


def token_aad(row: ConnectorRow | Any) -> bytes:
    """What a connector's sealed token is bound to: its organization and its id, so a
    row copied anywhere else does not open."""
    return f"connector:{row.organization_id}:{row.id}".encode()


def open_auth(row: ConnectorRow, settings: Settings) -> Auth:
    if row.auth_kind == "none" or row.secret_ciphertext is None:
        return Auth()
    cipher = load_cipher(settings)
    token = cipher.decrypt(
        row.secret_key_id or "",
        row.secret_nonce or b"",
        row.secret_ciphertext,
        aad=token_aad(row),
    )
    return Auth(kind=row.auth_kind, header=row.header_name, token=token)


class CallArgs(BaseModel):
    connector: str = Field(min_length=1, max_length=40)
    tool: str = Field(min_length=1, max_length=200)
    arguments: dict[str, Any] = Field(default_factory=dict)


class CallResult(BaseModel):
    ok: bool
    error: str | None = None
    text: str = ""
    is_error: bool = False
    connector: str = ""
    tool: str = ""


def build(
    settings: Settings, uow_factory: UnitOfWorkFactory | None, open_session: Connect = connect
) -> tuple[ToolDef, Any]:
    async def call(ctx: ToolContext, args: Any) -> CallResult:
        typed: CallArgs = args
        base = {"connector": typed.connector, "tool": typed.tool}
        if uow_factory is None:
            return CallResult(ok=False, error="this gateway was built without connectors", **base)
        async with uow_factory() as uow:
            row = await uow.connectors.by_name(uuid.UUID(ctx.organization_id), typed.connector)
        if row is None or not row.enabled:
            return CallResult(ok=False, error=f"there is no connector {typed.connector!r}", **base)
        refused = refusal(await org_policy(uow_factory, ctx), row.url)
        if refused:
            await record_block(uow_factory, ctx, row.url, "connector.call@1")
            return CallResult(ok=False, error=refused, **base)
        try:
            auth = open_auth(row, settings)
        except (VaultUnavailableError, ValueError) as exc:
            return CallResult(ok=False, error=str(exc).splitlines()[0], **base)
        except Exception:
            return CallResult(ok=False, error="the connector's token could not be opened", **base)
        try:
            async with open_session(row.url, auth) as session:
                result = await session.call_tool(typed.tool, typed.arguments)
        except MCPError as exc:
            return CallResult(ok=False, error=str(exc), **base)
        finally:
            del auth
        text, failed = result_text(result)
        return CallResult(ok=True, text=text, is_error=failed, **base)

    definition = ToolDef(
        name="connector.call",
        version=1,
        args_model=CallArgs,
        result_model=CallResult,
        capabilities=EffectCapabilities(
            mutates_external_state=True, max_blast_radius=BlastRadius.REVERSIBLE
        ),
        recovery_policy=RecoveryPolicy.MANUAL,
        timeout_s=90,
        max_retries=0,
    )
    return definition, call


def register(
    registry: ToolRegistry, settings: Settings, uow_factory: UnitOfWorkFactory | None = None
) -> None:
    definition, fn = build(settings, uow_factory)
    registry.register(definition, fn)
