"""M2 §5 — approvals, properly. T33, T34, T35.

M1's gate worked and was one gate. What these test is the machinery that stops it
becoming forty gates answered without reading: escalation so `on_expiry` fires on a
chain rather than on the first slow person, a per-approver daily budget that shortens
the queue rather than the wait, and resume tokens that cannot be pointed at the wrong
pause.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import text

from runtime.domain.authority import ActionAuthority
from runtime.domain.enums import ApprovalStatus, AuthorityLevel, OnExpiry, TaskOutcome
from runtime.domain.errors import ResumeTokenInvalid
from runtime.domain.ids import OrganizationId, TaskId, new_run_id
from runtime.domain.outputs import CONTENT_DRAFT_V1
from runtime.org.approvals import ApprovalService, approval_id_for
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.runtime.bootstrap import Registrar

pytestmark = pytest.mark.integration

ACTION = "publish_external"
SUBJECT = "task"

ESCALATING = ActionAuthority(
    action=ACTION,
    level=AuthorityLevel.HUMAN,
    approver_chain=("director", "operator"),
    max_escalations=1,
    ttl_seconds=3600,
    on_expiry=OnExpiry.DENY,
    source="department:marketing",
)

FAIL_RUN = ESCALATING.model_copy(update={"on_expiry": OnExpiry.FAIL_RUN})

NO_CHAIN = ActionAuthority(
    action=ACTION,
    level=AuthorityLevel.HUMAN,
    approver_chain=("operator",),
    max_escalations=0,
    ttl_seconds=3600,
)


async def _org(uow_factory: UnitOfWorkFactory) -> OrganizationId:
    organization_id = OrganizationId(uuid.uuid4())
    await Registrar(uow_factory).ensure_organization(organization_id, "acme")
    return organization_id


async def _task(
    uow_factory: UnitOfWorkFactory, organization_id: OrganizationId, *, status: str = "SUBMITTED"
) -> TaskId:
    task_id = TaskId(uuid.uuid4())
    async with uow_factory.transaction() as uow:
        await uow.tasks.create(
            {
                "id": task_id,
                "organization_id": organization_id,
                "project_id": None,
                "goal_id": None,
                "correlation_id": uuid.uuid4(),
                "parent_task_id": None,
                "title": "A draft awaiting the gate",
                "objective": "objective",
                "acceptance_criteria": [],
                "created_by_actor_id": None,
                "assignee_actor_id": None,
                "assignee_name": "content",
                "input_schema_ref": None,
                "output_schema_ref": CONTENT_DRAFT_V1,
                "input": {},
                "status": status,
                "due_at": None,
                "eval_deadline": None,
            }
        )
    return task_id


async def _expire_now(uow_factory: UnitOfWorkFactory, approval_id: uuid.UUID) -> None:
    async with uow_factory.transaction() as uow:
        await uow.session.execute(
            text("UPDATE approvals SET expires_at = now() - interval '1 minute' WHERE id = :id"),
            {"id": approval_id},
        )


# --- T33: escalation, then on_expiry at max_escalations ------------------------------


async def test_t33_an_ignored_approval_escalates_then_applies_on_expiry(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """v3 edges 25/29.

    The ordering inside the sweep is the design: an approval that still has chain left
    has been *ignored*, not *expired*. Running expiry first would apply `on_expiry` the
    first time somebody was in a meeting, and `max_escalations` would be a number that
    never had any effect.
    """
    organization_id = await _org(uow_factory)
    approvals = ApprovalService(uow_factory)
    subject = str(uuid.uuid4())

    row = await approvals.require(
        organization_id=organization_id,
        authority=ESCALATING,
        subject_type=SUBJECT,
        subject_id=subject,
    )
    assert row is not None
    assert row.approver == "director"
    assert row.escalation_chain == ["director", "operator"]
    assert row.escalation_index == 0

    # Nobody answers. The first sweep escalates rather than expiring.
    await _expire_now(uow_factory, row.id)
    escalated, expired = await approvals.sweep()

    assert [r.id for r in escalated] == [row.id]
    assert expired == [], "still had chain left — this is being ignored, not expired"
    assert escalated[0].approver == "operator"
    assert escalated[0].escalation_index == 1
    assert escalated[0].escalation_count == 1
    assert escalated[0].status is ApprovalStatus.PENDING
    assert escalated[0].expires_at > dt.datetime.now(dt.UTC), "a fresh deadline, not the old one"

    # Nobody answers again. The chain is out, so `on_expiry` applies.
    await _expire_now(uow_factory, row.id)
    escalated_2, expired_2 = await approvals.sweep()

    assert escalated_2 == [], "no chain left to escalate to"
    assert [r.id for r in expired_2] == [row.id]
    assert expired_2[0].status is ApprovalStatus.EXPIRED
    assert expired_2[0].permits is False
    assert "after 1 escalation" in (expired_2[0].decision_note or "")


async def test_an_approval_with_no_chain_expires_on_the_first_sweep(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """M1's behaviour, preserved for a policy that asks for it."""
    organization_id = await _org(uow_factory)
    approvals = ApprovalService(uow_factory)
    row = await approvals.require(
        organization_id=organization_id,
        authority=NO_CHAIN,
        subject_type=SUBJECT,
        subject_id=str(uuid.uuid4()),
    )
    assert row is not None
    await _expire_now(uow_factory, row.id)

    escalated, expired = await approvals.sweep()
    assert escalated == []
    assert [r.status for r in expired] == [ApprovalStatus.EXPIRED]


async def test_on_expiry_fail_run_cancels_the_work_rather_than_letting_it_continue(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """For some actions, "we could not get approval so we did the rest anyway" is the
    wrong outcome. `fail_run` is the option that says so, and it has to actually stop
    the work or it is a label."""
    organization_id = await _org(uow_factory)
    task_id = await _task(uow_factory, organization_id)
    approvals = ApprovalService(uow_factory)

    row = await approvals.require(
        organization_id=organization_id,
        authority=FAIL_RUN.model_copy(update={"max_escalations": 0}),
        subject_type=SUBJECT,
        subject_id=str(task_id),
        run_id=new_run_id(),
    )
    assert row is not None
    await _expire_now(uow_factory, row.id)
    await approvals.sweep()

    async with uow_factory() as uow:
        task = await uow.tasks.get(task_id)
        events = (
            await uow.session.execute(
                text("SELECT topic FROM outbox WHERE topic = 'approval.fail_run'")
            )
        ).all()
    assert task is not None
    assert task.outcome is TaskOutcome.REJECTED, (
        "the task did not merely stop, it stopped because nobody authorised it — and "
        "the metrics have to be able to say which"
    )
    assert len(events) == 1


# --- T34: resume tokens are bound to an interrupt ------------------------------------


async def test_t34_a_token_from_one_approval_cannot_resume_another_interrupt(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """v3 edge 28. Two pauses in one run are two interrupts, and an approval granted
    for the first must not resume the second."""
    organization_id = await _org(uow_factory)
    approvals = ApprovalService(uow_factory)
    run_id = new_run_id()

    first = await approvals.require(
        organization_id=organization_id,
        authority=NO_CHAIN,
        subject_type=SUBJECT,
        subject_id=str(uuid.uuid4()),
        interrupt_id="thread-1:gate:publish_external",
        run_id=run_id,
    )
    second = await approvals.require(
        organization_id=organization_id,
        authority=NO_CHAIN,
        subject_type=SUBJECT,
        subject_id=str(uuid.uuid4()),
        interrupt_id="thread-1:gate2:publish_external",
        run_id=run_id,
    )
    assert first is not None and second is not None
    assert first.resume_token != second.resume_token

    await approvals.decide(first.id, granted=True, decided_by="operator")

    with pytest.raises(ResumeTokenInvalid, match="bound to interrupt"):
        await approvals.redeem_resume_token(
            first.resume_token or "",
            interrupt_id="thread-1:gate2:publish_external",
            run_id=run_id,
        )

    redeemed = await approvals.redeem_resume_token(
        first.resume_token or "",
        interrupt_id="thread-1:gate:publish_external",
        run_id=run_id,
    )
    assert redeemed.id == first.id


async def test_a_resume_token_is_single_use(uow_factory: UnitOfWorkFactory) -> None:
    """Single-use is what stops one approval authorising unbounded resumptions."""
    organization_id = await _org(uow_factory)
    approvals = ApprovalService(uow_factory)
    run_id = new_run_id()
    row = await approvals.require(
        organization_id=organization_id,
        authority=NO_CHAIN,
        subject_type=SUBJECT,
        subject_id=str(uuid.uuid4()),
        interrupt_id="i1",
        run_id=run_id,
    )
    assert row is not None
    await approvals.decide(row.id, granted=True, decided_by="operator")

    await approvals.redeem_resume_token(row.resume_token or "", interrupt_id="i1", run_id=run_id)
    with pytest.raises(ResumeTokenInvalid, match="already spent"):
        await approvals.redeem_resume_token(
            row.resume_token or "", interrupt_id="i1", run_id=run_id
        )


async def test_an_ungranted_approval_cannot_be_resumed(uow_factory: UnitOfWorkFactory) -> None:
    """A token exists from the moment the approval is requested. It must not open a
    gate nobody has answered — otherwise holding the token *is* the approval."""
    organization_id = await _org(uow_factory)
    approvals = ApprovalService(uow_factory)
    row = await approvals.require(
        organization_id=organization_id,
        authority=NO_CHAIN,
        subject_type=SUBJECT,
        subject_id=str(uuid.uuid4()),
        interrupt_id="i1",
    )
    assert row is not None
    with pytest.raises(ResumeTokenInvalid, match="only a GRANTED approval resumes"):
        await approvals.redeem_resume_token(
            row.resume_token or "", interrupt_id="i1", run_id=new_run_id()
        )


async def test_an_unknown_token_is_refused(uow_factory: UnitOfWorkFactory) -> None:
    approvals = ApprovalService(uow_factory)
    with pytest.raises(ResumeTokenInvalid, match="no approval holds"):
        await approvals.redeem_resume_token("not-a-token", interrupt_id="i", run_id=new_run_id())


def test_resume_tokens_are_random_rather_than_derived() -> None:
    """The contrast with `approval_id_for` is the point.

    An id may be derivable — knowing it grants nothing. A resume token *is* the
    authority to resume, so anything that could recompute it from public facts could
    forge it.
    """
    from runtime.org.approvals import new_resume_token

    assert new_resume_token() != new_resume_token()
    assert approval_id_for("task", "abc", ACTION) == approval_id_for("task", "abc", ACTION)


# --- T35: the approver daily budget --------------------------------------------------


async def test_t35_exceeding_the_daily_budget_alerts_and_does_not_lengthen_the_queue(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """v3 edge 31, and M2 §5's sharpest sentence: a 40-item approval queue produces
    rubber-stamping, which is worse than a blocked run because it looks like oversight.

    So the run stays blocked — the approval is PENDING and nobody has answered — and
    the *queue* stops growing. Those are different things, and conflating them is how
    a daily limit turns into a way to lose work.
    """
    organization_id = await _org(uow_factory)
    approvals = ApprovalService(uow_factory)
    budget = 3

    rows = [
        await approvals.require(
            organization_id=organization_id,
            authority=NO_CHAIN,
            subject_type=SUBJECT,
            subject_id=str(uuid.uuid4()),
            approver_daily_budget=budget,
        )
        for _ in range(5)
    ]
    assert all(r is not None for r in rows)

    queue = await approvals.pending(organization_id)
    everything = await approvals.pending(organization_id, include_deferred=True)

    assert len(queue) == budget, "the queue is capped at the daily budget"
    assert len(everything) == 5, "and nothing was dropped — the runs are still blocked"
    assert all(r.status is ApprovalStatus.PENDING for r in everything)

    deferred = [r for r in everything if not r.queued]
    assert len(deferred) == 2
    assert all(r.deferred_until is not None for r in deferred)

    load = await approvals.approver_load(organization_id)
    assert load == [{"approver": "operator", "assigned": 5, "daily_limit": 3, "alerted": True}]

    async with uow_factory() as uow:
        alerts = (
            await uow.session.execute(
                text("SELECT count(*) FROM outbox WHERE topic = 'approval.budget_exceeded'")
            )
        ).scalar_one()
    assert alerts == 1, "one alert per approver per day — more would train people to filter it"


async def test_a_replayed_request_does_not_charge_the_budget_twice(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """A gate node replays on every resumed run. Charging per request rather than per
    *assignment* would let one approval eat a person's whole day of oversight."""
    organization_id = await _org(uow_factory)
    approvals = ApprovalService(uow_factory)
    subject = str(uuid.uuid4())

    for _ in range(4):
        await approvals.require(
            organization_id=organization_id,
            authority=NO_CHAIN,
            subject_type=SUBJECT,
            subject_id=subject,
            approver_daily_budget=2,
        )

    load = await approvals.approver_load(organization_id)
    assert load[0]["assigned"] == 1
    assert load[0]["alerted"] is False
    assert len(await approvals.pending(organization_id)) == 1


async def test_a_deferred_item_becomes_visible_the_next_day(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """Deferral is a delay, not a deletion. The work is still waiting."""
    organization_id = await _org(uow_factory)
    approvals = ApprovalService(uow_factory)
    for _ in range(2):
        await approvals.require(
            organization_id=organization_id,
            authority=NO_CHAIN,
            subject_type=SUBJECT,
            subject_id=str(uuid.uuid4()),
            approver_daily_budget=1,
        )
    assert len(await approvals.pending(organization_id)) == 1

    async with uow_factory.transaction() as uow:
        await uow.session.execute(
            text("UPDATE approvals SET deferred_until = now() - interval '1 hour'")
        )
    assert len(await approvals.pending(organization_id)) == 2


async def test_escalation_clears_a_deferral(uow_factory: UnitOfWorkFactory) -> None:
    """The new approver has their own budget; carrying the old one's deferral would
    hide the item from somebody who has room for it."""
    organization_id = await _org(uow_factory)
    approvals = ApprovalService(uow_factory)
    for _ in range(2):
        row = await approvals.require(
            organization_id=organization_id,
            authority=ESCALATING,
            subject_type=SUBJECT,
            subject_id=str(uuid.uuid4()),
            approver_daily_budget=1,
        )
    assert row is not None and not row.queued

    await _expire_now(uow_factory, row.id)
    escalated, _ = await approvals.sweep()

    assert [r.id for r in escalated] == [row.id]
    assert escalated[0].approver == "operator"
    assert escalated[0].queued, "visible to the operator it escalated to"


# --- edge case 27: decisions on a terminal subject ------------------------------------


async def test_a_decision_on_a_closed_task_is_refused_and_recorded(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """I6 / edge case 27.

    Granting an approval whose task was cancelled while it sat in a queue would
    authorise an action against work that no longer exists — and the run that would
    carry it out is started *by the grant*. The refusal is recorded, because "I
    approved it and nothing happened" is otherwise an unanswerable support question.
    """
    organization_id = await _org(uow_factory)
    task_id = await _task(uow_factory, organization_id)
    approvals = ApprovalService(uow_factory)

    row = await approvals.require(
        organization_id=organization_id,
        authority=NO_CHAIN,
        subject_type=SUBJECT,
        subject_id=str(task_id),
    )
    assert row is not None

    async with uow_factory.transaction() as uow:
        await uow.tasks.close(
            task_id, outcome=TaskOutcome.REJECTED, reason="CANCELLED", require_submitted=False
        )

    won, current = await approvals.decide(row.id, granted=True, decided_by="operator")

    assert won is False
    assert current is not None and current.status is ApprovalStatus.PENDING, "not granted"

    async with uow_factory() as uow:
        outcomes = (
            (
                await uow.session.execute(
                    text("SELECT outcome FROM audit_log WHERE action = 'approval.decided'")
                )
            )
            .scalars()
            .all()
        )
    assert outcomes == ["refused_terminal_subject"]


async def test_a_decision_on_a_live_task_is_applied(uow_factory: UnitOfWorkFactory) -> None:
    """The control for the test above — the check must not refuse everything."""
    organization_id = await _org(uow_factory)
    task_id = await _task(uow_factory, organization_id)
    approvals = ApprovalService(uow_factory)
    row = await approvals.require(
        organization_id=organization_id,
        authority=NO_CHAIN,
        subject_type=SUBJECT,
        subject_id=str(task_id),
    )
    assert row is not None

    won, current = await approvals.decide(row.id, granted=True, decided_by="operator")
    assert won is True
    assert current is not None and current.permits


# --- the rendered action (§5) ----------------------------------------------------------


async def test_the_approval_carries_the_rendered_action_not_a_summary(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """§5's last bullet. The approver reads the actual arguments, built by the runtime.

    A model-written summary is the model's account of its own intentions, and that is
    precisely the thing under review.
    """
    from runtime.gateway.tools import _render_action

    args = {
        "title": "The number nobody publishes",
        "body_markdown": "x" * 9_000,
        "channel": "production",
        "recipients": ["press@example.com", "list@example.com"],
    }
    rendered = _render_action("publish.external@1", args)

    assert rendered["_tool"] == "publish.external@1"
    assert rendered["channel"] == "production", "the *actual* channel, not a description"
    assert "press@example.com" in rendered["recipients"]
    assert "This is a truncation, not a summary" in rendered["body_markdown"], (
        "a long body is cut and says so — M1's 200-character preview is exactly how "
        "somebody approves the first paragraph and ships the rest"
    )


async def test_the_rendered_action_has_no_secret_in_it(uow_factory: UnitOfWorkFactory) -> None:
    """An approval request lands in an audit row, a CLI screen and possibly an email.
    It is a wide surface and it does not need the key."""
    from runtime.domain.scrub import registry
    from runtime.gateway.tools import _render_action

    secret = "sk-ant-api03-NOTAREALKEY0000000000000000"
    registry().register(secret, label="credential:test")
    try:
        rendered = _render_action("publish.external@1", {"auth": f"Bearer {secret}"})
        assert secret not in rendered["auth"]
        assert "[REDACTED]" in rendered["auth"]
    finally:
        registry().forget_all()
