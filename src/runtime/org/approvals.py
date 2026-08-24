"""The approval gate.

M1 shipped one gate: one approver, a 24h TTL, `on_expiry = deny`, no escalation. M2
keeps the shape and fills in the policy, and the shape is worth restating because
everything else depends on it.

**A run does not wait.** A pending approval can take a day; a run holds a lease
measured in seconds and a worker slot the whole time. So the gate is asynchronous:
the run that hits it records the request and ends. When a decision arrives, the
decision sends an inbox message, the dispatcher starts a fresh run, and the work
resumes from the task rather than from the middle of a graph.

That is why approvals are keyed on `(subject_type, subject_id, action)` — the task —
and not on `logical_call_id`, which contains the run id and would therefore be a
different value in the run that resumes.

**What M2 adds, and why each one is not optional at real scale:**

*Escalation.* A chain resolved from the authority the run was admitted under, frozen
onto the row, walked by the sweeper. Without it, `on_expiry` fires on the first
person who was in a meeting.

*Per-approver daily budgets.* Exceeding one raises an alert and **defers** — the run
stays blocked, the queue does not grow. M2 §5: a 40-item approval queue produces
rubber-stamping, which is worse than a blocked run because it looks like oversight
while being its absence. The temptation this resists is making the limit a threshold
somebody raises; §10 says treat it firing as a design problem.

*Resume tokens bound to an interrupt.* An approval may resume the pause it was
created for and no other, even in the same run (edge case 28).

*The approver sees the rendered action.* `detail['rendered']` is built by the runtime
from the actual tool arguments — the actual recipients, the actual amount, the actual
diff — never a model-written summary of what it intends to do. A summary is the
model's account of itself, and that is precisely the thing under review.
"""

from __future__ import annotations

import datetime as dt
import secrets
import uuid

from runtime.domain.authority import ActionAuthority
from runtime.domain.enums import (
    ApprovalStatus,
    AuditSeverity,
    OnExpiry,
    RejectionReason,
    TaskOutcome,
    TaskStatus,
)
from runtime.domain.errors import ResumeTokenInvalid
from runtime.domain.ids import ApprovalId, CorrelationId, OrganizationId, RunId
from runtime.observability.logging import get_logger
from runtime.persistence.repositories.approvals import ApprovalRow
from runtime.persistence.uow import UnitOfWork, UnitOfWorkFactory

log = get_logger("org.approvals")

SUBJECT_TASK = "task"

TOPIC_APPROVAL_ALERT = "approval.budget_exceeded"
TOPIC_APPROVAL_FAIL_RUN = "approval.fail_run"


def approval_id_for(subject_type: str, subject_id: str, action: str) -> ApprovalId:
    """Derive the id so a replayed gate node re-requests the same approval.

    A `uuid4()` here would still be deduped by `uq_approval_subject`, but the
    losing insert would burn an id and the caller would have to re-read to find the
    real one. Deriving it makes the request idempotent all the way down.
    """
    name = f"approval:{subject_type}:{subject_id}:{action}"
    return ApprovalId(uuid.uuid5(uuid.NAMESPACE_URL, name))


def new_resume_token() -> str:
    """Random, never derived.

    The contrast with `approval_id_for` above is deliberate. An id may be derivable —
    knowing it grants nothing. A resume token *is* the authority to resume, so
    anything that can recompute it from public facts can forge it.
    """
    return secrets.token_urlsafe(32)


def _next_midnight(now: dt.datetime) -> dt.datetime:
    return (now + dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


class ApprovalService:
    def __init__(self, uow_factory: UnitOfWorkFactory) -> None:
        self._uow = uow_factory

    async def require(
        self,
        *,
        organization_id: OrganizationId,
        authority: ActionAuthority,
        subject_type: str,
        subject_id: str,
        detail: dict[str, object] | None = None,
        run_id: RunId | None = None,
        actor_name: str | None = None,
        correlation_id: CorrelationId | None = None,
        interrupt_id: str | None = None,
        approver_daily_budget: int | None = None,
        now: dt.datetime | None = None,
    ) -> ApprovalRow | None:
        """Ensure the approval exists. `None` means the action is AUTO — nothing to wait for.

        The caller inspects `row.status`: GRANTED proceeds, PENDING stops and is *not*
        an error, DENIED/EXPIRED stops and is.

        The approver's daily budget is charged **only on creation**. A gate node that
        replays — which happens on every resumed run — would otherwise spend a
        person's whole day of oversight budget on one approval.
        """
        if not authority.needs_human:
            return None

        moment = now or dt.datetime.now(dt.UTC)
        expires = moment + dt.timedelta(seconds=authority.ttl_seconds)
        approver = authority.approver or "operator"

        async with self._uow.transaction() as uow:
            row, created = await uow.approvals.request(
                approval_id_for(subject_type, subject_id, authority.action),
                organization_id=organization_id,
                subject_type=subject_type,
                subject_id=subject_id,
                action=authority.action,
                approver=approver,
                expires_at=expires,
                detail=dict(detail or {}),
                requested_by_run_id=run_id,
                requested_by_actor=actor_name,
                correlation_id=correlation_id,
                on_expiry=authority.on_expiry.value,
                escalation_chain=list(authority.approver_chain),
                max_escalations=authority.max_escalations,
                ttl_seconds=authority.ttl_seconds,
                resume_token=new_resume_token(),
                interrupt_id=interrupt_id,
            )

            if created and approver_daily_budget is not None:
                row = await self._charge_approver(
                    uow,
                    row,
                    organization_id=organization_id,
                    approver=approver,
                    daily_limit=approver_daily_budget,
                    now=moment,
                )

            await uow.audit.record(
                organization_id=organization_id,
                run_id=run_id,
                root_run_id=run_id,
                action="approval.required",
                target=f"{authority.action}:{subject_id}",
                severity=AuditSeverity.HIGH,
                outcome=row.status.value,
                detail={
                    "approval_id": str(row.id),
                    "approver": row.approver,
                    "chain": row.escalation_chain,
                    "expires_at": row.expires_at.isoformat(),
                    "deferred": not row.queued,
                    "authority_source": authority.source,
                },
            )
        log.info(
            "approval.required",
            action=authority.action,
            subject=subject_id,
            status=row.status.value,
            approver=row.approver,
            queued=row.queued,
            approval_id=str(row.id),
        )
        return row

    async def _charge_approver(
        self,
        uow: UnitOfWork,
        row: ApprovalRow,
        *,
        organization_id: OrganizationId,
        approver: str,
        daily_limit: int,
        now: dt.datetime,
    ) -> ApprovalRow:
        """Count the assignment; defer and alert if the approver is over budget.

        Deferring rather than denying is the point. The work is still blocked and the
        approval is still pending — what changes is that it is not in anybody's queue
        today, so the queue stays a length a person can actually read.
        """
        day = now.date()
        assigned, over = await uow.approvals.charge_approver(
            uuid.uuid5(uuid.NAMESPACE_URL, f"approver_budget:{organization_id}:{approver}:{day}"),
            organization_id,
            approver=approver,
            day=day,
            daily_limit=daily_limit,
        )
        if not over:
            return row

        until = _next_midnight(now)
        await uow.approvals.defer(row.id, until)
        first_alert = await uow.approvals.mark_alerted(organization_id, approver, day)
        if first_alert:
            await uow.outbox.enqueue(
                organization_id=organization_id,
                topic=TOPIC_APPROVAL_ALERT,
                payload={
                    "approver": approver,
                    "assigned": assigned,
                    "daily_limit": daily_limit,
                    "day": day.isoformat(),
                },
                dedupe_key=f"approver-budget:{organization_id}:{approver}:{day}",
            )
            await uow.audit.record(
                organization_id=organization_id,
                action="approval.budget_exceeded",
                target=approver,
                severity=AuditSeverity.HIGH,
                outcome="alerted",
                detail={"assigned": assigned, "daily_limit": daily_limit},
            )
            log.warning(
                "approval.budget_exceeded",
                approver=approver,
                assigned=assigned,
                daily_limit=daily_limit,
                note="queue not lengthened; item deferred to tomorrow",
            )
        found = await uow.approvals.get(row.id)
        return found or row

    async def find(self, action: str, subject_type: str, subject_id: str) -> ApprovalRow | None:
        async with self._uow() as uow:
            return await uow.approvals.find(subject_type, subject_id, action)

    async def decide(
        self,
        approval_id: ApprovalId,
        *,
        granted: bool,
        decided_by: str,
        note: str | None = None,
    ) -> tuple[bool, ApprovalRow | None]:
        """Record a human decision. `(won, row)`.

        `won is False` means somebody — or the TTL sweeper — got there first. The
        losing decision is written to `audit_log` rather than dropped: T23 asks for
        "first wins, second recorded", and a decision that vanishes is how two
        operators come to believe different things about the same gate.

        **A decision on a terminal subject is refused** (I6, edge case 27). If the
        task was cancelled or closed while the approval sat in a queue, granting it
        now would authorise an action against work that no longer exists — and the
        run that would carry it out would be started by the grant itself. The refusal
        is recorded, because "I approved it and nothing happened" is otherwise an
        unanswerable support question.
        """
        status = ApprovalStatus.GRANTED if granted else ApprovalStatus.DENIED
        async with self._uow.transaction() as uow:
            row = await uow.approvals.get(approval_id)
            if row is None:
                return False, None

            terminal = await self._subject_is_terminal(uow, row)
            if terminal is not None:
                await uow.audit.record(
                    organization_id=row.organization_id,
                    action="approval.decided",
                    target=f"{row.action}:{row.subject_id}",
                    severity=AuditSeverity.HIGH,
                    outcome="refused_terminal_subject",
                    detail={
                        "approval_id": str(approval_id),
                        "attempted": status.value,
                        "decided_by": decided_by,
                        "subject_status": terminal,
                    },
                )
                log.warning(
                    "approval.refused_terminal",
                    approval_id=str(approval_id),
                    subject=row.subject_id,
                    subject_status=terminal,
                )
                return False, row

            won = await uow.approvals.decide(
                approval_id, status=status, decided_by=decided_by, note=note
            )
            current = await uow.approvals.get(approval_id)
            await uow.audit.record(
                organization_id=row.organization_id,
                action="approval.decided",
                target=f"{row.action}:{row.subject_id}",
                severity=AuditSeverity.HIGH,
                outcome="applied" if won else "superseded",
                detail={
                    "approval_id": str(approval_id),
                    "attempted": status.value,
                    "decided_by": decided_by,
                    "note": note,
                    "standing": current.status.value if current else None,
                    "escalations": row.escalation_count,
                },
            )
            if won and current is not None and current.subject_type == SUBJECT_TASK:
                # A human was involved in this task reaching its outcome, whichever
                # way they decided. Feeds the unassisted-completion metric.
                await uow.tasks.mark_human_touched(current.subject_id)  # type: ignore[arg-type]
        log.info(
            "approval.decided",
            approval_id=str(approval_id),
            granted=granted,
            won=won,
            decided_by=decided_by,
        )
        return won, current

    async def _subject_is_terminal(self, uow: UnitOfWork, row: ApprovalRow) -> str | None:
        """The subject's status if it is *known* to be terminal, else None.

        Two things are deliberately not treated as terminal.

        A subject this service does not model — `subject_type` of `run`, or anything a
        future caller invents — is not checked at all. Refusing decisions on subjects
        whose lifecycle we cannot read would mean silently gating on ignorance.

        A **missing** task is likewise not terminal. Absence is not evidence: a subject
        id that names no row is either a caller using task-shaped ids for something
        else, or a hard delete that is a much larger problem than this approval. In
        both cases refusing here would be answering a question we cannot see, so the
        decision proceeds and the audit row records what happened.
        """
        if row.subject_type != SUBJECT_TASK:
            return None
        try:
            task_id = uuid.UUID(row.subject_id)
        except ValueError:
            return None
        task = await uow.tasks.get(task_id)  # type: ignore[arg-type]
        if task is None:
            return None
        status = TaskStatus(task.status)
        return status.value if status.is_terminal else None

    # --- resume tokens (edge case 28) -----------------------------------------------

    async def redeem_resume_token(
        self, token: str, *, interrupt_id: str, run_id: RunId
    ) -> ApprovalRow:
        """Spend a resume token against the interrupt it was issued for.

        Four refusals, and the third is the edge case:

        - unknown token — nothing to resume;
        - already spent — a resume is single-use, so a replayed one loses;
        - **bound to a different interrupt** — an approval for this run's publish must
          not resume this run's *other* pause;
        - not granted — a token exists from the moment the approval is requested, so
          it must not open a gate nobody has answered yet.

        The bind check happens before the spend, so a mismatched attempt does not burn
        the token that the correct attempt still needs.
        """
        async with self._uow.transaction() as uow:
            row = await uow.approvals.by_resume_token(token)
            if row is None:
                raise ResumeTokenInvalid("no approval holds this resume token")
            if row.resume_token_used_at is not None:
                raise ResumeTokenInvalid(
                    f"resume token for approval {row.id} was already spent at "
                    f"{row.resume_token_used_at.isoformat()}"
                )
            if row.interrupt_id != interrupt_id:
                raise ResumeTokenInvalid(
                    f"approval {row.id} is bound to interrupt {row.interrupt_id!r} and "
                    f"cannot resume {interrupt_id!r}"
                )
            if not row.permits:
                raise ResumeTokenInvalid(
                    f"approval {row.id} is {row.status.value}; only a GRANTED approval resumes"
                )
            spent = await uow.approvals.spend_resume_token(token, run_id=run_id)
            if not spent:
                raise ResumeTokenInvalid(f"resume token for approval {row.id} was already spent")
            await uow.audit.record(
                organization_id=row.organization_id,
                run_id=run_id,
                action="approval.resumed",
                target=f"{row.action}:{row.subject_id}",
                severity=AuditSeverity.HIGH,
                outcome="applied",
                detail={"approval_id": str(row.id), "interrupt_id": interrupt_id},
            )
        return row

    # --- the sweep -------------------------------------------------------------------

    async def sweep(self) -> tuple[list[ApprovalRow], list[ApprovalRow]]:
        """Escalate what can escalate, then settle what cannot. `(escalated, expired)`.

        The order is the design. An approval that still has chain left has been
        *ignored*, not *expired*, and those want different responses. Running expiry
        first would apply `on_expiry` the first time somebody was slow, which makes
        `max_escalations` a number that never has any effect.
        """
        escalated = await self.escalate_due()
        expired = await self.expire_due()
        return escalated, expired

    async def escalate_due(self) -> list[ApprovalRow]:
        async with self._uow.transaction() as uow:
            rows = await uow.approvals.escalate_due()
            for row in rows:
                await uow.audit.record(
                    organization_id=row.organization_id,
                    action="approval.escalated",
                    target=f"{row.action}:{row.subject_id}",
                    severity=AuditSeverity.HIGH,
                    outcome="escalated",
                    detail={
                        "approval_id": str(row.id),
                        "to": row.approver,
                        "escalation": row.escalation_count,
                        "of": row.max_escalations,
                        "new_deadline": row.expires_at.isoformat(),
                    },
                )
        for row in rows:
            log.warning(
                "approval.escalated",
                approval_id=str(row.id),
                action=row.action,
                to=row.approver,
                escalation=f"{row.escalation_count}/{row.max_escalations}",
            )
        return rows

    async def expire_due(self) -> list[ApprovalRow]:
        """Settle every approval that has run out of chain. Counted, never silent.

        `fail_run` gets an event and a cancelled task. The alternative — treating it
        as a denial and letting the work carry on — is exactly what the option exists
        to prevent: for some actions, "we could not get approval so we did the rest
        anyway" is worse than stopping.
        """
        async with self._uow.transaction() as uow:
            expired = await uow.approvals.expire_due()
            for row in expired:
                await uow.audit.record(
                    organization_id=row.organization_id,
                    action="approval.expired",
                    target=f"{row.action}:{row.subject_id}",
                    severity=AuditSeverity.HIGH,
                    outcome=row.status.value,
                    detail={
                        "approval_id": str(row.id),
                        "on_expiry": row.on_expiry,
                        "escalations": row.escalation_count,
                        "waited_seconds": (row.expires_at - row.created_at).total_seconds(),
                    },
                )
                if row.on_expiry == OnExpiry.FAIL_RUN.value:
                    await self._fail_run(uow, row)
        for row in expired:
            log.warning(
                "approval.expired",
                approval_id=str(row.id),
                action=row.action,
                subject=row.subject_id,
                outcome=row.status.value,
                on_expiry=row.on_expiry,
            )
        return expired

    async def _fail_run(self, uow: UnitOfWork, row: ApprovalRow) -> None:
        await uow.outbox.enqueue(
            organization_id=row.organization_id,
            topic=TOPIC_APPROVAL_FAIL_RUN,
            payload={
                "approval_id": str(row.id),
                "action": row.action,
                "subject_type": row.subject_type,
                "subject_id": row.subject_id,
                "run_id": str(row.requested_by_run_id) if row.requested_by_run_id else None,
            },
            dedupe_key=f"approval-fail-run:{row.id}",
            run_id=row.requested_by_run_id,
        )
        if row.subject_type == SUBJECT_TASK:
            # REJECTED / CANCELLED rather than a bare status change: the task did not
            # merely stop, it stopped *because* nobody authorised it, and the
            # rejection-reason vocabulary is what §10's diagnosis reads. A task that
            # vanished with no outcome would show up in the metrics as neither
            # accepted nor rejected, which is the one thing worse than either.
            await uow.tasks.close(
                uuid.UUID(row.subject_id),  # type: ignore[arg-type]
                outcome=TaskOutcome.REJECTED,
                reason=RejectionReason.CANCELLED.value,
                require_submitted=False,
            )

    async def pending(
        self, organization_id: OrganizationId, *, include_deferred: bool = False
    ) -> list[ApprovalRow]:
        async with self._uow() as uow:
            return await uow.approvals.pending(organization_id, include_deferred=include_deferred)

    async def counts(self, organization_id: OrganizationId) -> dict[str, int]:
        async with self._uow() as uow:
            return await uow.approvals.count_by_status(organization_id)

    async def approver_load(
        self, organization_id: OrganizationId, day: dt.date | None = None
    ) -> list[dict[str, object]]:
        """What each approver has been handed today. The number §10 says to watch."""
        async with self._uow() as uow:
            return await uow.approvals.approver_load(
                organization_id, day or dt.datetime.now(dt.UTC).date()
            )


async def approval_permits(
    uow: UnitOfWork, action: str, subject_type: str, subject_id: str
) -> bool:
    """Read-only check used inside the tool gateway's pipeline.

    Takes a `UnitOfWork` rather than the factory because the gateway is already
    inside one and opening a second connection to answer "may I" would be a
    round trip per irreversible call.
    """
    row = await uow.approvals.find(subject_type, subject_id, action)
    return row is not None and row.permits
