"""Deterministic worker handlers.

Bound by `max_llm_calls == 0`, enforced at the ModelGateway rather than by
convention. Like `runtime.graphs`, this package may not import `httpx`, `boto3`,
`redis` or a provider SDK.
"""

from runtime.handlers import (
    analytics,  # noqa: F401  (registers analytics@1 on import)
    hasher,  # noqa: F401  (registers hasher@1 on import)
)
from runtime.handlers.registry import get_handler, known_handlers, register_handler

__all__ = ["get_handler", "known_handlers", "register_handler"]
