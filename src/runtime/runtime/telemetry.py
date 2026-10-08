"""The telemetry exporter — each organization's events to its collector, once each.

A loop in the worker, beside the notifier. For every organization with export turned
on (`otel_configs`) it sends, as OTLP logs (`gateway.otel`):

- **the control-plane trail** (`org_audit_events`): sign-ins, invitations, roles, single
  sign-on, SCIM, policies, secrets (by name), bots shared and deleted, template links,
  apps, routines — stamped `exported_at` once the collector accepts them;
- **action records** (when `include_actions`): every tool call a bot made and how the
  gateway decided it — the tool, allowed or denied and by which check, the run, the
  bot's actor — from the gateway's decision log (`audit_logs`), from a cursor. Never a
  tool's arguments or result, a message, a file or a page: those stay in the runtime.

Members' emails are left out unless the admin asked for them (`include_email`): the
actor is then the member's id, and an email that is an event's target is withheld.

A batch the collector refuses stays for the next tick, and the reason is shown to
admins (`last_error`). Headers (an API key, say) are sealed and opened only here.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass
from typing import Any

from runtime.gateway.otel import OTelError, OTLPSender, log_record, payload
from runtime.gateway.vault import VaultUnavailableError, load_cipher
from runtime.observability.logging import get_logger
from runtime.persistence.repositories.enterprise import AuditEventRow, OtelRow
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.runtime.enterprise import otel_aad
from runtime.settings import Settings

log = get_logger("runtime.telemetry")

TICK_SECONDS = 10.0
BATCH = 500


@dataclass
class ExportTick:
    organizations: int = 0
    events: int = 0
    actions: int = 0
    failed: int = 0


def event_record(row: AuditEventRow, *, include_email: bool) -> dict[str, Any]:
    target = row.target
    attrs: dict[str, Any] = {
        "agent_org.surface": "bots",
        "actor.member_id": str(row.actor_member_id) if row.actor_member_id else "",
        "actor.kind": "member" if row.actor_member_id else (row.actor or "person"),
        "event.target": target if include_email or "@" not in target else "",
        "event.detail": json.dumps(row.detail, default=str) if row.detail else "",
        "event.id": row.id,
    }
    if include_email and row.actor_member_id:
        attrs["actor.email"] = row.actor
    return log_record(row.occurred_at, row.action, attrs)


def action_record(row: dict[str, Any]) -> dict[str, Any]:
    denied = row["decision"] != "allowed"
    return log_record(
        row["occurred_at"],
        "tool.call",
        {
            "agent_org.surface": "bots",
            "tool.name": row["subject"],
            "tool.decision": row["decision"],
            "tool.check": row["check_name"] or "",
            "tool.blast_radius": row["blast_radius"] or "",
            "run.id": str(row["run_id"]) if row["run_id"] else "",
            "actor.name": row["actor_name"] or "",
            "event.id": int(row["id"]),
        },
        severity="WARN" if denied else "INFO",
    )


class TelemetryExporter:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        settings: Settings,
        sender: OTLPSender | None = None,
    ) -> None:
        self._uow = uow_factory
        self._settings = settings
        self._sender = sender or OTLPSender()
        self._stopping = asyncio.Event()

    async def tick(self) -> ExportTick:
        out = ExportTick()
        async with self._uow() as uow:
            configs = await uow.otel.enabled()
        for config in configs:
            out.organizations += 1
            try:
                events, actions = await self._export(config)
                out.events += events
                out.actions += actions
            except (OTelError, VaultUnavailableError, ValueError) as exc:
                out.failed += 1
                async with self._uow.transaction() as uow:
                    await uow.otel.outcome(config.organization_id, error=str(exc).splitlines()[0])
                log.warning(
                    "telemetry.failed", organization_id=str(config.organization_id), error=str(exc)
                )
        return out

    async def _export(self, config: OtelRow) -> tuple[int, int]:
        org = config.organization_id
        headers: dict[str, str] = {}
        if config.headers_ciphertext is not None:
            opened = load_cipher(self._settings).decrypt(
                config.headers_key_id or "",
                config.headers_nonce or b"",
                config.headers_ciphertext,
                aad=otel_aad(org),
            )
            headers = {str(k): str(v) for k, v in json.loads(opened).items()}
        async with self._uow() as uow:
            events = await uow.org_audit.unexported(org, BATCH)
        if events:
            records = [event_record(e, include_email=config.include_email) for e in events]
            await self._sender.send(
                config.endpoint, headers, payload(str(org), "agent-org.audit", records)
            )
            async with self._uow.transaction() as uow:
                await uow.org_audit.mark_exported([e.id for e in events])
        actions: list[dict[str, Any]] = []
        if config.include_actions:
            async with self._uow.transaction() as uow:
                cursor = await uow.otel.cursor(org)
                actions = await uow.org_audit.decisions_after(org, cursor, BATCH)
            if actions:
                records = [action_record(a) for a in actions]
                await self._sender.send(
                    config.endpoint, headers, payload(str(org), "agent-org.actions", records)
                )
                async with self._uow.transaction() as uow:
                    await uow.otel.set_cursor(org, int(actions[-1]["id"]))
        if events or actions:
            async with self._uow.transaction() as uow:
                await uow.otel.outcome(org, error="")
        headers.clear()
        return len(events), len(actions)

    async def run_forever(self) -> None:
        while not self._stopping.is_set():
            try:
                await self.tick()
            except Exception:
                log.exception("telemetry.tick_failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stopping.wait(), timeout=TICK_SECONDS)

    def stop(self) -> None:
        self._stopping.set()
