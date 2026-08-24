"""The agents YAML.

Pure: no database, no network. Every test here is about one of two properties —
that a valid file lands on the specs exactly as written, and that an invalid one
fails loudly rather than leaving the expensive model quietly in place.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from runtime.domain.enums import WorkClass
from runtime.domain.errors import SpecError
from runtime.org.agents_config import EMPTY, load_agents_config
from runtime.org.department import ANALYTICS, CONTENT, DEPARTMENT_SPECS, RESEARCH

EXAMPLE = Path("config/agents.example.yaml")

BASE: dict[str, object] = {
    "version": 1,
    "models": {
        "flash": {
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "input_cents_per_mtok": 22,
            "output_cents_per_mtok": 66,
        }
    },
    "agents": {RESEARCH: {"models": {"work": "flash", "summarization": "flash"}}},
}


def _write(tmp_path: Path, **overrides: object) -> Path:
    body = {**BASE, **overrides}
    path = tmp_path / "agents.yaml"
    path.write_text(yaml.safe_dump(body))
    return path


def _by_name(specs: tuple) -> dict:  # type: ignore[type-arg]
    return {spec.name: spec for spec in specs}


# --- the happy path -----------------------------------------------------------------


def test_unset_path_means_the_code_is_the_configuration() -> None:
    """A checkout with no config file behaves exactly as it did before there was one."""
    assert load_agents_config(None) is EMPTY
    assert EMPTY.apply(DEPARTMENT_SPECS) == DEPARTMENT_SPECS


def test_the_example_file_is_valid_and_applies() -> None:
    """The example is documentation, and documentation that does not load is worse
    than none — it is the file someone copies at 2am."""
    specs = _by_name(load_agents_config(EXAMPLE).apply(DEPARTMENT_SPECS))

    summarization = specs[CONTENT].model_profiles.for_work_class(WorkClass.SUMMARIZATION)
    assert (summarization.provider, summarization.model) == ("deepseek", "deepseek-v4-flash")

    work = specs[CONTENT].model_profiles.for_work_class(WorkClass.WORK)
    assert (work.provider, work.model) == ("deepseek", "deepseek-v4-pro")


def test_an_inline_profile_needs_no_preset(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        agents={
            RESEARCH: {
                "models": {
                    "work": {
                        "provider": "deepseek",
                        "model": "deepseek-v4-pro",
                        "max_output_tokens": 16_000,
                        "temperature": 0.0,
                        "input_cents_per_mtok": 66,
                        "output_cents_per_mtok": 198,
                    }
                }
            }
        },
    )
    profile = _by_name(load_agents_config(path).apply(DEPARTMENT_SPECS))[
        RESEARCH
    ].model_profiles.for_work_class(WorkClass.WORK)

    assert profile.model == "deepseek-v4-pro"
    assert profile.output_cents_per_mtok == 198


def test_the_three_call_dials_are_authorable(tmp_path: Path) -> None:
    """Thinking, effort and provider-side search are the reason this file exists at
    all: they are the spend decisions, and they are now in the same place as the
    price they move."""
    path = _write(
        tmp_path,
        agents={
            RESEARCH: {
                "models": {
                    "work": {
                        "provider": "deepseek",
                        "model": "deepseek-v4-pro",
                        "thinking": True,
                        "effort": "max",
                        "web_search": True,
                    },
                    "summarization": {
                        "provider": "deepseek",
                        "model": "deepseek-v4-flash",
                        "thinking": False,
                        "effort": "low",
                    },
                }
            }
        },
    )
    profiles = _by_name(load_agents_config(path).apply(DEPARTMENT_SPECS))[RESEARCH].model_profiles

    work = profiles.for_work_class(WorkClass.WORK)
    assert (work.thinking, work.effort, work.web_search) == (True, "max", True)

    summarization = profiles.for_work_class(WorkClass.SUMMARIZATION)
    assert (summarization.thinking, summarization.effort) == (False, "low")
    assert summarization.web_search is False, "not asked for, so not on"


def test_an_effort_outside_the_vocabulary_is_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        models={"flash": {"provider": "deepseek", "model": "x", "effort": "maximum"}},
    )
    with pytest.raises(SpecError, match="effort"):
        load_agents_config(path)


def test_profiles_are_replaced_not_merged(tmp_path: Path) -> None:
    """`ModelProfiles` is a per-actor allow-list over the work-class vocabulary: an
    actor with no profile for a class cannot call in that class. A merge would mean
    the file could only ever widen that, and quietly."""
    path = _write(tmp_path, agents={RESEARCH: {"models": {"work": "flash"}}})
    research = _by_name(load_agents_config(path).apply(DEPARTMENT_SPECS))[RESEARCH]

    assert set(research.model_profiles.profiles) == {WorkClass.WORK}
    with pytest.raises(SpecError):
        research.model_profiles.for_work_class(WorkClass.SUMMARIZATION)


def test_an_actor_the_file_omits_keeps_the_code_default(tmp_path: Path) -> None:
    path = _write(tmp_path)
    specs = _by_name(load_agents_config(path).apply(DEPARTMENT_SPECS))

    assert specs[CONTENT] == _by_name(DEPARTMENT_SPECS)[CONTENT]


def test_nothing_but_the_models_changes(tmp_path: Path) -> None:
    """Tools, ceilings and kind are the shape of the department. If this file could
    move them, `test_grants_mirror_the_actor_specs` would be checking a moving target."""
    path = _write(tmp_path)
    before = _by_name(DEPARTMENT_SPECS)[RESEARCH]
    after = _by_name(load_agents_config(path).apply(DEPARTMENT_SPECS))[RESEARCH]

    assert (after.allowed_tools, after.ceilings, after.kind, after.graph_ref) == (
        before.allowed_tools,
        before.ceilings,
        before.kind,
        before.graph_ref,
    )


# --- and every way it can be wrong --------------------------------------------------


def test_a_configured_path_that_does_not_exist_is_a_deployment_fault(tmp_path: Path) -> None:
    """ "Nobody configured this" and "the thing that was configured is missing" are
    different problems, and falling back to the code's defaults is the expensive one
    to guess."""
    with pytest.raises(SpecError, match="does not exist"):
        load_agents_config(tmp_path / "absent.yaml")


def test_an_unknown_actor_is_an_error_not_a_no_op(tmp_path: Path) -> None:
    path = _write(tmp_path, agents={"reserch": {"models": {"work": "flash"}}})
    with pytest.raises(SpecError, match="no such actor"):
        load_agents_config(path).apply(DEPARTMENT_SPECS)


def test_an_unknown_work_class_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, agents={RESEARCH: {"models": {"drafting": "flash"}}})
    with pytest.raises(SpecError, match="is not a work class"):
        load_agents_config(path)


def test_an_undefined_preset_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, agents={RESEARCH: {"models": {"work": "turbo"}}})
    with pytest.raises(SpecError, match="not defined"):
        load_agents_config(path)


def test_an_unknown_profile_field_is_rejected(tmp_path: Path) -> None:
    """Same reasoning as `extra="forbid"` on the specs: a typo must fail the load,
    not be carried along and ignored at the point it was meant to constrain spend."""
    path = _write(
        tmp_path,
        models={"flash": {"provider": "deepseek", "model": "x", "cost_per_token": 3}},
    )
    with pytest.raises(SpecError, match="unknown field"):
        load_agents_config(path)


def test_ceilings_are_not_configurable_here(tmp_path: Path) -> None:
    """A file that appears to set a ceiling and does not would be worse than one that
    cannot: it reads as governance while changing nothing."""
    path = _write(
        tmp_path,
        agents={RESEARCH: {"models": {"work": "flash"}, "ceilings": {"max_llm_calls": 99}}},
    )
    with pytest.raises(SpecError, match="unsupported key"):
        load_agents_config(path)


def test_the_deterministic_worker_cannot_be_given_a_model(tmp_path: Path) -> None:
    """`analytics` is the control actor. Its whole value to the gate numbers is that
    no LLM appears anywhere in its provenance, and this file is the only new way to
    take that away."""
    path = _write(tmp_path, agents={ANALYTICS: {"models": {"work": "flash"}}})
    with pytest.raises(SpecError, match="max_llm_calls=0"):
        load_agents_config(path).apply(DEPARTMENT_SPECS)


def test_an_unsupported_version_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, version=2)
    with pytest.raises(SpecError, match="unsupported version"):
        load_agents_config(path)
