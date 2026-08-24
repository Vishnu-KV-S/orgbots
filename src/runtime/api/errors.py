"""Domain exception → HTTP status, in one table.

Both routers map the same exceptions, and two hand-written `except` chains would
eventually disagree about one of them. The table below is the single answer.

**Order is the whole design.** Every config-plane error is a `SpecError` — `spec/
errors.py` says so deliberately: *"a control plane that raised a new family would be a
control plane whose failures escaped every existing handler"*. That makes `SpecError →
400` a catch-all which, checked first, would swallow `PlanStale`, `ApplyConflict`,
`RenameRefused` and both document errors and answer 400 to all of them. A stale plan is
not a malformed request; it is a conflict, and the client's correct response — re-plan
and re-read the diff — is one it can only take if the status says so.

So: subclass before superclass, most specific first, `SpecError` last. `isinstance`
walks this tuple in order and the first hit wins.

The statuses themselves:

    409  the request was well-formed and the world moved (or would be damaged)
    422  the *documents* are wrong — parseable request, unusable content
    404  no such actor
    501  a shape this build refuses to pretend it supports
    429  a budget ceiling
    403  a policy said no, kill switch included
    400  anything else the config plane raised
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import HTTPException

from runtime.domain.errors import (
    ApprovalRequired,
    BudgetExceeded,
    PolicyViolation,
    SpecError,
    UnknownActorError,
)
from runtime.spec.errors import (
    ApplyConflict,
    PlanStale,
    RenameRefused,
    SpecDocumentError,
    SpecValidationError,
)

STATUS_BY_ERROR: tuple[tuple[type[BaseException], int], ...] = (
    # --- 409: the world moved, or would be damaged ---
    (PlanStale, 409),
    (ApplyConflict, 409),
    (RenameRefused, 409),
    # --- 422: the documents are wrong. `RenameRefused` is above because it is a
    # `SpecValidationError` and is not a document problem — it is a refusal to do
    # something irreversible without being asked to. ---
    (SpecDocumentError, 422),
    (SpecValidationError, 422),
    # --- the four `app.py` has always had ---
    (UnknownActorError, 404),
    (NotImplementedError, 501),
    (BudgetExceeded, 429),
    (ApprovalRequired, 403),
    (PolicyViolation, 403),  # KillSwitchEngaged is one of these
    # --- last, and only last ---
    (SpecError, 400),
)


def status_for(exc: BaseException) -> int | None:
    """The status this exception maps to, or `None` if it is not one of ours.

    `None` means *let it propagate*: an exception nobody mapped is a 500 with a
    traceback in the log, which is the correct answer for a bug and a much worse
    answer to hide behind a tidy 400.
    """
    for kind, status in STATUS_BY_ERROR:
        if isinstance(exc, kind):
            return status
    return None


@contextmanager
def http_errors() -> Iterator[None]:
    """Translate domain exceptions into `HTTPException` for the duration of a block.

    A context manager rather than a decorator so a handler can put it around the one
    call that raises and keep its own 404s — which read better as explicit
    `HTTPException`s than as a domain error invented to carry a status.
    """
    try:
        yield
    except HTTPException:
        raise
    except Exception as exc:
        status = status_for(exc)
        if status is None:
            raise
        raise HTTPException(status_code=status, detail=str(exc)) from exc


__all__ = ["STATUS_BY_ERROR", "http_errors", "status_for"]
