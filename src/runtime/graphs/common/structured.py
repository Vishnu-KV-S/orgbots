"""Making a model call that has to conform to a pinned schema.

Every LLM call in M1 has an output schema, so every one of them goes through here.
Three things happen that would otherwise be repeated in four graphs and get subtly
different in at least one of them.

**The schema is sent, not described.** `json_schema` goes into the request metadata
and the provider turns it into `output_config.format`. Asking for JSON in prose and
parsing the reply makes "the schema failed" ambiguous between "the agent produced
the wrong thing" and "the agent produced the right thing and we mis-read it" — and
telling those apart is §10's first diagnostic question.

**One retry, with the errors.** A schema failure gets exactly one more attempt, and
the second prompt carries the typed error list. This is worth doing at the call site
rather than leaving to the task-level cap because a malformed *field* is usually a
one-shot fix, and burning a whole task rework cycle on it would spend three
expensive submissions to fix a missing comma. The task-level cap (three failures →
REJECTED) still stands above it and still counts every failure.

**The work class is required.** It is a positional-by-keyword argument with no
default, so a new call site cannot be written without choosing one. That is most of
what makes T25 true by construction rather than by audit.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypeVar

from pydantic import BaseModel

from runtime.domain.context import RunContext
from runtime.domain.enums import M1_WORK_CLASSES, WorkClass
from runtime.domain.errors import OutputSchemaViolation
from runtime.domain.ids import OrganizationId
from runtime.domain.schemas import SCHEMAS, RegisteredSchema
from runtime.gateway.models import ModelGateway, ModelRequest
from runtime.graphs.common.context import AssembledContext
from runtime.observability.logging import get_logger

log = get_logger("graphs.structured")

T = TypeVar("T", bound=BaseModel)

RETRY_PREAMBLE = (
    "Your previous response did not satisfy the required output schema. "
    "The validator reported these problems:\n\n{errors}\n\n"
    "Produce the same output again, corrected. Do not explain the correction; "
    "return only the conforming object."
)


@dataclass(frozen=True, slots=True)
class StructuredResult:
    value: BaseModel
    raw_text: str
    cost_cents: int
    input_tokens: int
    output_tokens: int
    attempts: int

    @property
    def retried(self) -> bool:
        return self.attempts > 1


async def call_structured(
    ctx: RunContext,
    models: ModelGateway,
    context: AssembledContext,
    schema_ref: str,
    *,
    work_class: WorkClass,
    call_site: str,
    max_output_tokens: int | None = None,
) -> StructuredResult:
    """One structured model call, with one corrective retry.

    Raises `OutputSchemaViolation` carrying the typed error list when both attempts
    fail. The caller — a task-producing node — turns that into a schema failure on
    the task, which is where the cap lives.
    """
    if work_class not in M1_WORK_CLASSES:
        # Not a style rule. `overhead_ratio` is `1 - work_share`, so a call outside
        # the M1 vocabulary silently lands in overhead and makes the coordination
        # ratio wrong in a direction nobody would question. Refuse instead. T25.
        raise ValueError(
            f"{call_site}: work_class {work_class.value!r} is outside the M1 "
            f"vocabulary {sorted(w.value for w in M1_WORK_CLASSES)}; the coordination "
            "ratio is defined over those four and nothing else"
        )

    schema: RegisteredSchema = SCHEMAS.get(schema_ref)
    prompt = context.prompt
    total_cost = 0
    total_in = 0
    total_out = 0
    last_errors: list[dict[str, str]] = []

    for attempt in (1, 2):
        response = await models.complete(
            ctx,
            ModelRequest(
                prompt=prompt,
                system=context.system,
                max_output_tokens=max_output_tokens,
                metadata={"json_schema": schema.json_schema, "schema_ref": schema_ref},
            ),
            work_class=work_class,
            call_site=call_site,
        )
        total_cost += response.cost_cents
        total_in += response.input_tokens
        total_out += response.output_tokens

        payload = _as_object(response.text)
        if payload is not None:
            errors = schema.check(payload)
            if not errors:
                log.info(
                    "structured.ok",
                    call_site=call_site,
                    schema=schema_ref,
                    attempt=attempt,
                    cost_cents=total_cost,
                    prompt_parts=context.parts,
                    **ctx.log_fields(),
                )
                return StructuredResult(
                    value=schema.validate(payload),
                    raw_text=response.text,
                    cost_cents=total_cost,
                    input_tokens=total_in,
                    output_tokens=total_out,
                    attempts=attempt,
                )
            last_errors = [e.to_json() for e in errors]
        else:
            last_errors = [
                {
                    "pointer": "/",
                    "message": "response was not a JSON object",
                    "kind": "not_json",
                }
            ]

        log.warning(
            "structured.invalid",
            call_site=call_site,
            schema=schema_ref,
            attempt=attempt,
            errors=last_errors[:5],
            # Only for `not_json`, and only the opening. A pointer-and-message list
            # tells you which field was wrong; "response was not a JSON object" tells
            # you nothing you can act on — a refusal in prose, a truncated object and a
            # markdown fence the parser did not recognise are three different bugs with
            # one error string, and the first 240 characters separate them immediately.
            # A validated payload is never logged: this is the branch where there
            # isn't one.
            raw_head=(response.text[:240] if payload is None else None),
            **ctx.log_fields(),
        )
        if attempt == 1:
            rendered = "\n".join(f"- {e['pointer']}: {e['message']}" for e in last_errors[:12])
            prompt = context.prompt + "\n\n" + RETRY_PREAMBLE.format(errors=rendered)

    raise OutputSchemaViolation(
        f"{call_site}: two attempts failed to satisfy {schema_ref}",
        errors=last_errors,
        schema_ref=schema_ref,
    )


async def record_schema_failure(
    node: Any,
    ctx: RunContext,
    exc: OutputSchemaViolation,
    *,
    manager_name: str,
) -> None:
    """Put the typed error list on the task before the run dies carrying it.

    Both attempts above have failed by the time a caller reaches here, so there is
    nothing left to salvage in *this* run — but the errors are the only thing that
    makes the next one better, and until now they were discarded with the stack
    trace. `tasks.last_schema_errors` stayed NULL through an evening of
    `OutputSchemaViolation` run failures, which made "which field?" unanswerable
    and left the correction block each work graph assembles from that column
    permanently empty.

    Recording also counts the failure against the three-strike cap, so a task no
    model can satisfy is eventually REJECTED and escalated rather than retried
    every week at WORK-class prices.

    Shared rather than written per graph: two copies of "record, then re-raise"
    is how one of them ends up recording and swallowing.
    """
    if ctx.task_id is None:
        return
    errors = exc.errors if isinstance(exc.errors, list) else []
    await node.org.tasks.record_schema_failure(
        ctx.task_id,
        errors,
        organization_id=OrganizationId(ctx.organization_id),
        manager_name=manager_name,
    )


def _as_object(text: str) -> dict[str, Any] | None:
    """Parse a JSON object out of a response.

    Structured output makes the plain case a plain `json.loads`. The fenced-block
    fallback exists because a refusal or a `max_tokens` truncation can still produce
    text, and distinguishing "the model wrapped it in a fence" from "the model did
    not answer" is worth the six lines — the first is recoverable on retry and the
    second is not.
    """
    import json

    stripped = text.strip()
    if not stripped:
        return None
    if stripped.startswith("```"):
        body = stripped.split("```", 2)
        if len(body) >= 2:
            stripped = body[1]
            if stripped.startswith("json"):
                stripped = stripped[4:]
            stripped = stripped.strip()
    try:
        loaded = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None
