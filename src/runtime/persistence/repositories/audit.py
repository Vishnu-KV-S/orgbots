"""Audit log. Append-only; never updated, never deleted.

Two tables, and the split is M2 §2's point rather than a schema accident.

`audit_log` (004) is the **action** trail: one row per thing that happened, keyed to
a `logical_call_id`, written inside the effect pipeline.

`audit_logs` (020) is the **decision** trail: one row per *check*, written by the
gateways whether the answer was yes or no. It is much higher volume, and that is
deliberate — a denial rate without a denominator is a number nobody can act on. It
is partitioned by month because a year of it on one heap makes the §9 review a
sequential scan.

`record_decisions` takes a list and writes one multi-row INSERT. That is not
micro-optimisation: M2 §9 names unbatched audit writes as the first thing to check
when the cost-per-outcome regression shows up, and a governance pipeline with eight
checks would otherwise add eight round trips to every tool call.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.domain.enums import AuditSeverity, GatewayDecision
from runtime.persistence.json import to_jsonb


@dataclass(frozen=True, slots=True)
class DecisionRow:
    """One gateway check and how it went.

    `check_name` is the name of the *check*, not of the outcome — `permission`,
    `authority`, `kill_switch`, `rate_limit`, `budget`. The §9 review groups on it,
    and "denied" without which gate denied it is the audit line that costs an hour.
    """

    organization_id: uuid.UUID
    gateway: str
    subject: str
    decision: GatewayDecision
    check_name: str
    reason: str | None = None
    severity: AuditSeverity = AuditSeverity.NORMAL
    run_id: uuid.UUID | None = None
    root_run_id: uuid.UUID | None = None
    actor_id: uuid.UUID | None = None
    actor_name: str | None = None
    blast_radius: str | None = None
    cost_cents: int | None = None
    trace_id: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    occurred_at: dt.datetime | None = None


class AuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def record(
        self,
        *,
        organization_id: uuid.UUID,
        action: str,
        outcome: str,
        run_id: uuid.UUID | None = None,
        root_run_id: uuid.UUID | None = None,
        actor_id: uuid.UUID | None = None,
        fence: int | None = None,
        target: str | None = None,
        severity: AuditSeverity = AuditSeverity.NORMAL,
        detail: dict[str, Any] | None = None,
        trace_id: str | None = None,
    ) -> None:
        await self._s.execute(
            text(
                """
                INSERT INTO audit_log (organization_id, run_id, root_run_id, actor_id,
                                       fence, action, target, severity, outcome, detail,
                                       trace_id)
                VALUES (:org, :run_id, :root_run_id, :actor_id, :fence, :action, :target,
                        :severity, :outcome, CAST(:detail AS jsonb), :trace_id)
                """
            ),
            {
                "org": organization_id,
                "run_id": run_id,
                "root_run_id": root_run_id,
                "actor_id": actor_id,
                "fence": fence,
                "action": action,
                "target": target,
                "severity": severity.value,
                "outcome": outcome,
                "detail": to_jsonb(detail or {}),
                "trace_id": trace_id,
            },
        )

    async def record_decisions(self, rows: list[DecisionRow]) -> int:
        """Write a batch of gateway decisions as one statement.

        One `executemany` rather than N round trips. The parameter list is built here
        rather than by the caller so that adding a column is one edit and cannot leave
        a call site writing NULLs into it.
        """
        if not rows:
            return 0
        await self._s.execute(
            text(
                """
                INSERT INTO audit_logs (organization_id, occurred_at, run_id, root_run_id,
                                        actor_id, actor_name, gateway, subject, decision,
                                        check_name, reason, severity, blast_radius,
                                        cost_cents, trace_id, detail)
                VALUES (:org, COALESCE(:occurred_at, now()), :run_id, :root_run_id,
                        :actor_id, :actor_name, :gateway, :subject, :decision,
                        :check_name, :reason, :severity, :blast_radius,
                        :cost_cents, :trace_id, CAST(:detail AS jsonb))
                """
            ),
            [
                {
                    "org": r.organization_id,
                    "occurred_at": r.occurred_at,
                    "run_id": r.run_id,
                    "root_run_id": r.root_run_id,
                    "actor_id": r.actor_id,
                    "actor_name": r.actor_name,
                    "gateway": r.gateway,
                    "subject": r.subject,
                    "decision": r.decision.value,
                    "check_name": r.check_name,
                    "reason": r.reason,
                    "severity": r.severity.value,
                    "blast_radius": r.blast_radius,
                    "cost_cents": r.cost_cents,
                    "trace_id": r.trace_id,
                    "detail": to_jsonb(r.detail),
                }
                for r in rows
            ],
        )
        return len(rows)

    async def decisions_for_run(self, run_id: uuid.UUID) -> list[dict[str, Any]]:
        rows = (
            await self._s.execute(
                text(
                    """
                    SELECT gateway, subject, decision, check_name, reason, severity,
                           blast_radius, detail, occurred_at
                      FROM audit_logs WHERE run_id = :run_id ORDER BY occurred_at, id
                    """
                ),
                {"run_id": run_id},
            )
        ).all()
        return [
            {
                "gateway": r.gateway,
                "subject": r.subject,
                "decision": r.decision,
                "check": r.check_name,
                "reason": r.reason,
                "severity": r.severity,
                "blast_radius": r.blast_radius,
                "detail": r.detail,
                "occurred_at": r.occurred_at.isoformat(),
            }
            for r in rows
        ]

    async def denial_stream(
        self, organization_id: uuid.UUID, *, since: dt.datetime, limit: int = 200
    ) -> list[dict[str, Any]]:
        """The §9 exit criterion, as a query.

        Grouped rather than listed, because the review's question is "what does an
        actor keep trying to do that it cannot" and a thousand identical rows answer
        it worse than one row with a count of a thousand.
        """
        rows = (
            await self._s.execute(
                text(
                    """
                    SELECT actor_name, gateway, subject, check_name, reason,
                           count(*) AS denials,
                           min(occurred_at) AS first_seen,
                           max(occurred_at) AS last_seen
                      FROM audit_logs
                     WHERE organization_id = :org AND occurred_at >= :since
                       AND decision <> 'allowed'
                     GROUP BY actor_name, gateway, subject, check_name, reason
                     ORDER BY count(*) DESC
                     LIMIT :limit
                    """
                ),
                {"org": organization_id, "since": since, "limit": limit},
            )
        ).all()
        return [
            {
                "actor": r.actor_name,
                "gateway": r.gateway,
                "subject": r.subject,
                "check": r.check_name,
                "reason": r.reason,
                "denials": int(r.denials),
                "first_seen": r.first_seen.isoformat(),
                "last_seen": r.last_seen.isoformat(),
            }
            for r in rows
        ]

    async def decision_counts(
        self, organization_id: uuid.UUID, *, since: dt.datetime
    ) -> dict[str, int]:
        rows = (
            await self._s.execute(
                text(
                    """
                    SELECT decision, count(*) AS n FROM audit_logs
                     WHERE organization_id = :org AND occurred_at >= :since
                     GROUP BY decision
                    """
                ),
                {"org": organization_id, "since": since},
            )
        ).all()
        return {r.decision: int(r.n) for r in rows}

    async def ensure_partition(self, month: dt.date) -> str:
        """Create the partition covering `month` if it does not exist.

        Called by the operator CLI and by the scheduler's monthly tick. A row landing
        in the DEFAULT partition is not lost, but it is a signal that nobody extended
        the window, so `v_audit_partition_health` reports the default's size.
        """
        return str(
            (
                await self._s.execute(
                    text("SELECT ensure_audit_partition(:month)"), {"month": month}
                )
            ).scalar_one()
        )

    async def for_run(self, run_id: uuid.UUID) -> list[dict[str, Any]]:
        rows = (
            await self._s.execute(
                text(
                    """
                    SELECT action, target, severity, outcome, detail, created_at
                      FROM audit_log WHERE run_id = :run_id ORDER BY id
                    """
                ),
                {"run_id": run_id},
            )
        ).all()
        return [
            {
                "action": r.action,
                "target": r.target,
                "severity": r.severity,
                "outcome": r.outcome,
                "detail": r.detail,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]
