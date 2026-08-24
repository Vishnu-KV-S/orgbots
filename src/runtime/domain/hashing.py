"""Canonical hashing.

Two properties matter and both are tested (T1, T6):

1. The same logical value hashes identically regardless of dict insertion order,
   set iteration order, or `PYTHONHASHSEED`.
2. The same logical value hashes identically across processes and across restarts.

That rules out `hash()`, `id()`, `repr()` of anything unordered, and pickle. What
is left is: normalise to a total order, serialise with a fixed separator set, hash
with SHA-256.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

_HASH_PREFIX_LEN = 32
"""Hex characters retained. 128 bits of a SHA-256 — collision-free for any number
of specs a real org will ever have, and short enough to read in a log line."""


def _normalise(value: Any) -> Any:
    """Map a value onto the JSON subset, in a total order.

    Sets are sorted by their *normalised serialised form* rather than by the values
    themselves, because a set of mixed types has no natural order and sorting must
    not depend on `__lt__` being defined.
    """
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        # Reject non-finite floats rather than emit `NaN`, which is not JSON and
        # would not compare equal to itself anyway.
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"non-finite float is not canonicalisable: {value!r}")
        return value
    if isinstance(value, Enum):
        return _normalise(value.value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes | bytearray):
        return hashlib.sha256(bytes(value)).hexdigest()
    if isinstance(value, Mapping):
        return {str(k): _normalise(v) for k, v in sorted(value.items(), key=lambda kv: str(kv[0]))}
    if isinstance(value, frozenset | set):
        items = [_normalise(v) for v in value]
        return sorted(items, key=lambda v: json.dumps(v, sort_keys=True, separators=(",", ":")))
    if isinstance(value, Sequence):
        return [_normalise(v) for v in value]
    if hasattr(value, "model_dump"):  # pydantic BaseModel, without importing it here
        return _normalise(value.model_dump(mode="python"))
    raise TypeError(f"not canonicalisable: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Deterministic JSON text for any canonicalisable value."""
    return json.dumps(
        _normalise(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


def canonical_hash(value: Any, *, prefix_len: int = _HASH_PREFIX_LEN) -> str:
    """SHA-256 of the canonical form, truncated to `prefix_len` hex characters."""
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return digest[:prefix_len]


def spec_hash(spec: Any) -> str:
    """Identity of a compiled spec. Two runs with the same `spec_hash` ran under
    provably the same configuration."""
    return canonical_hash(spec)


def args_hash(args: Any) -> str:
    """Identity of one tool call's arguments. Feeds `logical_call_id`, so its
    determinism is the determinism of exactly-once."""
    return canonical_hash(args)
