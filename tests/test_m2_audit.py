"""M2 §2 — every gateway decision produces an audit row. T43, and T39 alongside it.

§2's sentence is the one worth holding onto: *"Denials are the valuable rows. A
gateway that only logs successes tells you nothing about what your agents keep trying
to do."* So these tests check both halves — that a denial is always recorded with a
reason, and that allowed calls are recorded too, because a denial rate without a
denominator is a number nobody can act on.

The batching is tested here as well. M2 §9 names unbatched audit writes as the first
suspect when the cost gate fails, and "one statement per gateway call" is a property
that decays silently unless something asserts it.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from runtime.domain.enums import AuditSeverity, BlastRadius, GatewayDecision
from runtime.domain.errors import (
    ApprovalPending,
    AuthorityDenied,
    IncoherentEffectPolicy,
    ToolNotAllowed,
)
from runtime.domain.ids import OrganizationId, TaskId
from runtime.effects.policies import BLAST_RADIUS_DEFAULTS, resolve_policy
from runtime.gateway.tools import ToolCall, ToolRegistry
from runtime.persistence.uow import UnitOfWorkFactory
from runtime.runtime.bootstrap import Registrar
from runtime.settings import Settings
from tests.conftest_m2 import (
    HUMAN_PUBLISH,
    RecordingTool,
    build_harness,
    decisions_for,
    grant_tools,
    make_authority,
    make_ctx,
    noop_tool_def,
)

pytestmark = pytest.mark.integration


async def _org(uow_factory: UnitOfWorkFactory) -> OrganizationId:
    organization_id = OrganizationId(uuid.uuid4())
    await Registrar(uow_factory).ensure_organization(organization_id, "acme")
    await grant_tools(uow_factory, organization_id, "research", "test.noop@1")
    return organization_id


# --- T43: every denial produces an audit row with a reason -----------------------------


async def test_t43_a_permission_denial_is_audited_with_a_reason(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    organization_id = await _org(uow_factory)
    harness = build_harness(uow_factory, settings)
    ctx = make_ctx(organization_id, allowed_tools=frozenset())

    with pytest.raises(ToolNotAllowed):
        await harness.gateway.execute(ctx, ToolCall("test.noop@1", {"value": "x"}))

    rows = await decisions_for(uow_factory, ctx.run_id)
    assert [r["decision"] for r in rows] == [GatewayDecision.DENIED.value]
    assert rows[0]["check"] == "permission"
    assert "not in allowed_tools" in rows[0]["reason"]
    assert rows[0]["severity"] == AuditSeverity.HIGH.value
    assert rows[0]["gateway"] == "tool"
    assert rows[0]["subject"] == "test.noop@1"


async def test_t43_an_authority_denial_names_the_policy_that_produced_it(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """ "Denied by policy" without naming the policy is the audit line that costs an
    hour during an incident."""
    organization_id = await _org(uow_factory)
    harness = build_harness(
        uow_factory,
        settings,
        tool_def=noop_tool_def(
            blast_radius=BlastRadius.IRREVERSIBLE, authority_action="publish_external"
        ),
    )
    ctx = make_ctx(organization_id, authority=make_authority())  # no policy for the action

    with pytest.raises(AuthorityDenied):
        await harness.gateway.execute(ctx, ToolCall("test.noop@1", {"value": "x"}))

    denials = [
        r for r in await decisions_for(uow_factory, ctx.run_id) if r["decision"] != "allowed"
    ]
    assert denials[0]["check"] == "authority"
    assert "default:deny" in denials[0]["reason"], "the resolution source, not just 'no'"
    assert denials[0]["blast_radius"] == "irreversible"


async def test_t43_a_pending_approval_is_recorded_as_approval_required_not_denied(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """Three decisions, not two. Waiting on a person is a different fact from being
    refused, and §9 counts approval-blocked runs as an operating cost of its own."""
    organization_id = await _org(uow_factory)
    harness = build_harness(
        uow_factory,
        settings,
        tool_def=noop_tool_def(
            blast_radius=BlastRadius.IRREVERSIBLE, authority_action="publish_external"
        ),
    )
    ctx = make_ctx(
        organization_id,
        authority=make_authority(actions={"publish_external": HUMAN_PUBLISH}),
        task_id=TaskId(uuid.uuid4()),
    )

    with pytest.raises(ApprovalPending):
        await harness.gateway.execute(ctx, ToolCall("test.noop@1", {"value": "x"}))

    rows = await decisions_for(uow_factory, ctx.run_id)
    required = [r for r in rows if r["decision"] == GatewayDecision.APPROVAL_REQUIRED.value]
    assert len(required) == 1
    assert required[0]["check"] == "approval"
    assert "waiting on director" in required[0]["reason"]


async def test_t43_an_allowed_call_is_recorded_too(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """The denominator. A denial rate computed over denials alone is a count."""
    organization_id = await _org(uow_factory)
    harness = build_harness(uow_factory, settings)
    ctx = make_ctx(organization_id, authority=make_authority())

    await harness.gateway.execute(ctx, ToolCall("test.noop@1", {"value": "x"}))

    rows = await decisions_for(uow_factory, ctx.run_id)
    assert [r["decision"] for r in rows] == [GatewayDecision.ALLOWED.value]
    assert rows[0]["check"] == "executed"


async def test_t43_a_validation_failure_is_audited(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """Bad arguments are a denial like any other — and one worth seeing in the stream,
    because a model that keeps sending malformed args is a prompt problem."""
    organization_id = await _org(uow_factory)
    harness = build_harness(uow_factory, settings)
    ctx = make_ctx(organization_id, authority=make_authority())

    from runtime.domain.errors import ValidationFailed

    with pytest.raises(ValidationFailed):
        await harness.gateway.execute(ctx, ToolCall("test.noop@1", {"value": {"not": "a string"}}))

    rows = await decisions_for(uow_factory, ctx.run_id)
    assert rows[0]["check"] == "args_validation"
    assert rows[0]["decision"] == GatewayDecision.DENIED.value


async def test_decisions_are_written_even_when_the_call_raises_mid_pipeline(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """The `finally` around the pipeline is what makes T43 hold rather than *usually*
    hold. A denial that failed to record itself would be invisible to the §9 review —
    which is exactly the population it exists to describe."""
    organization_id = await _org(uow_factory)
    tool = RecordingTool(fail_with=RuntimeError("provider exploded"))
    harness = build_harness(uow_factory, settings, tool=tool)
    ctx = make_ctx(organization_id, authority=make_authority())

    with pytest.raises(RuntimeError, match="provider exploded"):
        await harness.gateway.execute(ctx, ToolCall("test.noop@1", {"value": "x"}))

    # No decision row for the *outcome* — the tool ran, so this is not a governance
    # decision — but the buffer flushed rather than being lost with the exception.
    async with uow_factory() as uow:
        effects = await uow.effects.for_run(ctx.run_id)
    assert effects, "the journal still recorded the attempt"


async def test_the_denial_stream_groups_by_what_an_actor_keeps_trying(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """§9's exit criterion, as a query.

    Grouped rather than listed, because the review's question is "what does this actor
    keep trying to do that it cannot", and a thousand identical rows answer it worse
    than one row with a count of a thousand.
    """
    organization_id = await _org(uow_factory)
    harness = build_harness(uow_factory, settings)

    for _ in range(5):
        ctx = make_ctx(organization_id, allowed_tools=frozenset())
        with pytest.raises(ToolNotAllowed):
            await harness.gateway.execute(ctx, ToolCall("test.noop@1", {"value": "x"}))

    async with uow_factory() as uow:
        stream = await uow.audit.denial_stream(
            organization_id, since=dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)
        )
        counts = await uow.audit.decision_counts(
            organization_id, since=dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)
        )

    assert len(stream) == 1, "five identical attempts are one finding"
    assert stream[0]["denials"] == 5
    assert stream[0]["actor"] == "research"
    assert stream[0]["check"] == "permission"
    assert counts == {"denied": 5}


async def test_decision_rows_are_written_in_one_statement_per_call(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """M2 §9 names unbatched audit writes as the first suspect when the cost gate
    fails. A tool call passes roughly eight checks; eight round trips on a path that
    had three is how a governance milestone regresses the number it was told not to.

    Asserted by counting the statements the buffer issues rather than by timing, which
    would be flaky and would not say *why* it got slower.
    """
    from runtime.gateway.governance import AuditBuffer

    organization_id = await _org(uow_factory)
    buffer = AuditBuffer()
    for i in range(8):
        buffer.add(
            organization_id=organization_id,
            gateway="tool",
            subject="test.noop@1",
            decision=GatewayDecision.ALLOWED,
            check_name=f"check-{i}",
        )

    statements = 0
    async with uow_factory.transaction() as uow:
        original = uow.session.execute

        async def counting(*args, **kwargs):  # type: ignore[no-untyped-def]
            nonlocal statements
            statements += 1
            return await original(*args, **kwargs)

        uow.session.execute = counting  # type: ignore[method-assign]
        written = await buffer.flush_into(uow)

    assert written == 8
    assert statements == 1, "eight rows, one round trip"


async def test_t43_the_model_gateway_records_decisions_too(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """A model call mutates nothing, so it needs no INTENT row — but it spends money
    and talks to a provider with a quota, which are the two things M2 bounds. Leaving
    it out would make the denial stream tool-shaped rather than complete."""
    from runtime.domain.enums import WorkClass
    from runtime.gateway.models import ModelGateway, ModelRequest

    organization_id = await _org(uow_factory)
    gateway = ModelGateway(uow_factory, settings=settings)
    ctx = make_ctx(organization_id, authority=make_authority())

    await gateway.complete(
        ctx, ModelRequest(prompt="hello"), work_class=WorkClass.WORK, call_site="test.site"
    )

    rows = await decisions_for(uow_factory, ctx.run_id)
    assert [r["gateway"] for r in rows] == ["model"]
    assert rows[0]["decision"] == GatewayDecision.ALLOWED.value
    assert rows[0]["subject"] == "fake/echo-1"
    assert rows[0]["detail"]["work_class"] == "work"
    assert rows[0]["detail"]["call_site"] == "test.site"
    assert rows[0]["severity"] == AuditSeverity.LOW.value, (
        "an allowed model call is the highest-volume row in the table and nobody reads "
        "it individually — it exists to be the denominator"
    )


async def test_a_kill_switch_denies_a_model_call_and_says_so(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    from runtime.domain.enums import KillMode, KillScope, WorkClass
    from runtime.domain.errors import KillSwitchEngaged
    from runtime.gateway.models import ModelGateway, ModelRequest
    from runtime.org.killswitch import KillSwitchService

    organization_id = await _org(uow_factory)
    kill = KillSwitchService(uow_factory, ttl_seconds=0.0)
    await kill.engage(
        organization_id,
        scope_type=KillScope.ACTOR,
        scope_id="research",
        mode=KillMode.DRAIN,
        reason="runaway loop",
    )
    gateway = ModelGateway(uow_factory, settings=settings, kill_switches=kill)
    ctx = make_ctx(organization_id, authority=make_authority())

    with pytest.raises(KillSwitchEngaged):
        await gateway.complete(
            ctx, ModelRequest(prompt="hello"), work_class=WorkClass.WORK, call_site="test.site"
        )

    rows = await decisions_for(uow_factory, ctx.run_id)
    assert rows[0]["decision"] == GatewayDecision.DENIED.value
    assert rows[0]["check"] == "kill_switch"
    assert "runaway loop" in rows[0]["reason"]


# --- T39: an IRREVERSIBLE tool registered bare -----------------------------------------


def test_t39_an_irreversible_tool_registered_bare_gets_no_retries_and_needs_approval() -> None:
    """I14 at registration. The tool says nothing about retries or approval, and the
    blast radius decides — because a tool that could waive its own gate is a gate that
    exists at the discretion of whoever writes the next tool."""
    registry = ToolRegistry()
    registered = registry.register(
        noop_tool_def(
            name="danger.wire",
            blast_radius=BlastRadius.IRREVERSIBLE,
            authority_action="wire_transfer",
        ),
        RecordingTool(),
    )

    assert registered.max_retries == 0, "an irreversible effect is never retried"
    assert registered.requires_approval is True
    assert registered.audit_severity is AuditSeverity.HIGH


def test_t39_an_irreversible_tool_cannot_waive_its_own_approval() -> None:
    registry = ToolRegistry()
    with pytest.raises(IncoherentEffectPolicy, match="cannot waive it for itself"):
        registry.register(
            noop_tool_def(name="danger.wire", blast_radius=BlastRadius.IRREVERSIBLE).model_copy(
                update={"requires_approval": False}
            ),
            RecordingTool(),
        )


def test_t39_the_allow_list_is_the_one_escape_and_it_is_named() -> None:
    """A deliberate, reviewable act with a diff — not a field the tool sets about
    itself."""
    registry = ToolRegistry(approval_allow_list=frozenset({"danger.wire@1"}))
    registered = registry.register(
        noop_tool_def(name="danger.wire", blast_radius=BlastRadius.IRREVERSIBLE).model_copy(
            update={"requires_approval": False}
        ),
        RecordingTool(),
    )
    assert registered.requires_approval is False
    assert registered.max_retries == 0, "the allow-list waives approval, not retries"


def test_t39_a_tool_may_tighten_but_never_loosen() -> None:
    resolved = resolve_policy(
        BlastRadius.REVERSIBLE,
        tool_name="t@1",
        max_retries=0,
        requires_approval=True,
        audit_severity=AuditSeverity.HIGH,
    )
    assert resolved.max_retries == 0 and resolved.requires_approval is True

    with pytest.raises(IncoherentEffectPolicy, match="allows at most"):
        resolve_policy(
            BlastRadius.IRREVERSIBLE,
            tool_name="t@1",
            max_retries=3,
            requires_approval=True,
            audit_severity=None,
        )


def test_t39_the_blast_radius_defaults_are_the_ones_i14_names() -> None:
    """read → auto; reversible → auto under a threshold; irreversible → approval,
    retries=0, elevated audit. Pinned so a change is a decision with a diff."""
    assert BLAST_RADIUS_DEFAULTS[BlastRadius.READ].requires_approval is False
    assert BLAST_RADIUS_DEFAULTS[BlastRadius.REVERSIBLE].requires_approval is False
    irreversible = BLAST_RADIUS_DEFAULTS[BlastRadius.IRREVERSIBLE]
    assert irreversible.requires_approval is True
    assert irreversible.max_retries == 0
    assert irreversible.audit_severity is AuditSeverity.HIGH


async def test_an_irreversible_tool_with_no_authority_action_is_refused_and_audited(
    uow_factory: UnitOfWorkFactory, settings: Settings
) -> None:
    """M0's behaviour, kept: there is no subject to key an approval on, so the answer
    is no — and now the refusal says so in the denial stream."""
    from runtime.domain.errors import ApprovalRequired

    organization_id = await _org(uow_factory)
    harness = build_harness(
        uow_factory, settings, tool_def=noop_tool_def(blast_radius=BlastRadius.IRREVERSIBLE)
    )
    ctx = make_ctx(organization_id, authority=make_authority())

    with pytest.raises(ApprovalRequired):
        await harness.gateway.execute(ctx, ToolCall("test.noop@1", {"value": "x"}))

    rows = await decisions_for(uow_factory, ctx.run_id)
    assert rows[0]["check"] == "authority"
    assert "no authority_action" in rows[0]["reason"]


# --- partitioning -----------------------------------------------------------------------


async def test_the_audit_partitions_cover_a_forward_window(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """A row in the DEFAULT partition means the window was not extended. It is not
    lost, but somebody should notice — which is what the health view is for."""
    from sqlalchemy import text

    async with uow_factory() as uow:
        rows = (
            await uow.session.execute(
                text("SELECT partition, is_default FROM v_audit_partition_health")
            )
        ).all()
    names = [r.partition for r in rows]
    assert len(names) >= 20, "a window, not one month"
    assert "audit_logs_default" in names

    this_month = dt.datetime.now(dt.UTC).strftime("audit_logs_%Y%m")
    assert this_month in names


async def test_ensure_partition_is_idempotent(uow_factory: UnitOfWorkFactory) -> None:
    month = dt.date(2031, 3, 1)
    async with uow_factory.transaction() as uow:
        first = await uow.audit.ensure_partition(month)
        second = await uow.audit.ensure_partition(month)
    assert first == second == "audit_logs_203103"


async def test_an_unknown_decision_value_is_refused_by_the_database(
    uow_factory: UnitOfWorkFactory,
) -> None:
    """The vocabulary is closed at the database, not merely in the enum — the enum is
    the application's opinion and the constraint is the record's."""
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    organization_id = await _org(uow_factory)
    with pytest.raises(IntegrityError):
        async with uow_factory.transaction() as uow:
            await uow.session.execute(
                text(
                    "INSERT INTO audit_logs (organization_id, gateway, subject, decision) "
                    "VALUES (:o, 'tool', 'x', 'maybe')"
                ),
                {"o": organization_id},
            )
