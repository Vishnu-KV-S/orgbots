"""Graph state discipline.

The M0 retro measured it: LangGraph checkpoints the *entire* state at every
superstep, so the cost is `state_size * supersteps`, not `changed_bytes`. One
`echo_agent@1` run with a three-field state wrote 4 checkpoints averaging 676 B.
A four-node M1 graph holding an 8 KB `CompetitorReport` in state would write ~40 KB
per run, per replay, forever — for a payload already sitting in the artifact store
with a digest on it.

The retro's decision was: **no `DeltaChannel`; state discipline instead.**

    A graph's state may hold artifact *references*. It may not hold artifact
    *bodies*.

`ArtifactRefView` is what a reference looks like: an id, a digest, a size and a
summary capped at 512 characters. It is small, it is enough to put in a prompt, and
`load()` is how a node that genuinely needs the bytes gets them.

The rule is testable rather than conventional — `test_m1_state_discipline.py`
invokes each M1 graph and asserts no checkpoint row exceeds `MAX_CHECKPOINT_BYTES`.
When that starts failing under real workloads, that is the signal to build
`DeltaChannel` in M3, and the test tells us the week it happens rather than the
quarter.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from runtime.artifacts.store import ArtifactStore
from runtime.domain.ids import ArtifactId

MAX_SUMMARY_CHARS = 512
MAX_CHECKPOINT_BYTES = 8 * 1024
"""The assertion the discipline test makes. Ten times what M0 measured for a
trivial graph, and roughly a fifth of what one inlined report would cost."""


@dataclass(frozen=True, slots=True)
class ArtifactRefView:
    """What a graph state is allowed to know about an artifact."""

    artifact_id: str
    sha256: str
    size_bytes: int
    kind: str
    summary: str

    def to_json(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "kind": self.kind,
            "summary": self.summary,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> ArtifactRefView:
        return cls(
            artifact_id=data["artifact_id"],
            sha256=data["sha256"],
            size_bytes=int(data["size_bytes"]),
            kind=data.get("kind", ""),
            summary=data.get("summary", ""),
        )

    async def load(self, artifacts: ArtifactStore) -> dict[str, Any]:
        """Fetch the body. Call this inside a node, never store what it returns.

        `ArtifactStore.get` verifies the digest, so a reference that has drifted
        from its bytes raises rather than returning something plausible.
        """
        raw = await artifacts.get(ArtifactId(_uuid(self.artifact_id)))
        loaded: dict[str, Any] = json.loads(raw.decode("utf-8"))
        return loaded


def summarise(payload: Any, limit: int = MAX_SUMMARY_CHARS) -> str:
    """A short, stable rendering of an output, for prompts and for state.

    Deterministic: sorted keys, no timestamps. A summary that varied between
    processes would change the prompt hash and destroy the cache hit rate on the
    system prefix, which is the one caching win the fixed template gets for free.
    """
    if isinstance(payload, str):
        text = payload
    else:
        text = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    text = " ".join(text.split())
    return text[:limit] + ("…" if len(text) > limit else "")


def _uuid(value: str) -> Any:
    import uuid

    return uuid.UUID(value)


def last(_current: Any, incoming: Any) -> Any:
    """The reducer every M1 state field uses.

    Last-write-wins, matching `echo_agent@1`. M1's graphs are linear or
    loop-with-accumulator; none of them fans out into concurrent writes to the same
    key, so a merge reducer would be machinery with no case behind it.
    """
    return incoming
