"""T10 — deterministic worker isolation, and the rest of the gateway's refusals.

The claim being tested is not "the hasher does not call an LLM". It is that the
hasher *cannot*: the refusal lives in the gateway, so it holds for a handler
nobody has read, written by someone who did not know the rule.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

from runtime.domain.context import Lease, RunContext
from runtime.domain.enums import ActorKind, WorkClass
from runtime.domain.errors import (
    ApprovalRequired,
    CeilingExceeded,
    KillSwitchEngaged,
    MissingWorkClass,
    ModelCallNotAllowed,
    StaleFence,
    ToolNotAllowed,
    UnknownToolError,
    ValidationFailed,
)
from runtime.domain.ids import Fence, OrganizationId, WorkerId
from runtime.domain.specs import RunSpec
from runtime.gateway.models import ModelGateway, ModelRequest
from runtime.gateway.tools import KillSwitch, ToolCall
from runtime.observability.tracing import current_trace_id
from runtime.settings import Settings
from tests.conftest_runtime import Runtime, build_runtime

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def rt(settings: Settings, organization_id: OrganizationId) -> AsyncIterator[Runtime]:
    async for runtime in build_runtime(settings, organization_id):
        yield runtime


async def _context(rt: Runtime, actor: str, key: str) -> RunContext:
    """Admit a run and claim it, returning the context a node would receive."""
    started = await rt.start(actor, key, {})
    lease = await rt.worker.leases.claim(started.run_id, rt.worker.worker_id)
    assert lease is not None
    async with rt.uow() as uow:
        row = await uow.runs.get_spec(started.run_id)
    assert row is not None
    spec = RunSpec.model_validate(row.spec)
    return RunContext(
        run_id=spec.run_id,
        organization_id=spec.organization_id,
        root_run_id=spec.root_run_id,
        actor_id=spec.spec.actor_id,
        actor_version=spec.spec.actor_version,
        spec=spec,
        lease=lease,
        worker_id=rt.worker.worker_id,
        trace_id=current_trace_id(),
    )


# --- T10 ----------------------------------------------------------------------------


async def test_a_deterministic_worker_cannot_call_a_model(rt: Runtime) -> None:
    """T10. `max_llm_calls == 0` is enforced at the gateway."""
    ctx = await _context(rt, "hasher", "t10-hasher")
    models = ModelGateway(rt.uow, settings=rt.settings)

    with pytest.raises(ModelCallNotAllowed, match="max_llm_calls=0"):
        await models.complete(
            ctx,
            ModelRequest(prompt="are you there"),
            work_class=WorkClass.GENERATION,
            call_site="hasher.body",
        )


async def test_the_refusal_names_the_actor_kind(rt: Runtime) -> None:
    ctx = await _context(rt, "hasher", "t10-kind")
    models = ModelGateway(rt.uow, settings=rt.settings)
    with pytest.raises(ModelCallNotAllowed, match=ActorKind.DETERMINISTIC_WORKER.value):
        await models.complete(
            ctx, ModelRequest(prompt="x"), work_class=WorkClass.GENERATION, call_site="c"
        )


async def test_an_llm_agent_may_call_a_model_and_the_usage_is_recorded(rt: Runtime) -> None:
    ctx = await _context(rt, "echo-agent", "t10-agent")
    models = ModelGateway(rt.uow, settings=rt.settings)

    response = await models.complete(
        ctx,
        ModelRequest(prompt="hello"),
        work_class=WorkClass.GENERATION,
        call_site="echo.respond",
    )
    assert response.text == "echo: hello"
    assert response.trust.value == "untrusted"

    async with rt.uow() as uow:
        spend = await uow.budget.spend_for_run(ctx.run_id)
    assert spend == response.cost_cents


async def test_a_model_call_without_a_call_site_is_refused(rt: Runtime) -> None:
    """I12's other half: `work_class` selects the profile, `call_site` says who
    asked. A ledger row with neither is unattributable."""
    ctx = await _context(rt, "echo-agent", "t10-callsite")
    models = ModelGateway(rt.uow, settings=rt.settings)
    with pytest.raises(MissingWorkClass, match="call_site"):
        await models.complete(
            ctx, ModelRequest(prompt="x"), work_class=WorkClass.GENERATION, call_site=""
        )


async def test_an_unmapped_work_class_is_a_spec_error(rt: Runtime) -> None:
    from runtime.domain.errors import SpecError

    ctx = await _context(rt, "echo-agent", "t10-unmapped")
    models = ModelGateway(rt.uow, settings=rt.settings)
    with pytest.raises(SpecError, match="no model profile"):
        await models.complete(
            ctx, ModelRequest(prompt="x"), work_class=WorkClass.REASONING, call_site="c"
        )


async def test_the_llm_call_ceiling_is_enforced(rt: Runtime) -> None:
    ctx = await _context(rt, "echo-agent", "t10-ceiling")
    models = ModelGateway(rt.uow, settings=rt.settings)
    for _ in range(ctx.spec.ceilings.max_llm_calls):
        await models.complete(
            ctx, ModelRequest(prompt="x"), work_class=WorkClass.GENERATION, call_site="c"
        )
    with pytest.raises(CeilingExceeded, match="max_llm_calls"):
        await models.complete(
            ctx, ModelRequest(prompt="x"), work_class=WorkClass.GENERATION, call_site="c"
        )


# --- tool gateway refusals ----------------------------------------------------------


async def test_a_tool_outside_allowed_tools_is_refused(rt: Runtime) -> None:
    ctx = await _context(rt, "hasher", "gw-notallowed")
    with pytest.raises(ToolNotAllowed, match="may not call"):
        await rt.worker.executor.tools.execute(
            ctx, ToolCall(tool="web.fetch@1", args={"url": "http://example.invalid"})
        )


async def test_an_unregistered_tool_is_refused(rt: Runtime) -> None:
    ctx = await _context(rt, "echo-agent", "gw-unknown")
    with pytest.raises(UnknownToolError):
        await rt.worker.executor.tools.execute(ctx, ToolCall(tool="nope.missing@9", args={}))


async def test_invalid_arguments_are_refused_before_the_journal(rt: Runtime) -> None:
    """Nothing cheap-and-refusable may leave an INTENT row behind."""
    ctx = await _context(rt, "echo-agent", "gw-badargs")
    with pytest.raises(ValidationFailed):
        await rt.worker.executor.tools.execute(
            ctx, ToolCall(tool="fixture.sideeffect@1", args={"delay_ms": -5})
        )
    async with rt.uow() as uow:
        assert await uow.effects.for_run(ctx.run_id) == []


async def test_a_stale_fence_is_refused_before_the_journal(rt: Runtime) -> None:
    """T5, at the gateway: a zombie leaves no trace because it never gets in."""
    ctx = await _context(rt, "echo-agent", "gw-fence")
    ctx.lease = Lease(
        run_id=ctx.run_id,
        worker_id=WorkerId(uuid.uuid4()),
        fence=Fence(99),
        lease_until=dt.datetime.now(dt.UTC),
    )
    with pytest.raises(StaleFence):
        await rt.worker.executor.tools.execute(ctx, ToolCall(tool="fixture.sideeffect@1", args={}))
    async with rt.uow() as uow:
        assert await uow.effects.for_run(ctx.run_id) == []


async def test_the_kill_switch_stops_an_in_flight_run(rt: Runtime) -> None:
    """A stop that only applies to new runs is not a stop."""
    from runtime.artifacts.store import ArtifactStore
    from runtime.effects.journal import EffectJournal
    from runtime.gateway.builtin import build_registry
    from runtime.gateway.tools import ToolGateway

    ctx = await _context(rt, "echo-agent", "gw-kill")
    gateway = ToolGateway(
        build_registry(rt.uow),
        EffectJournal(rt.uow),
        rt.uow,
        artifacts=ArtifactStore(rt.uow, settings=rt.settings),
        settings=rt.settings,
        kill_switch=KillSwitch(engaged=True, reason="incident 42"),
    )
    with pytest.raises(KillSwitchEngaged, match="incident 42"):
        await gateway.execute(ctx, ToolCall(tool="fixture.sideeffect@1", args={}))


async def test_a_tool_requiring_approval_cannot_run_in_m0(rt: Runtime) -> None:
    """M0 has no approvals subsystem. Refusing is the correct behaviour — the
    alternative is letting an irreversible tool through unapproved."""
    from pydantic import BaseModel

    from runtime.domain.enums import BlastRadius, RecoveryPolicy
    from runtime.gateway.tools import EffectCapabilities, ToolDef

    class _A(BaseModel):
        pass

    class _R(BaseModel):
        ok: bool = True

    async def _fn(_c: object, _a: object) -> _R:
        return _R()

    ctx = await _context(rt, "echo-agent", "gw-approval")
    registry = rt.worker.executor.tools.registry
    registry.register(
        ToolDef(
            name="danger.wire",
            version=1,
            args_model=_A,
            result_model=_R,
            capabilities=EffectCapabilities(
                mutates_external_state=True,
                accepts_idempotency_key=True,
                idempotency_key_field="Idempotency-Key",
                max_blast_radius=BlastRadius.IRREVERSIBLE,
            ),
            recovery_policy=RecoveryPolicy.IDEMPOTENCY_KEY,
        ),
        _fn,  # type: ignore[arg-type]
    )
    ctx.spec = ctx.spec.model_copy(
        update={
            "spec": ctx.spec.spec.model_copy(update={"allowed_tools": frozenset({"danger.wire@1"})})
        }
    )
    with pytest.raises(ApprovalRequired):
        await rt.worker.executor.tools.execute(ctx, ToolCall(tool="danger.wire@1", args={}))


async def test_the_tool_call_ceiling_is_enforced(rt: Runtime) -> None:
    ctx = await _context(rt, "echo-agent", "gw-toolceiling")
    ctx.tool_calls = ctx.spec.ceilings.max_tool_calls
    with pytest.raises(CeilingExceeded, match="max_tool_calls"):
        await rt.worker.executor.tools.execute(ctx, ToolCall(tool="fixture.sideeffect@1", args={}))
