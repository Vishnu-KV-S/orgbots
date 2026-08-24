"""`publish.external@1` — the one real external effect, behind the gate.

PR-26 is last in the build order for a reason and this module is why. Everything
else in M1 can be wrong and the cost is a bad number; this one can be wrong and the
cost is something a stranger reads.

**Three independent things must all be true before it publishes for real.**

1. `publish_endpoint_url` is set. Unset, the tool raises.
2. `publish_target = "production"`. Defaults to `"staging"`.
3. `publish_allow_production = true`. Defaults to false.

Risk 5 says *"point it at a staging destination for the first week regardless of
what the approval gate says"*, and the defaults do that on their own — forgetting is
safe, and going live is a deliberate act in two places. One setting is a typo away
from a real publish; two are not.

**Blast radius IRREVERSIBLE**, which the registry turns into `retries = 0`,
`requires_approval = True` and elevated audit, and which a tool may tighten but
never loosen. It declares `authority_action = "publish_external"` — the one entry
in the M1 authority dict — so `ToolGateway._authorise` refuses unless a human has
granted approval *for the task*.

**Recovery is `idempotency_key`.** The logical call id goes out as an
`Idempotency-Key` header, so a crash between the POST and the journal commit ends
with a replay that re-sends the same key and the far side deduplicates. That is the
only recovery policy honest for this tool: `probe` would need a searchable marker
the endpoint does not promise, and `replay_safe` would be a lie that publishes
twice.
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import BaseModel, Field

from runtime.domain.enums import AuditSeverity, BlastRadius, RecoveryPolicy
from runtime.domain.errors import ProviderUnavailable, SpecError
from runtime.gateway.tools import EffectCapabilities, ToolContext, ToolDef, ToolRegistry
from runtime.observability.logging import get_logger
from runtime.settings import Settings

log = get_logger("gateway.publish")

AUTHORITY_ACTION = "publish_external"


class PublishArgs(BaseModel):
    title: str = Field(min_length=10, max_length=200)
    body_markdown: str = Field(min_length=200, max_length=100_000)
    channel: str = Field(min_length=2, max_length=40)
    task_id: str = Field(min_length=1, max_length=64)
    """The task whose approval authorises this. Carried in the payload as well as
    checked at the gateway, so the far side's record says what it was published
    under — an audit that only exists on our side is half an audit."""


class PublishResult(BaseModel):
    published: bool
    target: str
    url: str | None = None
    provider_ref: str | None = None
    idempotency_key: str


def build(settings: Settings) -> tuple[ToolDef, Any]:
    async def publish(ctx: ToolContext, args: Any) -> PublishResult:
        typed: PublishArgs = args
        endpoint = settings.publish_endpoint_url
        if not endpoint:
            raise SpecError(
                "publish.external@1 is not configured: set RUNTIME_PUBLISH_ENDPOINT_URL"
            )

        live = settings.publish_target == "production" and settings.publish_allow_production
        target = "production" if live else "staging"
        if settings.publish_target == "production" and not settings.publish_allow_production:
            # Configured for production but not permitted. Downgrading silently
            # would be worse than either alternative — the operator would believe
            # it went live — so it is logged at warning and the result says staging.
            log.warning(
                "publish.downgraded_to_staging",
                reason="publish_allow_production is false",
                task_id=typed.task_id,
            )

        headers = {
            "content-type": "application/json",
            # The whole recovery policy, in one header. Reproduced exactly on
            # replay because `logical_call_id` is a pure function of the call.
            "idempotency-key": ctx.idempotency_key,
            "x-runtime-marker": ctx.marker,
            "x-runtime-target": target,
        }
        if settings.publish_api_key:
            headers["authorization"] = f"Bearer {settings.publish_api_key}"

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                endpoint,
                headers=headers,
                json={
                    "title": typed.title,
                    "body_markdown": typed.body_markdown,
                    "channel": typed.channel,
                    "task_id": typed.task_id,
                    "target": target,
                    "run_id": ctx.run_id,
                },
            )
        if response.status_code >= 500:
            raise ProviderUnavailable(f"publish endpoint returned {response.status_code}")
        response.raise_for_status()

        body: dict[str, Any] = {}
        if response.headers.get("content-type", "").startswith("application/json"):
            body = response.json()

        log.info(
            "publish.completed",
            target=target,
            task_id=typed.task_id,
            status=response.status_code,
            provider_ref=body.get("id"),
        )
        return PublishResult(
            published=True,
            target=target,
            url=body.get("url"),
            provider_ref=str(body["id"]) if body.get("id") is not None else None,
            idempotency_key=ctx.idempotency_key,
        )

    definition = ToolDef(
        name="publish.external",
        version=1,
        args_model=PublishArgs,
        result_model=PublishResult,
        capabilities=EffectCapabilities(
            mutates_external_state=True,
            accepts_idempotency_key=True,
            idempotency_key_field="idempotency-key",
            max_blast_radius=BlastRadius.IRREVERSIBLE,
        ),
        recovery_policy=RecoveryPolicy.IDEMPOTENCY_KEY,
        # Stated explicitly even though IRREVERSIBLE forces them. A reader of this
        # file should not have to know the blast-radius table to know a failed
        # publish is not retried and an unapproved one does not run.
        max_retries=0,
        requires_approval=True,
        audit_severity=AuditSeverity.HIGH,
        authority_action=AUTHORITY_ACTION,
        timeout_s=30.0,
    )
    return definition, publish


def register(registry: ToolRegistry, settings: Settings) -> None:
    definition, fn = build(settings)
    registry.register(definition, fn)
