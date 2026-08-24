"""Schema registry.

Task outputs are pinned to a schema the way a tool call is pinned to a `ToolDef`:
by a ref string resolved out of a code registry. Tasks store `"CompetitorReport@1"`;
there is no schema table and there is no migration when a schema is added.

Three properties, and each one is a decision that could have gone the other way.

**A registered version is immutable.** Re-registering a ref with a different model
raises. Changing a field means `@2`. Without this, a task written on Monday and
evaluated on Friday could be judged against a schema that did not exist when the
work was assigned, and "did it meet the spec" would stop being answerable.

**The registry stores the JSON Schema alongside the model.** Two callers need
different things: validation wants the Pydantic class, and the prompt that tells an
LLM what to produce wants the JSON Schema text. Deriving the latter on every call
would make an identical prompt hash differently between processes.

**Validation returns typed errors, never a bare exception string.** A schema
failure is a thing the manager has to act on and the assignee has to fix, so
`validate()` returns a list of `(pointer, message)` pairs that survive being put in
a JSON column and read back a week later. T14 asserts exactly that.

Pure: no I/O, no database, importable from a unit test with nothing running.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ValidationError

from runtime.domain.errors import OutputSchemaViolation, SpecError

_REF_PATTERN = re.compile(r"^(?P<name>[A-Za-z][A-Za-z0-9_]*)@(?P<version>[1-9][0-9]*)$")


@dataclass(frozen=True, slots=True)
class SchemaError:
    """One validation failure, in a form that survives a round trip through JSONB.

    `pointer` is a JSON Pointer into the offending value ("/sources/2/url"), not a
    Pydantic location tuple, because the thing that reads this back is a prompt
    telling an agent what to fix.
    """

    pointer: str
    message: str
    kind: str

    def to_json(self) -> dict[str, str]:
        return {"pointer": self.pointer, "message": self.message, "kind": self.kind}


@dataclass(frozen=True, slots=True)
class RegisteredSchema:
    ref: str
    name: str
    version: int
    model: type[BaseModel]
    json_schema: dict[str, Any]

    def validate(self, payload: Any) -> BaseModel:
        """Return the parsed model or raise `ValidationFailed` carrying `errors`."""
        try:
            return self.model.model_validate(payload)
        except ValidationError as exc:
            raise OutputSchemaViolation(
                f"{self.ref}: {exc.error_count()} schema error(s)",
                errors=[e.to_json() for e in _as_schema_errors(exc)],
                schema_ref=self.ref,
            ) from exc

    def check(self, payload: Any) -> list[SchemaError]:
        """Non-raising form. Empty list means valid."""
        try:
            self.model.model_validate(payload)
        except ValidationError as exc:
            return _as_schema_errors(exc)
        return []


class SchemaRegistry:
    """Ref → schema. One instance, module-level, populated at import."""

    def __init__(self) -> None:
        self._by_ref: dict[str, RegisteredSchema] = {}

    def register(self, model: type[BaseModel], *, version: int, name: str | None = None) -> str:
        """Register `model` as `"{name}@{version}"` and return the ref.

        Idempotent for the *same* model object, so importing a module twice under
        different names is harmless. A different model on an existing ref raises:
        that is the immutability guarantee, and it fires at import time rather than
        at the moment a week-old task is evaluated against a schema that moved.
        """
        resolved = name or model.__name__
        ref = f"{resolved}@{version}"
        if not _REF_PATTERN.match(ref):
            raise SpecError(f"{ref!r} is not a valid schema ref (expected Name@Version)")
        existing = self._by_ref.get(ref)
        if existing is not None:
            if existing.model is model:
                return ref
            raise SpecError(
                f"schema {ref!r} is already registered as {existing.model.__name__}; "
                f"a registered version is immutable — publish {resolved}@{version + 1} instead"
            )
        self._by_ref[ref] = RegisteredSchema(
            ref=ref,
            name=resolved,
            version=version,
            model=model,
            json_schema=model.model_json_schema(),
        )
        return ref

    def get(self, ref: str) -> RegisteredSchema:
        try:
            return self._by_ref[ref]
        except KeyError as exc:
            raise SpecError(
                f"no schema registered as {ref!r}; known: {sorted(self._by_ref)}"
            ) from exc

    def has(self, ref: str) -> bool:
        return ref in self._by_ref

    def refs(self) -> frozenset[str]:
        return frozenset(self._by_ref)


def _as_schema_errors(exc: ValidationError) -> list[SchemaError]:
    out: list[SchemaError] = []
    for err in exc.errors():
        pointer = "/" + "/".join(str(part) for part in err["loc"]) if err["loc"] else "/"
        out.append(SchemaError(pointer=pointer, message=err["msg"], kind=err["type"]))
    return out


SCHEMAS = SchemaRegistry()
"""The process-wide registry. `runtime.domain.outputs` populates it at import."""
