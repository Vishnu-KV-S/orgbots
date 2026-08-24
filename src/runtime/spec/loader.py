"""YAML → `DocumentSet`.

Three things happen here and nothing else: the text is parsed, the envelope is
checked, and the `spec:` body is handed to the Pydantic model for its kind. Every
question that needs a second document is `validation`'s.

**`yaml.safe_load`, never `yaml.load`** (edge case 86). The unsafe loader constructs
arbitrary Python objects from tags — `!!python/object/apply:os.system` is the classic
— which makes a config file a code-execution path. This is not a hypothetical for a
control plane: the whole point of M4 is that the org is defined in files, and files
travel. `safe_load` refuses unknown tags outright, so a document carrying one fails to
parse rather than executing.

**Load order does not matter.** Documents are collected, then sorted by kind and name
(`DocumentSet.build`). A format where file order changed the result would make a diff
depend on which file somebody happened to open, which is edge case 85 arriving through
the front door.
"""

from __future__ import annotations

import io
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from runtime.observability.logging import get_logger
from runtime.spec.documents import (
    API_VERSION,
    SPEC_BY_KIND,
    Document,
    DocumentSet,
    Kind,
    Metadata,
    scan_for_secrets,
)
from runtime.spec.errors import SpecDocumentError

log = get_logger("spec.loader")

YAML_SUFFIXES = (".yaml", ".yml")

_ENVELOPE_KEYS = frozenset({"apiVersion", "kind", "metadata", "spec"})


def load_path(path: str | Path) -> DocumentSet:
    """Load one file, or every YAML file under a directory (recursively, sorted).

    A directory is the normal case — one file per kind, or one per department — and
    sorting the walk means two machines with different filesystem orderings produce
    the same document list and therefore the same plan.
    """
    root = Path(path)
    if not root.exists():
        raise SpecDocumentError(f"{root} does not exist")
    files = (
        [root]
        if root.is_file()
        else sorted(p for p in root.rglob("*") if p.suffix in YAML_SUFFIXES and p.is_file())
    )
    if not files:
        raise SpecDocumentError(f"{root} contains no {' or '.join(YAML_SUFFIXES)} files")
    documents: list[Document] = []
    for file in files:
        documents.extend(parse_text(file.read_text(encoding="utf-8"), source=str(file)))
    log.info("spec.loaded", files=len(files), documents=len(documents), root=str(root))
    return DocumentSet.build(documents)


def load_documents(sources: Iterable[str | Path]) -> DocumentSet:
    """Load several paths as one set. Duplicate identities across paths are an error."""
    documents: list[Document] = []
    for source in sources:
        documents.extend(load_path(source).documents)
    return DocumentSet.build(documents)


def parse_text(text: str, *, source: str = "<memory>") -> list[Document]:
    """Parse one file's worth of YAML into documents.

    The per-document source text is recovered by re-emitting the parsed value rather
    than by slicing the original on `---`. Slicing is what everybody tries first and it
    is wrong: `---` inside a block scalar is legal YAML, and a slicer that split there
    would store a truncated document as the "source" of a correctly applied one.
    """
    try:
        raw_documents = list(yaml.safe_load_all(io.StringIO(text)))
    except yaml.YAMLError as exc:
        raise SpecDocumentError(f"{source}: {exc}") from exc

    out: list[Document] = []
    for index, raw in enumerate(raw_documents):
        if raw is None:  # a trailing `---`, or a comment-only document
            continue
        out.append(_document(raw, source=source, index=index))
    if not out:
        raise SpecDocumentError(f"{source}: no documents")
    return out


def _document(raw: Any, *, source: str, index: int) -> Document:
    where = f"{source}[{index}]"
    if not isinstance(raw, dict):
        raise SpecDocumentError(f"{where}: expected a mapping, got {type(raw).__name__}")

    unknown = sorted(set(raw) - _ENVELOPE_KEYS)
    if unknown:
        raise SpecDocumentError(
            f"{where}: unknown top-level key(s) {unknown}; a document is {sorted(_ENVELOPE_KEYS)}"
        )

    api_version = raw.get("apiVersion")
    if api_version != API_VERSION:
        raise SpecDocumentError(
            f"{where}: apiVersion {api_version!r}, expected {API_VERSION!r}. A document "
            "this build cannot represent is refused rather than half-applied."
        )

    kind_raw = raw.get("kind")
    try:
        kind = Kind(str(kind_raw))
    except ValueError as exc:
        raise SpecDocumentError(
            f"{where}: kind {kind_raw!r} is not a document kind; known kinds are "
            f"{[k.value for k in Kind]}"
        ) from exc

    try:
        metadata = Metadata.model_validate(raw.get("metadata") or {})
    except ValidationError as exc:
        raise SpecDocumentError(f"{where} {kind.value}: metadata: {_terse(exc)}") from exc

    body = raw.get("spec")
    if body is None:
        body = {}
    if not isinstance(body, dict):
        raise SpecDocumentError(f"{where} {kind.value}/{metadata.name}: `spec` must be a mapping")

    named = f"{where} {kind.value}/{metadata.name}"
    # Before Pydantic, not after. `extra="forbid"` would reject an unexpected key and
    # the message would be about the key rather than about the credential in it.
    scan_for_secrets(body, where=named)

    try:
        spec = SPEC_BY_KIND[kind].model_validate(body)
    except ValidationError as exc:
        raise SpecDocumentError(f"{named}: {_terse(exc)}") from exc

    return Document(
        kind=kind,
        metadata=metadata,
        spec=spec,
        source=source,
        source_yaml=_emit(raw),
        index=index,
    )


def _emit(raw: Any) -> str:
    """The document's own text, canonically re-emitted.

    Deliberately not the operator's original bytes. Storing those would mean
    `spec_documents.source_yaml` differed between two applies of the same logical
    document over a comment or an indentation change, and the `UNIQUE (org, kind, name,
    spec_hash)` key would then be doing less work than it looks like it is. What is kept
    is what was *parsed* — which is the thing the compiler acted on.
    """
    return yaml.safe_dump(raw, sort_keys=True, default_flow_style=False, allow_unicode=True)


def _terse(exc: ValidationError) -> str:
    """Pydantic's report, one line per failure, without the URL footer.

    The default rendering is three lines per error and ends with a docs link, which in
    a CLI that may print twenty failures buries the one that matters.
    """
    parts: list[str] = []
    for error in exc.errors():
        location = ".".join(str(p) for p in error["loc"]) or "spec"
        parts.append(f"{location}: {error['msg']}")
    return "; ".join(parts)


def dump_documents(documents: Sequence[Document]) -> str:
    """Render documents back to a single multi-document YAML file.

    Used by the exporter (PR-37) and by `spec show`. `sort_keys=True` throughout, so a
    re-export of an unchanged org produces an unchanged file and `git diff` says
    nothing — which is the property that makes the exporter usable as a drift tool.
    """
    return "---\n".join(
        yaml.safe_dump(doc.envelope(), sort_keys=True, default_flow_style=False, allow_unicode=True)
        for doc in documents
    )


__all__ = ["dump_documents", "load_documents", "load_path", "parse_text"]
