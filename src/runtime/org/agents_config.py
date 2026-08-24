"""The agents YAML: model profiles, authored outside the code.

`runtime.org.department` says there is no YAML and that the hardcoding is the point.
That is still true of the things it was said about — which actors exist, what kind
each is, which graph it runs, which tools it holds, what its ceilings are. All of
that stays in code, because it is the shape of the department and changing it is a
change to the system, not to its configuration.

What this file adds is narrower and is the one thing that genuinely is an operating
decision: **which model each work class runs on, and at what price**. Swapping
SUMMARIZATION onto a cheaper model is a spend choice an operator makes on a Tuesday;
it does not change what any actor is allowed to do, and requiring a code change for
it is how a cost dial ends up never being turned.

Three properties make this safe to have:

**It is applied at seed time, not at run time.** `seed_department` overlays it and
publishes an actor version, so the profiles are compiled into the `ActorSpec`, hashed
into `spec_hash` and frozen into every `RunSpec` at admission. An operator editing the
YAML under a run in flight changes nothing about that run — the same guarantee the
ceilings comment in `department` describes, for the same reason.

**An actor listed here has its profiles replaced, not merged.** `ModelProfiles` is a
per-actor allow-list over the work-class vocabulary — an actor with no profile for a
class *cannot make a call in that class* — so a merge would mean the YAML could only
ever widen it, and quietly. Replacement makes the file the whole answer for the
actors it names, and the code the whole answer for the ones it does not.

**Every error is loud.** An unknown actor name, an unknown work class, a model preset
that does not exist, a profile handed to the deterministic worker: all `SpecError` at
load. A typo in a config file that silently leaves the expensive model in place is the
failure this format exists to make impossible, and it is worth failing a deploy over.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from runtime.domain.enums import WorkClass
from runtime.domain.errors import SpecError
from runtime.domain.specs import ActorSpec, ModelProfile, ModelProfiles
from runtime.observability.logging import get_logger

log = get_logger("org.agents_config")

SUPPORTED_VERSION = 1

_PROFILE_FIELDS = frozenset(ModelProfile.model_fields)


@dataclass(frozen=True, slots=True)
class AgentsConfig:
    """The parsed file. Empty is a valid, meaningful value: the code's own defaults."""

    profiles_by_actor: dict[str, ModelProfiles]
    source: str | None = None

    def apply(self, specs: tuple[ActorSpec, ...]) -> tuple[ActorSpec, ...]:
        """Overlay onto the authored specs, validating against them.

        Validation happens *here* rather than at parse time because it is the specs
        that make an actor name real, a work class affordable and `max_llm_calls=0`
        binding. A loader that validated only the file's shape would accept
        `analytics: {WORK: deepseek-pro}` — which is the one thing in this whole file that
        would invalidate the M1 control numbers.
        """
        if not self.profiles_by_actor:
            return specs

        by_name = {spec.name: spec for spec in specs}
        unknown = sorted(set(self.profiles_by_actor) - set(by_name))
        if unknown:
            raise SpecError(
                f"{self._where()}: no such actor(s) {unknown}; the department defines "
                f"{sorted(by_name)}"
            )

        out: list[ActorSpec] = []
        for spec in specs:
            profiles = self.profiles_by_actor.get(spec.name)
            if profiles is None:
                out.append(spec)
                continue
            if spec.ceilings.max_llm_calls == 0 and profiles.profiles:
                raise SpecError(
                    f"{self._where()}: actor {spec.name!r} has max_llm_calls=0 and cannot "
                    "be given model profiles; it is a deterministic worker and the "
                    "gateway would refuse every call anyway"
                )
            out.append(spec.model_copy(update={"model_profiles": profiles}))
            log.info(
                "agents_config.applied",
                actor=spec.name,
                profiles={
                    wc.value: f"{p.provider}/{p.model}" for wc, p in profiles.profiles.items()
                },
            )
        return tuple(out)

    def _where(self) -> str:
        return self.source or "agents config"


EMPTY = AgentsConfig(profiles_by_actor={})


def load_agents_config(path: str | Path | None) -> AgentsConfig:
    """Read the file, or return the empty config when there is nothing to read.

    An unset path is the default checkout and means "use the code's values". A path
    that is *set but missing* is a deployment fault and raises: the difference between
    "nobody configured this" and "the thing that was configured is not there" is the
    whole value of the error, and silently falling back to the code's defaults would be
    the expensive direction to be wrong in.
    """
    if not path:
        return EMPTY
    file = Path(path)
    if not file.exists():
        raise SpecError(f"agents config {file} does not exist (RUNTIME_AGENTS_CONFIG_PATH)")

    raw = yaml.safe_load(file.read_text()) or {}
    if not isinstance(raw, dict):
        raise SpecError(f"{file}: expected a mapping at the top level")

    version = raw.get("version", SUPPORTED_VERSION)
    if version != SUPPORTED_VERSION:
        raise SpecError(f"{file}: unsupported version {version!r}, expected {SUPPORTED_VERSION}")

    presets = _parse_presets(raw.get("models") or {}, file)
    agents = raw.get("agents") or {}
    if not isinstance(agents, dict):
        raise SpecError(f"{file}: `agents` must be a mapping of actor name to config")

    profiles_by_actor: dict[str, ModelProfiles] = {}
    for actor, body in agents.items():
        if not isinstance(body, dict):
            raise SpecError(f"{file}: agent {actor!r} must be a mapping")
        extra = sorted(set(body) - {"models"})
        if extra:
            # Deliberately narrow. Tools, ceilings and kind are not configurable here
            # and a file that appears to set them would be worse than one that cannot:
            # it would read as governance while changing nothing.
            raise SpecError(
                f"{file}: agent {actor!r} has unsupported key(s) {extra}; only `models` "
                "is configurable — tools, ceilings and kind live in runtime.org.department"
            )
        models = body.get("models") or {}
        if not isinstance(models, dict):
            raise SpecError(f"{file}: agent {actor!r}: `models` must be a mapping")
        profiles_by_actor[str(actor)] = ModelProfiles(
            profiles={
                _work_class(name, actor, file): _resolve(value, presets, actor, file)
                for name, value in models.items()
            }
        )

    return AgentsConfig(profiles_by_actor=profiles_by_actor, source=str(file))


def _parse_presets(raw: Any, file: Path) -> dict[str, ModelProfile]:
    if not isinstance(raw, dict):
        raise SpecError(f"{file}: `models` must be a mapping of preset name to profile")
    return {str(name): _profile(body, f"models.{name}", file) for name, body in raw.items()}


def _resolve(value: Any, presets: dict[str, ModelProfile], actor: str, file: Path) -> ModelProfile:
    """A work class names a preset, or spells a profile out inline."""
    if isinstance(value, str):
        try:
            return presets[value]
        except KeyError as exc:
            raise SpecError(
                f"{file}: agent {actor!r} references model preset {value!r}, which is not "
                f"defined; known presets are {sorted(presets)}"
            ) from exc
    return _profile(value, f"agents.{actor}", file)


def _profile(body: Any, where: str, file: Path) -> ModelProfile:
    if not isinstance(body, dict):
        raise SpecError(f"{file}: {where}: expected a mapping of profile fields")
    unknown = sorted(set(body) - _PROFILE_FIELDS)
    if unknown:
        raise SpecError(
            f"{file}: {where}: unknown field(s) {unknown}; a ModelProfile takes "
            f"{sorted(_PROFILE_FIELDS)}"
        )
    try:
        return ModelProfile(**body)
    except Exception as exc:
        # Pydantic's message names the field and the constraint, which is more useful
        # than anything this layer could add; it is wrapped only so that every failure
        # out of this module is one exception type the boot path can report.
        raise SpecError(f"{file}: {where}: {exc}") from exc


def _work_class(name: Any, actor: str, file: Path) -> WorkClass:
    try:
        return WorkClass(str(name).lower())
    except ValueError as exc:
        raise SpecError(
            f"{file}: agent {actor!r}: {name!r} is not a work class; expected one of "
            f"{[wc.value for wc in WorkClass]}"
        ) from exc


__all__ = ["EMPTY", "AgentsConfig", "load_agents_config"]
