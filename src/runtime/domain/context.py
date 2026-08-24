"""Run context and lease value objects.

`RunContext` is the single argument every gateway takes. It carries the frozen
RunSpec, the identity set that must appear on every log line, and the lease whose
fence authorises the call.

The one piece of mutable state here is the per-node call ordinal, and it is
mutable for a specific reason — see `NodeScope`.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime

from runtime.domain.ids import (
    ActorId,
    Fence,
    OrganizationId,
    RunId,
    SessionId,
    TaskId,
    WorkerId,
)
from runtime.domain.specs import RunSpec


@dataclass(frozen=True, slots=True)
class Lease:
    """Proof that this worker owns this run right now.

    `fence` is the whole point. Holding a `Lease` object means nothing; holding one
    whose fence still matches the run row is what authorises a side effect.
    """

    run_id: RunId
    worker_id: WorkerId
    fence: Fence
    lease_until: datetime


@dataclass(slots=True)
class NodeScope:
    """Ordinal counter for one execution of one graph node.

    Ordinals must be a function of position within the node, not of wall-clock call
    order across the whole run. A fresh scope is opened each time a node body runs,
    so a replayed node re-issues ordinals 0, 1, 2… exactly as the crashed attempt
    did — which is what makes `logical_call_id` reproduce.
    """

    node: str
    checkpoint_ns: str = ""
    _next: int = 0

    def next_ordinal(self) -> int:
        ordinal = self._next
        self._next += 1
        return ordinal


@dataclass(slots=True)
class RunContext:
    """Everything a gateway needs to decide whether a call may proceed."""

    run_id: RunId
    organization_id: OrganizationId
    root_run_id: RunId
    actor_id: ActorId
    actor_version: int
    spec: RunSpec
    lease: Lease
    worker_id: WorkerId
    trace_id: str
    session_id: SessionId | None = None
    scope: NodeScope = field(default_factory=lambda: NodeScope(node=""))
    llm_calls: int = 0
    tool_calls: int = 0

    @property
    def fence(self) -> Fence:
        return self.lease.fence

    @property
    def task_id(self) -> TaskId | None:
        """The task this run serves, from the frozen spec.

        Read through the spec rather than stored again here: a second copy is a
        second thing that can be stale, and the approval gate keys on this value.
        """
        return self.spec.task_id

    @property
    def correlation_id(self) -> str | None:
        return self.spec.correlation_id

    @contextmanager
    def node(
        self, name: str, *, checkpoint_ns: str = "", iteration: int | None = None
    ) -> Iterator[NodeScope]:
        """Enter a node. Restores the previous scope on exit so a node that calls a
        helper which itself opens a scope does not corrupt the caller's ordinals.

        `checkpoint_ns` must distinguish repeat executions of the same node that are
        *not* replays — a loop iteration, a fan-out branch. A linear graph is
        correct with the empty namespace; a graph with a cycle that omits it has two
        iterations collide on one journal key, and the second silently receives the
        first one's result.

        `iteration=` is the ergonomic form and the one to reach for: it builds the
        namespace itself, so the loop body reads `with ctx.node("fetch",
        iteration=i)` and there is nothing to get wrong. The M0 retro records why
        this exists — M1's `research` graph fetches in a loop, and the raw
        `checkpoint_ns` spelling was an accident waiting for the first cyclic graph.

        Passing both raises rather than picking one: two callers disagreeing about
        the namespace is precisely the bug this parameter exists to prevent.
        """
        if iteration is not None:
            if checkpoint_ns:
                raise ValueError(
                    f"node({name!r}) got both checkpoint_ns={checkpoint_ns!r} and "
                    f"iteration={iteration}; pass one"
                )
            checkpoint_ns = f"i{iteration}"
        previous = self.scope
        self.scope = NodeScope(node=name, checkpoint_ns=checkpoint_ns)
        try:
            yield self.scope
        finally:
            self.scope = previous

    def log_fields(self) -> dict[str, object]:
        """The ID set that every log line carries. See the definition of done."""
        return {
            "organization_id": str(self.organization_id),
            "root_run_id": str(self.root_run_id),
            "run_id": str(self.run_id),
            "actor_id": str(self.actor_id),
            "actor_version": self.actor_version,
            "fence": int(self.lease.fence),
            "trace_id": self.trace_id,
        }
