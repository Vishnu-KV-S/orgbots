"""Spec compilation rules, including T10a: HYBRID raises at compile."""

from __future__ import annotations

from uuid import UUID

import pytest
from pydantic import ValidationError

from runtime.domain.enums import ActorKind
from runtime.domain.errors import SpecError
from runtime.domain.ids import ActorId
from runtime.domain.specs import ActorSpec, Ceilings, compile_actor_spec

ACTOR_ID = ActorId(UUID("22222222-2222-2222-2222-222222222222"))


def test_hybrid_raises_not_implemented_at_compile() -> None:
    """T10a. HYBRID is not policed at call time; it does not compile at all."""
    spec = ActorSpec(name="hybrid-thing", kind=ActorKind.HYBRID, graph_ref="x@1")
    with pytest.raises(NotImplementedError, match="HYBRID is not implemented"):
        compile_actor_spec(spec, actor_id=ACTOR_ID, actor_version=1)


def test_llm_agent_requires_a_graph_ref() -> None:
    with pytest.raises(ValidationError, match="graph_ref"):
        ActorSpec(name="a", kind=ActorKind.LLM_AGENT)


def test_deterministic_worker_requires_a_handler_ref() -> None:
    with pytest.raises(ValidationError, match="handler_ref"):
        ActorSpec(name="a", kind=ActorKind.DETERMINISTIC_WORKER, ceilings=Ceilings(max_llm_calls=0))


def test_deterministic_worker_must_declare_zero_llm_calls() -> None:
    """The ceiling is the enforcement point, so the spec may not contradict it."""
    with pytest.raises(ValidationError, match="max_llm_calls == 0"):
        ActorSpec(
            name="hasher",
            kind=ActorKind.DETERMINISTIC_WORKER,
            handler_ref="hasher@1",
            ceilings=Ceilings(max_llm_calls=1),
        )


def test_allowed_model_call_sites_is_hybrid_only() -> None:
    with pytest.raises(ValidationError, match="HYBRID"):
        ActorSpec(
            name="a",
            kind=ActorKind.LLM_AGENT,
            graph_ref="g@1",
            allowed_model_call_sites=frozenset({"plan"}),
        )


def test_specs_are_frozen() -> None:
    spec = ActorSpec(name="a", kind=ActorKind.LLM_AGENT, graph_ref="g@1")
    with pytest.raises(ValidationError):
        spec.name = "b"  # type: ignore[misc]


def test_unknown_spec_fields_are_refused() -> None:
    with pytest.raises(ValidationError):
        ActorSpec(name="a", kind=ActorKind.LLM_AGENT, graph_ref="g@1", max_retires=3)  # type: ignore[call-arg]


def test_entrypoint_follows_kind() -> None:
    llm, _ = compile_actor_spec(
        ActorSpec(name="a", kind=ActorKind.LLM_AGENT, graph_ref="echo_agent@1"),
        actor_id=ACTOR_ID,
        actor_version=1,
    )
    worker, _ = compile_actor_spec(
        ActorSpec(
            name="hasher",
            kind=ActorKind.DETERMINISTIC_WORKER,
            handler_ref="hasher@1",
            ceilings=Ceilings(max_llm_calls=0),
        ),
        actor_id=ACTOR_ID,
        actor_version=1,
    )
    assert llm.entrypoint == "echo_agent@1"
    assert worker.entrypoint == "hasher@1"


def test_model_profiles_raise_spec_error_for_unmapped_work_class() -> None:
    from runtime.domain.enums import WorkClass
    from runtime.domain.specs import ModelProfiles

    with pytest.raises(SpecError, match="no model profile"):
        ModelProfiles().for_work_class(WorkClass.REASONING)
