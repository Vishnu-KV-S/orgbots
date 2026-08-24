"""Shared graph machinery.

Four things every M1 actor needs, factored out because four copies of any of them
would be four chances to get one subtly different — and the one that got it wrong
would be discovered as a metric that did not add up rather than as a bug.

- `state`     — the "references, not bodies" rule the M0 retro produced
- `context`   — the fixed six-part template §2 pins down
- `structured`— a schema-constrained model call with one corrective retry
- `summarize` — the SUMMARIZATION-class node every LLM actor ends with
"""

from runtime.graphs.common.context import AssembledContext, assemble
from runtime.graphs.common.state import ArtifactRefView, last, summarise
from runtime.graphs.common.structured import StructuredResult, call_structured
from runtime.graphs.common.summarize import maybe_summarize

__all__ = [
    "ArtifactRefView",
    "AssembledContext",
    "StructuredResult",
    "assemble",
    "call_structured",
    "last",
    "maybe_summarize",
    "summarise",
]
