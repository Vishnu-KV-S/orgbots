"""T22, T23 — the approval gate.

Both tests are about a decision that has already been made. T22: the TTL elapsed
and nobody answered, which must be a *counted* denial rather than a request that
sits pending forever looking like nothing happened. T23: two decisions arrived,
which must resolve to the first one with the second recorded rather than lost.

The last section drives the gate through `ToolGateway.execute()` rather than
through `ApprovalService` directly. That matters: the gate is enforced in the
gateway precisely so a graph that forgot its gate node cannot publish, and a test
that only exercised the service would be testing the half a careless graph bypasses.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text

from runtime.artifacts.store import ArtifactStore
from runtime.budget.service import BudgetService
from runtime.domain.authority import ActionAuthority, ResolvedAuthority
from runtime.domain.context import Lease, RunContext
from runtime.domain.enums import ActorKind, ApprovalStatus, AuthorityLevel, OnExpiry
from runtime.domain.errors import ApprovalDenied, ApprovalPending
from runtime.domain.ids import (
    ActorId,
    Fence,
    OrganizationId,
    TaskId,
    new_run_id,
    new_worker_id,
)
from runtime.domain.specs import Ceilings, CompiledSpec, ModelProfiles, RunSpec
from runtime.effects.journal import EffectJournal
from runtime.gateway.builtin import build_registry
from runtime.gateway.tools import ToolCall, ToolGateway
from runtime.org.approvals import ApprovalService, approval_id_for
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.runtime.bootstrap import Registrar
from runtime.settings import Settings

pytestmark = pytest.mark.integration

ACTION = "publish_external"
SUBJECT = "task"

HUMAN_PUBLISH = ActionAuthority(
    action=ACTION,
    level=AuthorityLevel.HUMAN,
    approver_chain=("director", "operator"),
    # Zero escalations, deliberately, even though the chain has two names. These
    # tests are about expiry and first-writer-wins, and with escalation enabled the
    # sweeper would escalate the row rather than expiring it — which is correct M2
    # behaviour and would make eight tests about something else fail. T33 in
    # `test_m2_governance.py` is where the chain actually gets walked.
    max_escalations=0,
    ttl_seconds=24 * 60 * 60,
    on_expiry=OnExpiry.DENY,
    source="department:marketing",
)
"""What M1's one dict entry became: a resolved value, passed in rather than looked up.

These tests construct it directly instead of seeding the tables, because what they
exercise is the *approval machinery* — first-writer-wins, TTL, the gateway gate — and
routing every one of them through a resolver would make a resolver bug fail fifteen
tests that are not about resolution. `test_m2_governance.py` covers the resolver.
"""

AUTO_NOTE = ActionAuthority(action="send_internal_note", level=AuthorityLevel.AUTO)


@pytest_asyncio.fixture
async def org(uow_factory: UnitOfWorkFactory) -> AsyncIterator[OrganizationId]:
    organization_id = OrganizationId(uuid.uuid4())
    await Registrar(uow_factory).ensure_organization(organization_id, "acme")
    yield organization_id


@pytest_asyncio.fixture
async def approvals(uow_factory: UnitOfWorkFactory) -> AsyncIterator[ApprovalService]:
    yield ApprovalService(uow_factory)


# --- the authority model itself -----------------------------------------------------


def test_m2_inverted_m1s_default_from_auto_to_denied() -> None:
    """M1's resolver was a dict that answered AUTO for anything unlisted. M2's
    answers DENIED.

    This test replaces `test_the_whole_m1_authority_model_is_one_entry`, which
    asserted that adding a second gate would be a deliberate change. M2 §3 *is* that
    deliberate change, and the property worth pinning now is the inversion: an action
    nobody wrote a policy for is an action nobody decided was safe.
    """
    authority = ResolvedAuthority(actor_name="marketing-head", actions={ACTION: HUMAN_PUBLISH})

    assert authority.gated_actions() == {ACTION}
    assert authority.for_action(ACTION).level is AuthorityLevel.HUMAN
    assert authority.for_action(ACTION).approver == "director"
    assert authority.for_action(ACTION).ttl_seconds == 24 * 60 * 60

    unlisted = authority.for_action("anything_else")
    assert unlisted.level is AuthorityLevel.DENIED, "M1 said AUTO here; M2 says no"
    assert unlisted.needs_human is False, "denied is not 'ask a person' — there is nobody to ask"


async def test_requesting_the_same_approval_twice_produces_one_thing_to_answer(
    approvals: ApprovalService, org: OrganizationId
) -> None:
    task_id = str(uuid.uuid4())
    first = await approvals.require(
        organization_id=org, authority=HUMAN_PUBLISH, subject_type=SUBJECT, subject_id=task_id
    )
    second = await approvals.require(
        organization_id=org, authority=HUMAN_PUBLISH, subject_type=SUBJECT, subject_id=task_id
    )
    assert first is not None and second is not None
    assert first.id == second.id == approval_id_for(SUBJECT, task_id, ACTION)
    assert second.status is ApprovalStatus.PENDING


async def test_an_auto_action_creates_no_approval_at_all(
    approvals: ApprovalService, org: OrganizationId
) -> None:
    row = await approvals.require(
        organization_id=org,
        authority=AUTO_NOTE,
        subject_type=SUBJECT,
        subject_id=str(uuid.uuid4()),
    )
    assert AUTO_NOTE.level is AuthorityLevel.AUTO
    assert row is None, "nothing to wait for means nothing to record"


# --- T22 -----------------------------------------------------------------------------


async def test_t22_an_expired_approval_denies_and_is_counted(
    approvals: ApprovalService, org: OrganizationId, uow_factory: UnitOfWorkFactory
) -> None:
    """A run blocked by an unanswered approval is a real signal about operating
    cost (§2), so the row is settled and countable — not left pending where it
    would look like nothing happened."""
    task_id = str(uuid.uuid4())
    row = await approvals.require(
        organization_id=org, authority=HUMAN_PUBLISH, subject_type=SUBJECT, subject_id=task_id
    )
    assert row is not None
    async with uow_factory.transaction() as uow:
        await uow.session.execute(
            text("UPDATE approvals SET expires_at = now() - interval '1 minute' WHERE id = :id"),
            {"id": row.id},
        )

    expired = await approvals.expire_due()

    assert [r.id for r in expired] == [row.id]
    assert expired[0].status is ApprovalStatus.EXPIRED
    assert expired[0].permits is False, "on_expiry = deny"
    assert expired[0].decided_by == "system:ttl"

    counts = await approvals.counts(org)
    assert counts == {ApprovalStatus.EXPIRED.value: 1}, "counted, not vanished"

    async with uow_factory() as uow:
        audited = (
            await uow.session.execute(
                text("SELECT severity, outcome FROM audit_log WHERE action = 'approval.expired'")
            )
        ).all()
    assert [(a.severity, a.outcome) for a in audited] == [("high", "EXPIRED")]


async def test_an_expired_approval_is_terminal(
    approvals: ApprovalService, org: OrganizationId, uow_factory: UnitOfWorkFactory
) -> None:
    """Answering after the TTL does not resurrect it. Otherwise "we denied it by
    not answering" and "we granted it late" would be the same state."""
    task_id = str(uuid.uuid4())
    row = await approvals.require(
        organization_id=org, authority=HUMAN_PUBLISH, subject_type=SUBJECT, subject_id=task_id
    )
    assert row is not None
    async with uow_factory.transaction() as uow:
        await uow.session.execute(
            text("UPDATE approvals SET expires_at = now() - interval '1 minute' WHERE id = :id"),
            {"id": row.id},
        )
    await approvals.expire_due()

    won, current = await approvals.decide(row.id, granted=True, decided_by="operator")

    assert won is False
    assert current is not None and current.status is ApprovalStatus.EXPIRED


# --- T23 -----------------------------------------------------------------------------


async def test_t23_two_decisions_resolve_to_the_first_and_record_the_second(
    approvals: ApprovalService, org: OrganizationId, uow_factory: UnitOfWorkFactory
) -> None:
    """First writer wins; the loser is remembered.

    A decision that vanished is how two operators come to believe different things
    about the same gate.
    """
    task_id = str(uuid.uuid4())
    row = await approvals.require(
        organization_id=org, authority=HUMAN_PUBLISH, subject_type=SUBJECT, subject_id=task_id
    )
    assert row is not None

    first_won, _ = await approvals.decide(row.id, granted=True, decided_by="alex", note="ship it")
    second_won, current = await approvals.decide(
        row.id, granted=False, decided_by="sam", note="actually no"
    )

    assert first_won is True
    assert second_won is False
    assert current is not None
    assert current.status is ApprovalStatus.GRANTED
    assert current.decided_by == "alex"
    assert current.decision_note == "ship it"

    async with uow_factory() as uow:
        decisions = (
            await uow.session.execute(
                text(
                    "SELECT outcome, detail FROM audit_log "
                    "WHERE action = 'approval.decided' ORDER BY id"
                )
            )
        ).all()
    assert [d.outcome for d in decisions] == ["applied", "superseded"]
    assert decisions[1].detail["decided_by"] == "sam"
    assert decisions[1].detail["attempted"] == "DENIED"
    assert decisions[1].detail["standing"] == "GRANTED", "and what actually stands"


async def test_racing_decisions_produce_exactly_one_winner(
    approvals: ApprovalService, org: OrganizationId
) -> None:
    import asyncio

    task_id = str(uuid.uuid4())
    row = await approvals.require(
        organization_id=org, authority=HUMAN_PUBLISH, subject_type=SUBJECT, subject_id=task_id
    )
    assert row is not None

    results = await asyncio.gather(
        *(approvals.decide(row.id, granted=i % 2 == 0, decided_by=f"person-{i}") for i in range(6))
    )
    assert sum(1 for won, _ in results if won) == 1


async def test_deciding_an_approval_on_a_task_marks_it_human_touched(
    approvals: ApprovalService, org: OrganizationId, uow_factory: UnitOfWorkFactory
) -> None:
    """Whichever way they decided, a person was involved — the unassisted-completion
    rate should say so."""
    from runtime.domain.outputs import CONTENT_DRAFT_V1

    task_id = uuid.uuid4()
    async with uow_factory.transaction() as uow:
        await uow.tasks.create(
            {
                "id": task_id,
                "organization_id": org,
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
                "status": "SUBMITTED",
                "due_at": None,
                "eval_deadline": None,
            }
        )
    row = await approvals.require(
        organization_id=org, authority=HUMAN_PUBLISH, subject_type=SUBJECT, subject_id=str(task_id)
    )
    assert row is not None

    await approvals.decide(row.id, granted=True, decided_by="operator")

    async with uow_factory() as uow:
        task = await uow.tasks.get(TaskId(task_id))
    assert task is not None and task.human_touched is True


# --- through the gateway, which is where it is actually enforced ---------------------


def _ctx(org: OrganizationId, task_id: TaskId | None) -> RunContext:
    run_id = new_run_id()
    compiled = CompiledSpec(
        actor_id=ActorId(uuid.uuid4()),
        actor_name="marketing-head",
        actor_version=1,
        kind=ActorKind.LLM_AGENT,
        graph_ref="marketing_head@1",
        handler_ref=None,
        allowed_tools=frozenset({"publish.external@1"}),
        ceilings=Ceilings(),
        model_profiles=ModelProfiles(),
        allowed_model_call_sites=None,
        # M2: authority is frozen into the spec at admission, and the gateway reads
        # it from there. A spec with none resolves every gated action to the
        # default, which denies — so a test that wants the gate to *open* has to say
        # who may open it.
        authority=ResolvedAuthority(
            actor_name="marketing-head",
            role="head",
            department="marketing",
            actions={ACTION: HUMAN_PUBLISH},
        ),
    )
    spec = RunSpec(
        run_id=run_id,
        organization_id=org,
        root_run_id=run_id,
        task_id=task_id,
        correlation_id=str(uuid.uuid4()),
        thread_id=str(run_id),
        spec=compiled,
        spec_hash="x" * 64,
        input={},
    )
    return RunContext(
        run_id=run_id,
        organization_id=org,
        root_run_id=run_id,
        actor_id=compiled.actor_id,
        actor_version=1,
        spec=spec,
        lease=Lease(
            run_id=run_id,
            worker_id=new_worker_id(),
            fence=Fence(1),
            lease_until=dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5),
        ),
        worker_id=new_worker_id(),
        trace_id="0" * 32,
    )


def _gateway(uow_factory: UnitOfWorkFactory, settings: Settings) -> ToolGateway:
    return ToolGateway(
        build_registry(uow_factory, settings),
        EffectJournal(uow_factory),
        uow_factory,
        artifacts=ArtifactStore(uow_factory, settings=settings),
        budget=BudgetService(),
        settings=settings,
    )


PUBLISH_ARGS: dict[str, Any] = {
    "title": "The number nobody publishes",
    "body_markdown": "x" * 400,
    "channel": "blog",
    "task_id": "",
}


async def test_an_irreversible_tool_without_an_approval_raises_pending_and_creates_one(
    org: OrganizationId, uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """The gateway is the enforcement point, so a graph that forgot its gate node
    still cannot publish — and the request it needs is created for it."""
    task_id = TaskId(uuid.uuid4())
    ctx = _ctx(org, task_id)
    gateway = _gateway(uow_factory, settings)

    with pytest.raises(ApprovalPending, match="waiting on director"):
        await gateway.execute(
            ctx,
            ToolCall(tool="publish.external@1", args={**PUBLISH_ARGS, "task_id": str(task_id)}),
        )

    async with uow_factory() as uow:
        row = await uow.approvals.find(SUBJECT, str(task_id), ACTION)
        effects = (
            await uow.session.execute(text("SELECT count(*) FROM effect_intents"))
        ).scalar_one()
    assert row is not None and row.status is ApprovalStatus.PENDING
    assert row.requested_by_actor == "marketing-head"
    assert effects == 0, "a refused call leaves no INTENT row behind"


async def test_a_denied_approval_raises_denied_not_pending(
    org: OrganizationId, uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """The distinction is load-bearing: pending is "waiting on a person", denied is
    "the answer was no". Only one of them is worth trying again later."""
    task_id = TaskId(uuid.uuid4())
    ctx = _ctx(org, task_id)
    gateway = _gateway(uow_factory, settings)
    service = ApprovalService(uow_factory)

    with pytest.raises(ApprovalPending):
        await gateway.execute(
            ctx,
            ToolCall(tool="publish.external@1", args={**PUBLISH_ARGS, "task_id": str(task_id)}),
        )
    await service.decide(
        approval_id_for(SUBJECT, str(task_id), ACTION),
        granted=False,
        decided_by="operator",
        note="the sourcing is not good enough",
    )

    with pytest.raises(ApprovalDenied, match="DENIED by operator"):
        await gateway.execute(
            _ctx(org, task_id),
            ToolCall(tool="publish.external@1", args={**PUBLISH_ARGS, "task_id": str(task_id)}),
        )


async def test_a_granted_approval_lets_the_call_through_to_the_tool(
    org: OrganizationId, uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """Past the gate, the tool's own configuration refusal is what stops it.

    `publish.external@1` is unconfigured in tests, so it raises `SpecError` — which
    is the proof the call reached the tool body rather than being turned away at
    the gate. M0's blanket "requires_approval means it cannot run" is genuinely gone.
    """
    from runtime.domain.errors import SpecError

    task_id = TaskId(uuid.uuid4())
    gateway = _gateway(uow_factory, settings)
    service = ApprovalService(uow_factory)

    with pytest.raises(ApprovalPending):
        await gateway.execute(
            _ctx(org, task_id),
            ToolCall(tool="publish.external@1", args={**PUBLISH_ARGS, "task_id": str(task_id)}),
        )
    won, _ = await service.decide(
        approval_id_for(SUBJECT, str(task_id), ACTION),
        granted=True,
        decided_by="operator",
    )
    assert won

    with pytest.raises(SpecError, match="not configured"):
        await gateway.execute(
            _ctx(org, task_id),
            ToolCall(tool="publish.external@1", args={**PUBLISH_ARGS, "task_id": str(task_id)}),
        )


async def test_the_approval_survives_the_run_that_asked_for_it(
    org: OrganizationId, uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """Keyed on the task, not the run.

    A run that resumes after a human decision is a *different* run with a different
    id, so anything run-scoped — `logical_call_id` included, since it contains the
    run id by construction — would lose the approval a moment after it was granted.
    """
    task_id = TaskId(uuid.uuid4())
    gateway = _gateway(uow_factory, settings)
    asking_ctx = _ctx(org, task_id)

    with pytest.raises(ApprovalPending):
        await gateway.execute(
            asking_ctx,
            ToolCall(tool="publish.external@1", args={**PUBLISH_ARGS, "task_id": str(task_id)}),
        )
    await ApprovalService(uow_factory).decide(
        approval_id_for(SUBJECT, str(task_id), ACTION), granted=True, decided_by="operator"
    )

    resuming_ctx = _ctx(org, task_id)
    assert resuming_ctx.run_id != asking_ctx.run_id

    async with uow_factory() as uow:
        row = await uow.approvals.find(SUBJECT, str(resuming_ctx.task_id), ACTION)
    assert row is not None and row.permits, "the grant is still found by the new run"


async def test_a_tool_requiring_approval_with_no_authority_action_is_refused(
    org: OrganizationId, uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """M0's behaviour, kept for exactly the case M0 was in: there is no subject to
    ask about, so the answer is no."""
    from pydantic import BaseModel

    from runtime.domain.enums import BlastRadius, RecoveryPolicy
    from runtime.domain.errors import ApprovalRequired
    from runtime.gateway.tools import EffectCapabilities, ToolDef

    class Args(BaseModel):
        pass

    class Result(BaseModel):
        done: bool = True

    async def never_runs(ctx: Any, args: Any) -> Result:  # pragma: no cover
        return Result()

    gateway = _gateway(uow_factory, settings)
    gateway.registry.register(
        ToolDef(
            name="nameless.irreversible",
            version=1,
            args_model=Args,
            result_model=Result,
            capabilities=EffectCapabilities(
                mutates_external_state=True,
                accepts_idempotency_key=True,
                idempotency_key_field="key",
                max_blast_radius=BlastRadius.IRREVERSIBLE,
            ),
            recovery_policy=RecoveryPolicy.IDEMPOTENCY_KEY,
        ),
        never_runs,
    )
    ctx = _ctx(org, TaskId(uuid.uuid4()))
    object.__setattr__(ctx.spec.spec, "allowed_tools", frozenset({"nameless.irreversible@1"}))

    with pytest.raises(ApprovalRequired, match="no authority_action"):
        await gateway.execute(ctx, ToolCall(tool="nameless.irreversible@1", args={}))


def test_the_publish_tool_declares_the_gated_action() -> None:
    """A new publish tool must resolve the same action, or "add a tool" becomes a
    way around the gate."""
    from runtime.gateway.builtin.publish_external import AUTHORITY_ACTION, build

    definition, _ = build(Settings())
    assert definition.authority_action == AUTHORITY_ACTION == ACTION
    assert definition.requires_approval is True
    assert definition.max_retries == 0, "IRREVERSIBLE means no retries"
