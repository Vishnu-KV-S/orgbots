"""`hasher@1` — a deterministic worker.

Hashes its input. That is all it does, and it is the right amount: the interesting
assertion about `hasher` is not what it computes but what happens when it tries to
call a model, which T10 checks by making it try.

It has no graph and no checkpointer. A deterministic worker's re-execution is
always safe — same input, same output, no external effect — so there is nothing to
checkpoint and nothing to recover.
"""

from __future__ import annotations

from typing import Any

from runtime.domain.hashing import canonical_hash, canonical_json
from runtime.handlers.registry import HandlerContext, register_handler


async def hasher(handler_ctx: HandlerContext, payload: dict[str, Any]) -> dict[str, Any]:
    _ = handler_ctx
    return {
        "sha256_prefix": canonical_hash(payload),
        "canonical": canonical_json(payload),
        "keys": sorted(payload),
    }


register_handler("hasher@1", hasher)
