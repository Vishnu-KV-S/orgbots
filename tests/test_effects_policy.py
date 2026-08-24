"""T6 — logical_call_id purity. T9 — capability entailment. T13 — blast radius.

These need no database. They are the checks that make the database-backed
exactly-once story *possible*, and they are cheap enough to run on every commit.
"""

from __future__ import annotations

import datetime as dt
import subprocess
import sys
import textwrap
import uuid

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import BaseModel

from runtime.domain.context import Lease, RunContext
from runtime.domain.enums import ActorKind, AuditSeverity, BlastRadius, RecoveryPolicy
from runtime.domain.errors import IncoherentEffectPolicy
from runtime.domain.ids import ActorId, Fence, OrganizationId, RunId, WorkerId
from runtime.domain.specs import ActorSpec, RunSpec, compile_actor_spec
from runtime.effects.journal import logical_call_id
from runtime.effects.policies import BLAST_RADIUS_DEFAULTS, check_entailment, resolve_policy
from runtime.effects.probes import clear_probes, register_probe
from runtime.gateway.tools import EffectCapabilities, ToolDef, ToolRegistry

RUN_ID = RunId(uuid.UUID("33333333-3333-3333-3333-333333333333"))
ORG_ID = OrganizationId(uuid.UUID("44444444-4444-4444-4444-444444444444"))


def _ctx(run_id: RunId = RUN_ID) -> RunContext:
    spec, spec_hash = compile_actor_spec(
        ActorSpec(name="echo-agent", kind=ActorKind.LLM_AGENT, graph_ref="echo_agent@1"),
        actor_id=ActorId(uuid.UUID("55555555-5555-5555-5555-555555555555")),
        actor_version=1,
    )
    run_spec = RunSpec(
        run_id=run_id,
        organization_id=ORG_ID,
        root_run_id=run_id,
        thread_id=str(run_id),
        spec=spec,
        spec_hash=spec_hash,
    )
    lease = Lease(
        run_id=run_id,
        worker_id=WorkerId(uuid.uuid4()),
        fence=Fence(1),
        lease_until=dt.datetime.now(dt.UTC),
    )
    return RunContext(
        run_id=run_id,
        organization_id=ORG_ID,
        root_run_id=run_id,
        actor_id=spec.actor_id,
        actor_version=1,
        spec=run_spec,
        lease=lease,
        worker_id=lease.worker_id,
        trace_id="t",
    )


# --- T6 -----------------------------------------------------------------------------


def test_same_inputs_produce_the_same_id() -> None:
    a = logical_call_id(_ctx(), "fetch", 0, "argsdigest")
    b = logical_call_id(_ctx(), "fetch", 0, "argsdigest")
    assert a == b


def test_a_different_ordinal_produces_a_different_id() -> None:
    ctx = _ctx()
    assert logical_call_id(ctx, "fetch", 0, "h") != logical_call_id(ctx, "fetch", 1, "h")


def test_each_of_the_five_inputs_changes_the_id() -> None:
    base = logical_call_id(_ctx(), "fetch", 0, "h", "")
    variants = {
        base,
        logical_call_id(_ctx(RunId(uuid.uuid4())), "fetch", 0, "h", ""),
        logical_call_id(_ctx(), "respond", 0, "h", ""),
        logical_call_id(_ctx(), "fetch", 1, "h", ""),
        logical_call_id(_ctx(), "fetch", 0, "other", ""),
        logical_call_id(_ctx(), "fetch", 0, "h", "loop:2"),
    }
    assert len(variants) == 6


def test_the_fence_is_deliberately_not_part_of_the_id() -> None:
    """Two attempts at the same logical call happen under different fences. If the
    fence were in the key, the replay would mint a new identity and fire twice —
    which is the exact bug the journal exists to prevent."""
    ctx = _ctx()
    first = logical_call_id(ctx, "fetch", 0, "h")
    ctx.lease = Lease(
        run_id=ctx.run_id,
        worker_id=ctx.lease.worker_id,
        fence=Fence(7),
        lease_until=ctx.lease.lease_until,
    )
    assert logical_call_id(ctx, "fetch", 0, "h") == first


PURITY_SCRIPT = textwrap.dedent(
    """
    import datetime as dt, uuid
    from runtime.domain.context import Lease, RunContext
    from runtime.domain.enums import ActorKind
    from runtime.domain.ids import ActorId, Fence, OrganizationId, RunId, WorkerId
    from runtime.domain.specs import ActorSpec, RunSpec, compile_actor_spec
    from runtime.effects.journal import logical_call_id

    run_id = RunId(uuid.UUID("33333333-3333-3333-3333-333333333333"))
    org = OrganizationId(uuid.UUID("44444444-4444-4444-4444-444444444444"))
    spec, h = compile_actor_spec(
        ActorSpec(name="echo-agent", kind=ActorKind.LLM_AGENT, graph_ref="echo_agent@1"),
        actor_id=ActorId(uuid.UUID("55555555-5555-5555-5555-555555555555")),
        actor_version=1,
    )
    rs = RunSpec(run_id=run_id, organization_id=org, root_run_id=run_id,
                 thread_id=str(run_id), spec=spec, spec_hash=h)
    lease = Lease(run_id=run_id, worker_id=WorkerId(uuid.uuid4()), fence=Fence(1),
                  lease_until=dt.datetime.now(dt.UTC))
    ctx = RunContext(run_id=run_id, organization_id=org, root_run_id=run_id,
                     actor_id=spec.actor_id, actor_version=1, spec=rs, lease=lease,
                     worker_id=lease.worker_id, trace_id="t")
    print(logical_call_id(ctx, "fetch", 0, "argsdigest"))
    """
)


@pytest.mark.parametrize("hash_seed", ["0", "7", "random"])
def test_the_id_is_identical_across_process_restarts(hash_seed: str) -> None:
    """T6's real assertion. A `hash()`-based implementation passes every
    single-process test and fails here."""
    in_process = logical_call_id(_ctx(), "fetch", 0, "argsdigest")
    out = subprocess.run(
        [sys.executable, "-c", PURITY_SCRIPT],
        capture_output=True,
        text=True,
        check=True,
        env={"PYTHONHASHSEED": hash_seed, "PATH": "/usr/bin:/bin"},
    )
    assert out.stdout.strip() == in_process


@settings(max_examples=200, deadline=None)
@given(
    node=st.text(min_size=0, max_size=16),
    ordinal=st.integers(min_value=0, max_value=1000),
    digest=st.text(min_size=1, max_size=32),
    ns=st.text(min_size=0, max_size=8),
)
def test_purity_property(node: str, ordinal: int, digest: str, ns: str) -> None:
    ctx = _ctx()
    first = logical_call_id(ctx, node, ordinal, digest, ns)
    second = logical_call_id(ctx, node, ordinal, digest, ns)
    assert first == second
    assert logical_call_id(ctx, node, ordinal + 1, digest, ns) != first


def test_node_scope_ordinals_restart_per_node_execution() -> None:
    """A replayed node must re-issue ordinals from 0, or its second call gets a
    different logical ID than it had on the attempt that crashed."""
    ctx = _ctx()
    with ctx.node("fetch") as scope:
        first_pass = [scope.next_ordinal() for _ in range(3)]
    with ctx.node("fetch") as scope:
        replay = [scope.next_ordinal() for _ in range(3)]
    assert first_pass == replay == [0, 1, 2]


def test_node_scope_is_restored_on_exit() -> None:
    ctx = _ctx()
    with ctx.node("outer") as outer:
        outer.next_ordinal()
        with ctx.node("inner"):
            pass
        assert ctx.scope.node == "outer"
        assert ctx.scope.next_ordinal() == 1


# --- T9 -----------------------------------------------------------------------------


def _caps(**kwargs: object) -> dict[str, object]:
    base: dict[str, object] = {
        "tool_name": "t@1",
        "mutates_external_state": False,
        "accepts_idempotency_key": False,
        "idempotency_key_field": None,
        "searchable_marker": False,
        "marker_field": None,
        "marker_search_fn": None,
    }
    base.update(kwargs)
    return base


def test_replay_safe_requires_no_mutation() -> None:
    check_entailment(RecoveryPolicy.REPLAY_SAFE, **_caps())  # type: ignore[arg-type]
    with pytest.raises(IncoherentEffectPolicy, match="replay_safe but also mutates"):
        check_entailment(
            RecoveryPolicy.REPLAY_SAFE,
            **_caps(mutates_external_state=True),  # type: ignore[arg-type]
        )


def test_idempotency_key_policy_requires_a_key_and_a_field() -> None:
    with pytest.raises(IncoherentEffectPolicy, match="does not accept an idempotency key"):
        check_entailment(
            RecoveryPolicy.IDEMPOTENCY_KEY,
            **_caps(mutates_external_state=True),  # type: ignore[arg-type]
        )
    with pytest.raises(IncoherentEffectPolicy, match="without naming the field"):
        check_entailment(
            RecoveryPolicy.IDEMPOTENCY_KEY,
            **_caps(mutates_external_state=True, accepts_idempotency_key=True),  # type: ignore[arg-type]
        )
    check_entailment(
        RecoveryPolicy.IDEMPOTENCY_KEY,
        **_caps(
            mutates_external_state=True,
            accepts_idempotency_key=True,
            idempotency_key_field="Idempotency-Key",
        ),  # type: ignore[arg-type]
    )


def test_probe_policy_requires_a_searchable_marker_and_a_search_function() -> None:
    """The failure this prevents: probe declared, no marker, every replay silently
    degrades to re-execution and nothing errors."""
    with pytest.raises(IncoherentEffectPolicy, match="without a searchable marker"):
        check_entailment(RecoveryPolicy.PROBE, **_caps(mutates_external_state=True))  # type: ignore[arg-type]
    with pytest.raises(IncoherentEffectPolicy, match="without naming the marker field"):
        check_entailment(
            RecoveryPolicy.PROBE,
            **_caps(mutates_external_state=True, searchable_marker=True),  # type: ignore[arg-type]
        )
    with pytest.raises(IncoherentEffectPolicy, match="without a marker_search_fn"):
        check_entailment(
            RecoveryPolicy.PROBE,
            **_caps(mutates_external_state=True, searchable_marker=True, marker_field="m"),  # type: ignore[arg-type]
        )


def test_a_non_mutating_tool_may_not_claim_a_recovery_policy() -> None:
    with pytest.raises(IncoherentEffectPolicy, match="Use replay_safe"):
        check_entailment(
            RecoveryPolicy.PROBE,
            **_caps(searchable_marker=True, marker_field="m", marker_search_fn="p"),  # type: ignore[arg-type]
        )


def test_manual_entails_nothing() -> None:
    check_entailment(RecoveryPolicy.MANUAL, **_caps(mutates_external_state=True))  # type: ignore[arg-type]


# --- T13 ----------------------------------------------------------------------------


class _Args(BaseModel):
    x: int = 0


class _Result(BaseModel):
    ok: bool = True


async def _noop(_ctx: object, _args: object) -> _Result:
    return _Result()


def _irreversible_tool(**overrides: object) -> ToolDef:
    base: dict[str, object] = {
        "name": "danger.wire",
        "version": 1,
        "args_model": _Args,
        "result_model": _Result,
        "capabilities": EffectCapabilities(
            mutates_external_state=True,
            accepts_idempotency_key=True,
            idempotency_key_field="Idempotency-Key",
            max_blast_radius=BlastRadius.IRREVERSIBLE,
        ),
        "recovery_policy": RecoveryPolicy.IDEMPOTENCY_KEY,
    }
    base.update(overrides)
    return ToolDef(**base)  # type: ignore[arg-type]


def test_irreversible_defaults_to_no_retries_and_mandatory_approval() -> None:
    """T13."""
    registry = ToolRegistry()
    registered = registry.register(_irreversible_tool(), _noop)  # type: ignore[arg-type]
    assert registered.max_retries == 0
    assert registered.requires_approval is True
    assert registered.audit_severity is AuditSeverity.HIGH


def test_a_tool_may_not_loosen_its_blast_radius_defaults() -> None:
    registry = ToolRegistry()
    with pytest.raises(IncoherentEffectPolicy, match="allows at most 0 retries"):
        registry.register(_irreversible_tool(max_retries=3), _noop)  # type: ignore[arg-type]
    with pytest.raises(IncoherentEffectPolicy, match="cannot waive it for itself"):
        registry.register(_irreversible_tool(requires_approval=False), _noop)  # type: ignore[arg-type]
    with pytest.raises(IncoherentEffectPolicy, match="audit severity at least high"):
        registry.register(_irreversible_tool(audit_severity=AuditSeverity.LOW), _noop)  # type: ignore[arg-type]


def test_a_tool_may_tighten_its_defaults() -> None:
    registry = ToolRegistry()
    registered = registry.register(
        ToolDef(
            name="read.thing",
            version=1,
            args_model=_Args,
            result_model=_Result,
            capabilities=EffectCapabilities(
                mutates_external_state=False, max_blast_radius=BlastRadius.READ
            ),
            recovery_policy=RecoveryPolicy.REPLAY_SAFE,
            max_retries=0,
            requires_approval=True,
            audit_severity=AuditSeverity.HIGH,
        ),
        _noop,  # type: ignore[arg-type]
    )
    assert (registered.max_retries, registered.requires_approval) == (0, True)


def test_the_allow_list_is_the_only_way_out_of_mandatory_approval() -> None:
    registry = ToolRegistry(approval_allow_list=frozenset({"danger.wire@1"}))
    registered = registry.register(_irreversible_tool(requires_approval=False), _noop)  # type: ignore[arg-type]
    assert registered.requires_approval is False
    assert registered.max_retries == 0, "the allow-list waives approval, not retries"


def test_registration_rejects_a_probe_naming_an_unregistered_search_function() -> None:
    clear_probes()
    registry = ToolRegistry()
    definition = ToolDef(
        name="ghost.probe",
        version=1,
        args_model=_Args,
        result_model=_Result,
        capabilities=EffectCapabilities(
            mutates_external_state=True,
            searchable_marker=True,
            marker_field="marker",
            marker_search_fn="nobody.registered.this",
            max_blast_radius=BlastRadius.REVERSIBLE,
        ),
        recovery_policy=RecoveryPolicy.PROBE,
    )
    with pytest.raises(IncoherentEffectPolicy, match="not a registered probe"):
        registry.register(definition, _noop)  # type: ignore[arg-type]

    async def finder(marker: str) -> dict[str, object] | None:
        return None

    register_probe("nobody.registered.this", finder)
    assert registry.register(definition, _noop).policy is RecoveryPolicy.PROBE  # type: ignore[arg-type]


def test_every_blast_radius_has_defaults() -> None:
    assert set(BLAST_RADIUS_DEFAULTS) == set(BlastRadius)
    assert BLAST_RADIUS_DEFAULTS[BlastRadius.IRREVERSIBLE].max_retries == 0
    assert BLAST_RADIUS_DEFAULTS[BlastRadius.IRREVERSIBLE].requires_approval is True


def test_resolve_policy_defaults_are_used_when_unspecified() -> None:
    resolved = resolve_policy(
        BlastRadius.REVERSIBLE,
        tool_name="t@1",
        max_retries=None,
        requires_approval=None,
        audit_severity=None,
    )
    assert resolved == BLAST_RADIUS_DEFAULTS[BlastRadius.REVERSIBLE]
