"""Approvals.

Two conditional statements, and both of them are about "first writer wins".

`request()` conflicts on `(subject_type, subject_id, action)` and returns the
existing row, so asking twice produces one thing for a human to answer.

`decide()` requires `status = 'PENDING'`. A second decision — a race between the
CLI and the TTL sweeper, or two people clicking at once — updates zero rows and the
caller records the loser in `audit_log` rather than overwriting the winner. That is
T23: first wins, second recorded, neither lost.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from runtime.domain.enums import ApprovalStatus
from runtime.domain.ids import ApprovalId
from runtime.persistence.json import to_jsonb


@dataclass(frozen=True, slots=True)
class ApprovalRow:
    id: ApprovalId
    organization_id: uuid.UUID
    subject_type: str
    subject_id: str
    action: str
    detail: dict[str, Any]
    approver: str
    requested_by_actor: str | None
    status: ApprovalStatus
    on_expiry: str
    expires_at: dt.datetime
    decided_at: dt.datetime | None
    decided_by: str | None
    decision_note: str | None
    correlation_id: uuid.UUID | None
    created_at: dt.datetime
    # --- M2 ---
    escalation_chain: list[str] = field(default_factory=list)
    escalation_index: int = 0
    ttl_seconds: int = 24 * 60 * 60
    max_escalations: int = 0
    escalation_count: int = 0
    deferred_until: dt.datetime | None = None
    resume_token: str | None = None
    interrupt_id: str | None = None
    resume_token_used_at: dt.datetime | None = None
    resume_run_id: uuid.UUID | None = None
    requested_by_run_id: uuid.UUID | None = None

    @property
    def permits(self) -> bool:
        return self.status.permits

    @property
    def queued(self) -> bool:
        """Is this item visible in its approver's queue right now?

        A deferred item is pending and blocking, and it is *not* in the queue. That
        distinction is the whole of edge case 31: the run stays stopped, the person
        does not get a 40th thing to rubber-stamp.
        """
        return self.deferred_until is None

    @property
    def can_escalate(self) -> bool:
        return self.escalation_index < self.max_escalations


_SELECT = """
    SELECT id, organization_id, subject_type, subject_id, action, detail, approver,
           requested_by_actor, requested_by_run_id, status, on_expiry, expires_at,
           decided_at, decided_by, decision_note, correlation_id, created_at,
           escalation_chain, escalation_index, ttl_seconds, max_escalations,
           escalation_count, deferred_until, resume_token, interrupt_id,
           resume_token_used_at, resume_run_id
      FROM approvals
"""

_RETURNING = _SELECT.split("FROM")[0].replace("SELECT", "RETURNING", 1)
"""The same column list, as a RETURNING clause. Written once so a column added to
the row type cannot be silently absent from an UPDATE's result — which would surface
as an `AttributeError` in the sweeper at 2am rather than at import time."""


def _row(r: Any) -> ApprovalRow:
    return ApprovalRow(
        id=ApprovalId(r.id),
        organization_id=r.organization_id,
        subject_type=r.subject_type,
        subject_id=r.subject_id,
        action=r.action,
        detail=dict(r.detail or {}),
        approver=r.approver,
        requested_by_actor=r.requested_by_actor,
        requested_by_run_id=r.requested_by_run_id,
        status=ApprovalStatus(r.status),
        on_expiry=r.on_expiry,
        expires_at=r.expires_at,
        decided_at=r.decided_at,
        decided_by=r.decided_by,
        decision_note=r.decision_note,
        correlation_id=r.correlation_id,
        created_at=r.created_at,
        escalation_chain=list(r.escalation_chain or []),
        escalation_index=int(r.escalation_index),
        ttl_seconds=int(r.ttl_seconds),
        max_escalations=int(r.max_escalations),
        escalation_count=int(r.escalation_count),
        deferred_until=r.deferred_until,
        resume_token=r.resume_token,
        interrupt_id=r.interrupt_id,
        resume_token_used_at=r.resume_token_used_at,
        resume_run_id=r.resume_run_id,
    )


class ApprovalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._s = session

    async def request(
        self,
        approval_id: ApprovalId,
        *,
        organization_id: uuid.UUID,
        subject_type: str,
        subject_id: str,
        action: str,
        approver: str,
        expires_at: dt.datetime,
        detail: dict[str, Any] | None = None,
        requested_by_run_id: uuid.UUID | None = None,
        requested_by_actor: str | None = None,
        correlation_id: uuid.UUID | None = None,
        on_expiry: str = "deny",
        escalation_chain: list[str] | None = None,
        max_escalations: int = 0,
        ttl_seconds: int = 24 * 60 * 60,
        resume_token: str | None = None,
        interrupt_id: str | None = None,
        deferred_until: dt.datetime | None = None,
    ) -> tuple[ApprovalRow, bool]:
        """Create or return the approval for this action on this subject.

        Returns `(row, created)`. The flag matters in M2 in a way it did not in M1:
        the approver's daily budget is charged per *assignment*, so a re-request that
        found an existing row must not charge it again. Without the flag, a gate node
        that replays would eat somebody's whole day of oversight budget by itself.
        """
        created = (
            await self._s.execute(
                text(
                    """
                INSERT INTO approvals (id, organization_id, subject_type, subject_id, action,
                                       detail, requested_by_run_id, requested_by_actor,
                                       correlation_id, approver, expires_at, on_expiry,
                                       escalation_chain, max_escalations, ttl_seconds,
                                       resume_token, interrupt_id, deferred_until)
                VALUES (:id, :org, :st, :sid, :action, CAST(:detail AS jsonb), :run, :actor,
                        :corr, :approver, :expires, :on_expiry, CAST(:chain AS jsonb),
                        :max_esc, :ttl, :token, :interrupt, :deferred)
                ON CONFLICT ON CONSTRAINT uq_approval_subject DO NOTHING
                RETURNING id
                """
                ),
                {
                    "id": approval_id,
                    "org": organization_id,
                    "st": subject_type,
                    "sid": subject_id,
                    "action": action,
                    "detail": to_jsonb(detail or {}),
                    "run": requested_by_run_id,
                    "actor": requested_by_actor,
                    "corr": correlation_id,
                    "approver": approver,
                    "expires": expires_at,
                    "on_expiry": on_expiry,
                    "chain": to_jsonb(escalation_chain or []),
                    "max_esc": max_escalations,
                    "ttl": ttl_seconds,
                    "token": resume_token,
                    "interrupt": interrupt_id,
                    "deferred": deferred_until,
                },
            )
        ).one_or_none()
        row = await self.find(subject_type, subject_id, action)
        assert row is not None  # the insert above either wrote it or found it
        return row, created is not None

    async def find(self, subject_type: str, subject_id: str, action: str) -> ApprovalRow | None:
        r = (
            await self._s.execute(
                text(
                    _SELECT + " WHERE subject_type = :st AND subject_id = :sid AND action = :action"
                ),
                {"st": subject_type, "sid": subject_id, "action": action},
            )
        ).one_or_none()
        return _row(r) if r is not None else None

    async def get(self, approval_id: ApprovalId) -> ApprovalRow | None:
        r = (
            await self._s.execute(text(_SELECT + " WHERE id = :id"), {"id": approval_id})
        ).one_or_none()
        return _row(r) if r is not None else None

    async def decide(
        self,
        approval_id: ApprovalId,
        *,
        status: ApprovalStatus,
        decided_by: str,
        note: str | None = None,
    ) -> bool:
        """Record a decision. False means somebody already decided this one."""
        if status is ApprovalStatus.PENDING:
            raise ValueError("PENDING is not a decision")
        row = (
            await self._s.execute(
                text(
                    """
                    UPDATE approvals
                       SET status = :status, decided_at = now(), decided_by = :by,
                           decision_note = :note
                     WHERE id = :id AND status = 'PENDING'
                    RETURNING id
                    """
                ),
                {"id": approval_id, "status": status.value, "by": decided_by, "note": note},
            )
        ).one_or_none()
        return row is not None

    async def escalate_due(self, limit: int = 100) -> list[ApprovalRow]:
        """Move every escalatable, expired approval to the next approver in its chain.

        Runs *before* `expire_due` in the sweep, and the ordering is the whole design:
        an approval that can still escalate has not expired, it has been ignored, and
        those are different facts. Escalating first means `expire_due` only ever sees
        approvals that have exhausted their chain, so `on_expiry` means what it says —
        "what happens at `max_escalations`" — rather than "what happens the first time
        somebody is slow".

        The new deadline is a fresh `ttl_seconds` from now, read from the row rather
        than from live policy — so an escalation inherits the policy the approval was
        created under rather than whatever is configured this morning. Deriving it from
        `expires_at - created_at` instead is the obvious shortcut and is wrong: this
        statement rewrites `expires_at`, so after one hop that difference measures how
        late the first approver was.
        """
        rows = (
            await self._s.execute(
                text(
                    """
                    UPDATE approvals a
                       SET escalation_index = a.escalation_index + 1,
                           escalation_count = a.escalation_count + 1,
                           approver = a.escalation_chain ->> (a.escalation_index + 1),
                           expires_at = now() + make_interval(secs => a.ttl_seconds),
                           deferred_until = NULL,
                           detail = a.detail || jsonb_build_object(
                               'escalated_from', a.approver,
                               'escalated_at', to_jsonb(now())
                           )
                     WHERE a.id IN (
                        SELECT id FROM approvals
                         WHERE status = 'PENDING' AND expires_at < now()
                           AND escalation_index < max_escalations
                           AND jsonb_array_length(escalation_chain) > escalation_index + 1
                         ORDER BY expires_at LIMIT :limit
                         FOR UPDATE SKIP LOCKED
                     )
                    """
                    + _RETURNING
                ),
                {"limit": limit},
            )
        ).all()
        return [_row(r) for r in rows]

    async def expire_due(self, limit: int = 100) -> list[ApprovalRow]:
        """Apply `on_expiry` to every approval that has run out of chain.

        Denials on expiry are counted and reported (§2): a run blocked by an
        unanswered approval is a real signal about operating cost, so these rows are
        settled rather than left pending forever where nobody would count them.

        `escalate` is absent from the CASE deliberately. By the time a row reaches
        here it has no chain left, so "escalate" would mean "escalate to nobody" — and
        the honest reading of that is a denial. It is recorded as such, with the
        original `on_expiry` in the note, so the dashboard can tell an approval that
        was denied on purpose from one that ran out of people.
        """
        rows = (
            await self._s.execute(
                text(
                    """
                    UPDATE approvals
                       SET status = CASE WHEN on_expiry = 'grant' THEN 'GRANTED' ELSE 'EXPIRED' END,
                           decided_at = now(),
                           decided_by = 'system:ttl',
                           decision_note = 'TTL elapsed with on_expiry=' || on_expiry
                             || CASE WHEN escalation_count > 0
                                     THEN ' after ' || escalation_count || ' escalation(s)'
                                     ELSE '' END
                     WHERE id IN (
                        SELECT id FROM approvals
                         WHERE status = 'PENDING' AND expires_at < now()
                           AND (escalation_index >= max_escalations
                             OR jsonb_array_length(escalation_chain) <= escalation_index + 1)
                         ORDER BY expires_at LIMIT :limit
                         FOR UPDATE SKIP LOCKED
                     )
                    """
                    + _RETURNING
                ),
                {"limit": limit},
            )
        ).all()
        return [_row(r) for r in rows]

    async def pending(
        self, organization_id: uuid.UUID, limit: int = 50, *, include_deferred: bool = False
    ) -> list[ApprovalRow]:
        """The approver's queue.

        Deferred items are excluded by default and that is the enforcement point for
        edge case 31 — the daily budget shortens what a person is shown, not what the
        system is waiting on. `include_deferred=True` is for the dashboard, which has
        to report the blocked runs the queue is hiding.
        """
        clause = (
            "" if include_deferred else " AND (deferred_until IS NULL OR deferred_until <= now())"
        )
        rows = (
            await self._s.execute(
                text(
                    _SELECT
                    + " WHERE organization_id = :org AND status = 'PENDING'"
                    + clause
                    + " ORDER BY expires_at LIMIT :limit"
                ),
                {"org": organization_id, "limit": limit},
            )
        ).all()
        return [_row(r) for r in rows]

    async def by_resume_token(self, token: str) -> ApprovalRow | None:
        r = (
            await self._s.execute(text(_SELECT + " WHERE resume_token = :token"), {"token": token})
        ).one_or_none()
        return _row(r) if r is not None else None

    async def spend_resume_token(self, token: str, *, run_id: uuid.UUID) -> bool:
        """Mark a resume token used. False if it was already spent.

        Conditional on `resume_token_used_at IS NULL`, so a replayed resume loses the
        race rather than re-granting. Single-use is what stops one approval from
        authorising an unbounded number of resumptions.
        """
        row = (
            await self._s.execute(
                text(
                    """
                    UPDATE approvals
                       SET resume_token_used_at = now(), resume_run_id = :run
                     WHERE resume_token = :token AND resume_token_used_at IS NULL
                    RETURNING id
                    """
                ),
                {"token": token, "run": run_id},
            )
        ).one_or_none()
        return row is not None

    async def defer(self, approval_id: ApprovalId, until: dt.datetime) -> None:
        await self._s.execute(
            text("UPDATE approvals SET deferred_until = :until WHERE id = :id"),
            {"id": approval_id, "until": until},
        )

    # --- approver budgets -------------------------------------------------------

    async def charge_approver(
        self,
        budget_id: uuid.UUID,
        organization_id: uuid.UUID,
        *,
        approver: str,
        day: dt.date,
        daily_limit: int,
    ) -> tuple[int, bool]:
        """Count one assignment against an approver's day. `(assigned, over_budget)`.

        One statement, because two — read then increment — is how two concurrent
        assignments both see 9 of 10 and both proceed. The row is created on demand
        with the limit the caller resolved from the role, and an existing row keeps
        its own limit so that lowering a role's budget does not retroactively rewrite
        a day already in progress.
        """
        row = (
            await self._s.execute(
                text(
                    """
                    INSERT INTO approver_budgets (id, organization_id, approver, day,
                                                  daily_limit, assigned)
                    VALUES (:id, :org, :approver, :day, :limit, 1)
                    ON CONFLICT ON CONSTRAINT uq_approver_day DO UPDATE
                        SET assigned = approver_budgets.assigned + 1
                    RETURNING assigned, daily_limit
                    """
                ),
                {
                    "id": budget_id,
                    "org": organization_id,
                    "approver": approver,
                    "day": day,
                    "limit": daily_limit,
                },
            )
        ).one()
        return int(row.assigned), int(row.assigned) > int(row.daily_limit)

    async def mark_alerted(self, organization_id: uuid.UUID, approver: str, day: dt.date) -> bool:
        """Stamp the alert. False if it had already fired today.

        One alert per approver per day. An alert that fires on every subsequent
        assignment trains people to filter it, which turns the signal M2 §10 asks you
        to watch from day one into noise by day two.
        """
        row = (
            await self._s.execute(
                text(
                    """
                    UPDATE approver_budgets SET alerted_at = now()
                     WHERE organization_id = :org AND approver = :approver AND day = :day
                       AND alerted_at IS NULL
                    RETURNING id
                    """
                ),
                {"org": organization_id, "approver": approver, "day": day},
            )
        ).one_or_none()
        return row is not None

    async def approver_load(self, organization_id: uuid.UUID, day: dt.date) -> list[dict[str, Any]]:
        rows = (
            await self._s.execute(
                text(
                    """
                    SELECT approver, assigned, daily_limit, alerted_at
                      FROM approver_budgets
                     WHERE organization_id = :org AND day = :day
                     ORDER BY assigned DESC
                    """
                ),
                {"org": organization_id, "day": day},
            )
        ).all()
        return [
            {
                "approver": r.approver,
                "assigned": int(r.assigned),
                "daily_limit": int(r.daily_limit),
                "alerted": r.alerted_at is not None,
            }
            for r in rows
        ]

    async def count_by_status(self, organization_id: uuid.UUID) -> dict[str, int]:
        rows = (
            await self._s.execute(
                text(
                    "SELECT status, count(*) AS n FROM approvals "
                    "WHERE organization_id = :org GROUP BY status"
                ),
                {"org": organization_id},
            )
        ).all()
        return {r.status: int(r.n) for r in rows}
