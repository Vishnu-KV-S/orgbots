"""T1 — spec_hash determinism.

The hash must be a function of the logical value only: not of dict insertion
order, not of set iteration order, not of `PYTHONHASHSEED`, not of which process
computed it.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from uuid import UUID

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from runtime.domain.enums import ActorKind, WorkClass
from runtime.domain.hashing import args_hash, canonical_hash, canonical_json, spec_hash
from runtime.domain.ids import ActorId
from runtime.domain.specs import (
    ActorSpec,
    Ceilings,
    ModelProfile,
    ModelProfiles,
    compile_actor_spec,
)

ACTOR_ID = ActorId(UUID("11111111-1111-1111-1111-111111111111"))


def _spec(**overrides: object) -> ActorSpec:
    base: dict[str, object] = {
        "name": "echo-agent",
        "kind": ActorKind.LLM_AGENT,
        "graph_ref": "echo_agent@1",
        "allowed_tools": frozenset({"web.fetch@1", "fixture.sideeffect@1"}),
        "ceilings": Ceilings(max_llm_calls=4, max_tool_calls=8),
        "model_profiles": ModelProfiles(
            profiles={
                WorkClass.GENERATION: ModelProfile(provider="fake", model="echo-1"),
                WorkClass.CLASSIFICATION: ModelProfile(provider="fake", model="echo-mini"),
            }
        ),
    }
    base.update(overrides)
    return ActorSpec(**base)  # type: ignore[arg-type]


def test_dict_insertion_order_does_not_change_the_hash() -> None:
    a = canonical_hash({"a": 1, "b": {"x": 1, "y": 2}})
    b = canonical_hash({"b": {"y": 2, "x": 1}, "a": 1})
    assert a == b


def test_set_iteration_order_does_not_change_the_hash() -> None:
    a = canonical_hash({"tools": frozenset({"a", "b", "c"})})
    b = canonical_hash({"tools": frozenset({"c", "a", "b"})})
    assert a == b


def test_mixed_type_sets_are_orderable() -> None:
    # A set with no natural ordering must still canonicalise rather than raise.
    value = {"mixed": frozenset({1, "1", None})}
    assert canonical_hash(value) == canonical_hash({"mixed": frozenset({None, "1", 1})})


def test_compiled_spec_hash_is_stable_for_equal_specs() -> None:
    _, h1 = compile_actor_spec(_spec(), actor_id=ACTOR_ID, actor_version=1)
    _, h2 = compile_actor_spec(_spec(), actor_id=ACTOR_ID, actor_version=1)
    assert h1 == h2


def test_compiled_spec_hash_changes_when_anything_material_changes() -> None:
    _, base = compile_actor_spec(_spec(), actor_id=ACTOR_ID, actor_version=1)
    _, more_tools = compile_actor_spec(
        _spec(allowed_tools=frozenset({"web.fetch@1"})), actor_id=ACTOR_ID, actor_version=1
    )
    _, other_ceiling = compile_actor_spec(
        _spec(ceilings=Ceilings(max_llm_calls=5, max_tool_calls=8)),
        actor_id=ACTOR_ID,
        actor_version=1,
    )
    _, other_version = compile_actor_spec(_spec(), actor_id=ACTOR_ID, actor_version=2)
    assert len({base, more_tools, other_ceiling, other_version}) == 4


CROSS_PROCESS_SCRIPT = textwrap.dedent(
    """
    from uuid import UUID
    from runtime.domain.enums import ActorKind, WorkClass
    from runtime.domain.ids import ActorId
    from runtime.domain.specs import (
        ActorSpec, Ceilings, ModelProfile, ModelProfiles, compile_actor_spec,
    )

    spec = ActorSpec(
        name="echo-agent",
        kind=ActorKind.LLM_AGENT,
        graph_ref="echo_agent@1",
        allowed_tools=frozenset({"web.fetch@1", "fixture.sideeffect@1"}),
        ceilings=Ceilings(max_llm_calls=4, max_tool_calls=8),
        model_profiles=ModelProfiles(profiles={
            WorkClass.GENERATION: ModelProfile(provider="fake", model="echo-1"),
            WorkClass.CLASSIFICATION: ModelProfile(provider="fake", model="echo-mini"),
        }),
    )
    _, h = compile_actor_spec(
        spec,
        actor_id=ActorId(UUID("11111111-1111-1111-1111-111111111111")),
        actor_version=1,
    )
    print(h)
    """
)


@pytest.mark.parametrize("hash_seed", ["0", "1", "12345", "random"])
def test_hash_is_identical_across_processes_and_hash_seeds(
    hash_seed: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PYTHONHASHSEED changes set iteration order. It must not change the hash."""
    _, in_process = compile_actor_spec(_spec(), actor_id=ACTOR_ID, actor_version=1)
    out = subprocess.run(
        [sys.executable, "-c", CROSS_PROCESS_SCRIPT],
        capture_output=True,
        text=True,
        check=True,
        env={"PYTHONHASHSEED": hash_seed, "PATH": "/usr/bin:/bin"},
    )
    assert out.stdout.strip() == in_process


def test_non_finite_floats_are_refused() -> None:
    with pytest.raises(ValueError, match="non-finite"):
        canonical_json({"x": float("nan")})


def test_uncanonicalisable_types_are_refused() -> None:
    with pytest.raises(TypeError, match="not canonicalisable"):
        canonical_json({"fn": lambda: None})


@settings(max_examples=200, deadline=None)
@given(
    st.dictionaries(
        st.text(max_size=8),
        st.one_of(st.integers(), st.text(max_size=8), st.booleans(), st.none()),
        max_size=6,
    )
)
def test_args_hash_is_a_function_of_the_value(payload: dict[str, object]) -> None:
    assert args_hash(payload) == args_hash(dict(reversed(list(payload.items()))))
    assert len(args_hash(payload)) == 32


def test_spec_hash_and_canonical_hash_agree() -> None:
    value = {"a": [1, 2, 3]}
    assert spec_hash(value) == canonical_hash(value)
