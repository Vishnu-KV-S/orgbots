"""`web.fetch@1` — an HTTP GET.

`replay_safe`, and it is the honest declaration rather than a convenient one: a GET
does not mutate the far side, so re-executing it after a crash costs a request and
nothing else. `check_entailment` would reject the declaration if this tool also
claimed to mutate external state.

This is the only module in the tool layer that imports `httpx`, and it is allowed
to because it lives under `runtime.gateway`. A graph node importing `httpx` fails
CI on the `gateway-only` contract.
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel, Field

from runtime.domain.enums import BlastRadius, RecoveryPolicy
from runtime.gateway.tools import EffectCapabilities, ToolContext, ToolDef, ToolRegistry

MAX_BODY_BYTES = 2 * 1024 * 1024
"""Refuse rather than stream a response that will not fit in an artifact anyway.
A tool that silently truncates produces results that look complete."""


class WebFetchArgs(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_s: float = Field(default=10.0, gt=0, le=60)


class WebFetchResult(BaseModel):
    url: str
    status_code: int
    headers: dict[str, str]
    body: str
    truncated: bool = False


async def web_fetch(ctx: ToolContext, args: Any) -> WebFetchResult:
    _ = ctx
    typed: WebFetchArgs = args
    async with httpx.AsyncClient(follow_redirects=True, timeout=typed.timeout_s) as client:
        response = await client.get(typed.url, headers=typed.headers)
    body = response.content[:MAX_BODY_BYTES]
    return WebFetchResult(
        url=str(response.url),
        status_code=response.status_code,
        headers={k.lower(): v for k, v in response.headers.items()},
        body=body.decode("utf-8", errors="replace"),
        truncated=len(response.content) > MAX_BODY_BYTES,
    )


DEFINITION = ToolDef(
    name="web.fetch",
    version=1,
    args_model=WebFetchArgs,
    result_model=WebFetchResult,
    capabilities=EffectCapabilities(
        mutates_external_state=False,
        max_blast_radius=BlastRadius.READ,
    ),
    recovery_policy=RecoveryPolicy.REPLAY_SAFE,
    timeout_s=30.0,
)


def register(registry: ToolRegistry) -> None:
    registry.register(DEFINITION, web_fetch)
