"""The config plane (M4).

The organization, defined in YAML instead of Python, **without changing what it
does**. That last clause is the whole milestone and it is checkable rather than
arguable: `tests/test_m4_roundtrip.py` exports the M1 department to YAML, compiles it
back, and asserts every `spec_hash` is byte-identical to the one the Python
definitions produce. If those match, the config plane provably changed nothing about
runtime behaviour (§8).

The pipeline, and every arrow is a module here:

    YAML files
       ↓  loader.load_documents      safe_load, envelope, per-kind Pydantic
       ↓  validation.validate        the §6 semantic checks
       ↓  compile.compile_org        dependency graph → topological order
       ↓  compile                    canonical JSON → spec_hash
       ↓  differ.build_plan          diff against active versions
       ↓  apply.apply_plan           one transaction, advisory-locked

**This is not a reconciler** and the omission is deliberate (§2). `apply` is a command
somebody runs, not a daemon that converges. A control loop that rewrote the org while
a measurement window was open is the exact opposite of what this milestone is for, and
"apply on git push" is one small step from here to there.

**Nothing in this package runs on the hot path.** It is imported by the CLI and by
seeding, never by a worker, a graph or a gateway. A run executes under the RunSpec it
was admitted with, so an apply mid-flight cannot change what an in-flight run does —
the same property M0 built and the reason edge case 72 needs no new mechanism.
"""

from __future__ import annotations

from runtime.spec.compile import CompiledActor, CompiledOrg, compile_org
from runtime.spec.documents import API_VERSION, Document, DocumentSet, Kind
from runtime.spec.errors import SpecDocumentError, SpecValidationError
from runtime.spec.loader import load_documents, load_path
from runtime.spec.validation import validate

__all__ = [
    "API_VERSION",
    "CompiledActor",
    "CompiledOrg",
    "Document",
    "DocumentSet",
    "Kind",
    "SpecDocumentError",
    "SpecValidationError",
    "compile_org",
    "load_documents",
    "load_path",
    "validate",
]
