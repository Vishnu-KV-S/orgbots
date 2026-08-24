"""Event topic names.

They used to live in `runtime.runtime.run_service`, next to the code that publishes
them, which read well until something *below* that layer needed to subscribe. M3's
`MemoryWorker` is that something: it consumes `run.succeeded` from `runtime.memory`,
which sits under `runtime.runtime`, and importing the name upward is the layering
violation `lint-imports` caught on the first run.

A topic name is part of the event vocabulary rather than part of the publisher, so it
belongs in the layer that owns the stream. Publisher and subscriber now both read the
same constant from below them, which is what a shared vocabulary should look like.

`runtime.worker.worker` keeps its own `TOPIC_QUEUED` literal for now. It is the same
string, and unifying it is a separate change from this one — the duplication is
recorded here rather than quietly fixed under an unrelated milestone.
"""

from __future__ import annotations

TOPIC_RUN_QUEUED = "run.queued"
TOPIC_RUN_SUCCEEDED = "run.succeeded"
TOPIC_RUN_FAILED = "run.failed"
TOPIC_RUN_REFUSED = "run.refused"
"""M2. Emitted when admission refuses. A separate topic from `run.failed` because the
two need different responses: a failure is investigated, a refusal is paid for."""

TOPIC_DELEGATION_REFUSED = "delegation.refused"
"""M5. One of §4's eight checks said no. Separate from `run.refused` because no run
was created — checks 1 to 6 refuse before the row exists — so a consumer counting
refused runs would see nothing at all."""

TOPIC_DELEGATION_LATE = "delegation.late_child"
"""M5, edge case 29. A child finished after its parent was already terminal. Its
result is persisted and attached; **nothing is resumed**. The event exists because
this is the one delegation outcome nobody is waiting for, and an unwatched outcome is
one nobody can tell happened."""

TOPIC_DELEGATION_CASCADE = "delegation.cascade"
"""M5, edge case 28. A terminal run cancelled its descendants."""

__all__ = [
    "TOPIC_DELEGATION_CASCADE",
    "TOPIC_DELEGATION_LATE",
    "TOPIC_DELEGATION_REFUSED",
    "TOPIC_RUN_FAILED",
    "TOPIC_RUN_QUEUED",
    "TOPIC_RUN_REFUSED",
    "TOPIC_RUN_SUCCEEDED",
]
