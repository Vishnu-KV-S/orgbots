"""Structured logging.

The definition of done requires every log line to carry
`organization_id, root_run_id, run_id, actor_id, actor_version, fence, trace_id`.
Requiring a caller to remember seven fields at every call site guarantees they will
be missing somewhere, so they are bound once into a contextvar when a run is
entered and merged into every line automatically.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator, MutableMapping
from contextlib import contextmanager
from typing import Any

import structlog
from structlog.contextvars import bind_contextvars, reset_contextvars

REQUIRED_FIELDS = (
    "organization_id",
    "root_run_id",
    "run_id",
    "actor_id",
    "actor_version",
    "fence",
    "trace_id",
)


def _mark_missing_ids(
    _logger: Any, _name: str, event: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Make an unbound ID visible instead of absent.

    A missing key in a log line is invisible in a dashboard; an explicit `null`
    shows up in a "where is run_id null" query. This is how the ID-set requirement
    gets audited rather than merely asserted.
    """
    for field in REQUIRED_FIELDS:
        event.setdefault(field, None)
    return event


def configure_logging(*, level: str = "INFO", json: bool = True) -> None:
    renderer: Any = (
        structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer(colors=False)
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _mark_missing_ids,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level.upper()]
        ),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> Any:
    return structlog.get_logger(name)


@contextmanager
def bound_ids(**fields: Any) -> Iterator[None]:
    """Bind IDs for the duration of a block, restoring whatever was bound before.

    Restoring matters: a worker handles many runs on one task, and leaking the
    previous run's ID onto the next run's lines is worse than having no ID at all.
    """
    tokens = bind_contextvars(**fields)
    try:
        yield
    finally:
        reset_contextvars(**tokens)
