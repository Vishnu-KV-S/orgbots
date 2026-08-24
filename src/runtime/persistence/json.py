"""JSON parameter adaptation.

psycopg3 does not adapt a bare `dict` to `jsonb`, and relying on a globally
registered dumper would make the behaviour depend on import order. Every jsonb
parameter goes through `to_jsonb()` and every statement casts it explicitly, so
what reaches the database is unambiguous.

Using the canonical encoder rather than `json.dumps` is a small bonus: stored JSON
comes out with sorted keys, so a `jsonb` column diff between two rows reflects a
real difference rather than a dict ordering difference.
"""

from __future__ import annotations

from typing import Any

from runtime.domain.hashing import canonical_json


def to_jsonb(value: Any) -> str:
    """Serialise a value for a `CAST(:param AS jsonb)` placeholder."""
    return canonical_json(value)


def to_jsonb_or_none(value: Any) -> str | None:
    return None if value is None else canonical_json(value)
