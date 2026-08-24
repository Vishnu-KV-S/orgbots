"""The ID set on every log line.

The definition of done says every log line carries `organization_id`,
`root_run_id`, `run_id`, `actor_id`, `actor_version`, `fence` and `trace_id`. That
is the kind of requirement that is true on the day it is written and quietly false
six months later, so it gets a test rather than a code review.

Two properties, and the second is the one that decays:

1. A line emitted inside a run carries all seven, populated.
2. A line emitted outside a run carries all seven as explicit nulls. A missing key
   is invisible in a dashboard; an explicit null shows up in a "where is run_id
   null" query, which is how the requirement gets audited instead of assumed.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import Iterator

import pytest
import structlog

from runtime.domain.context import Lease, RunContext
from runtime.domain.enums import ActorKind
from runtime.domain.ids import ActorId, Fence, OrganizationId, RunId, WorkerId
from runtime.domain.specs import ActorSpec, RunSpec, compile_actor_spec
from runtime.observability.logging import REQUIRED_FIELDS, bound_ids, configure_logging


@pytest.fixture
def capture() -> Iterator[list[dict[str, object]]]:
    """Capture rendered log events without touching the real configuration."""
    entries: list[dict[str, object]] = []
    configure_logging(level="DEBUG", json=True)
    original = structlog.get_config()["processors"]

    def _sink(_logger: object, _name: str, event: dict[str, object]) -> str:
        rendered = json.dumps(event, default=str)
        entries.append(json.loads(rendered))
        return rendered

    structlog.configure(processors=[*original[:-1], _sink])
    try:
        yield entries
    finally:
        structlog.configure(processors=original)


def _ctx() -> RunContext:
    run_id = RunId(uuid.uuid4())
    org = OrganizationId(uuid.uuid4())
    spec, spec_hash = compile_actor_spec(
        ActorSpec(name="echo-agent", kind=ActorKind.LLM_AGENT, graph_ref="echo_agent@1"),
        actor_id=ActorId(uuid.uuid4()),
        actor_version=3,
    )
    run_spec = RunSpec(
        run_id=run_id,
        organization_id=org,
        root_run_id=run_id,
        thread_id=str(run_id),
        spec=spec,
        spec_hash=spec_hash,
    )
    lease = Lease(
        run_id=run_id,
        worker_id=WorkerId(uuid.uuid4()),
        fence=Fence(4),
        lease_until=dt.datetime.now(dt.UTC),
    )
    return RunContext(
        run_id=run_id,
        organization_id=org,
        root_run_id=run_id,
        actor_id=spec.actor_id,
        actor_version=3,
        spec=run_spec,
        lease=lease,
        worker_id=lease.worker_id,
        trace_id="0123456789abcdef0123456789abcdef",
    )


def test_a_line_inside_a_run_carries_the_whole_id_set(
    capture: list[dict[str, object]],
) -> None:
    ctx = _ctx()
    with bound_ids(**ctx.log_fields()):
        structlog.get_logger("test").info("something.happened", extra="value")

    assert len(capture) == 1
    line = capture[0]
    for field in REQUIRED_FIELDS:
        assert field in line, f"{field} missing from the log line"
        assert line[field] is not None, f"{field} present but null inside a run"

    assert line["run_id"] == str(ctx.run_id)
    assert line["fence"] == 4
    assert line["actor_version"] == 3
    assert line["trace_id"] == "0123456789abcdef0123456789abcdef"
    assert line["extra"] == "value"


def test_a_line_outside_a_run_carries_explicit_nulls(
    capture: list[dict[str, object]],
) -> None:
    """A missing key is invisible in a dashboard. An explicit null is queryable."""
    structlog.get_logger("test").info("startup.happened")

    line = capture[0]
    for field in REQUIRED_FIELDS:
        assert field in line, f"{field} missing entirely — it must be an explicit null"
        assert line[field] is None


def test_ids_do_not_leak_from_one_run_to_the_next(
    capture: list[dict[str, object]],
) -> None:
    """A worker handles many runs on one task. Carrying the previous run's ID onto
    the next run's lines is worse than having no ID at all — it attributes work to
    a run that did not do it."""
    first, second = _ctx(), _ctx()
    log = structlog.get_logger("test")

    with bound_ids(**first.log_fields()):
        log.info("first.run")
    with bound_ids(**second.log_fields()):
        log.info("second.run")
    log.info("between.runs")

    assert capture[0]["run_id"] == str(first.run_id)
    assert capture[1]["run_id"] == str(second.run_id)
    assert capture[2]["run_id"] is None, "the last run's id leaked past its scope"


def test_nested_binding_restores_the_outer_scope(
    capture: list[dict[str, object]],
) -> None:
    outer, inner = _ctx(), _ctx()
    log = structlog.get_logger("test")

    with bound_ids(**outer.log_fields()):
        with bound_ids(**inner.log_fields()):
            log.info("inner")
        log.info("outer.again")

    assert capture[0]["run_id"] == str(inner.run_id)
    assert capture[1]["run_id"] == str(outer.run_id)


def test_the_required_field_list_matches_the_context() -> None:
    """`RunContext.log_fields()` and `REQUIRED_FIELDS` must not drift apart, or a
    field gets bound that nothing enforces, or enforced that nothing binds."""
    assert set(_ctx().log_fields()) == set(REQUIRED_FIELDS)


def test_trace_id_is_present_even_when_tracing_is_disabled() -> None:
    """Correlation must not depend on an optional exporter being configured."""
    from runtime.observability.tracing import current_trace_id

    first, second = current_trace_id(), current_trace_id()
    assert len(first) == 32
    assert first != second, "outside a span each call is its own correlation id"
