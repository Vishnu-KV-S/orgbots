"""OpenTelemetry spans.

Tracing is optional at runtime (`RUNTIME_OTEL_ENABLED`), but `trace_id` is not:
when tracing is off we still need a stable correlation ID on every log line, so
`current_trace_id()` falls back to a generated one rather than returning empty.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider

_CONFIGURED = False


def configure_tracing(*, service_name: str, enabled: bool = False) -> None:
    global _CONFIGURED
    if _CONFIGURED or not enabled:
        return
    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    trace.set_tracer_provider(provider)
    _CONFIGURED = True


def tracer(name: str = "runtime") -> trace.Tracer:
    return trace.get_tracer(name)


def current_trace_id() -> str:
    span = trace.get_current_span()
    ctx = span.get_span_context()
    if ctx.is_valid:
        return format(ctx.trace_id, "032x")
    return uuid.uuid4().hex


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[trace.Span]:
    with tracer().start_as_current_span(name) as s:
        for key, value in attributes.items():
            if value is None:
                continue
            scalar = value if isinstance(value, str | int | float | bool) else str(value)
            s.set_attribute(key, scalar)
        yield s
