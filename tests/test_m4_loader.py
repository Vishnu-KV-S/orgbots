"""The loader and the document format — §3 and edge cases 82, 83, 85, 86.

No database. Everything here is a property of one document or of one file.
"""

from __future__ import annotations

import textwrap

import pytest

from runtime.spec.documents import DocumentSet, Kind, scan_for_secrets
from runtime.spec.errors import (
    InlineSecretError,
    SpecDocumentError,
    UnpinnedReferenceError,
)
from runtime.spec.loader import dump_documents, load_path, parse_text

ACTOR = """\
apiVersion: agent-platform/v1
kind: Actor
metadata:
  name: research
spec:
  kind: llm_agent
  graph: research@1
  tools: [web.search@1]
"""


def parse(text: str, source: str = "t.yaml"):
    return parse_text(textwrap.dedent(text), source=source)


# --- the envelope ----------------------------------------------------------------------


def test_a_well_formed_document_parses() -> None:
    (doc,) = parse(ACTOR)
    assert doc.kind is Kind.ACTOR
    assert doc.name == "research"
    assert doc.spec.graph == "research@1"


def test_several_documents_per_file() -> None:
    docs = parse(ACTOR + "---\n" + ACTOR.replace("research", "content"))
    assert [d.name for d in docs] == ["research", "content"]


def test_a_trailing_separator_is_not_a_document() -> None:
    assert len(parse(ACTOR + "---\n")) == 1


def test_unknown_api_version_is_refused() -> None:
    with pytest.raises(SpecDocumentError, match="apiVersion"):
        parse(ACTOR.replace("agent-platform/v1", "agent-platform/v2"))


def test_unknown_kind_is_refused() -> None:
    with pytest.raises(SpecDocumentError, match="not a document kind"):
        parse(ACTOR.replace("kind: Actor", "kind: Robot"))


def test_unknown_top_level_key_is_refused() -> None:
    with pytest.raises(SpecDocumentError, match="unknown top-level key"):
        parse(ACTOR + "status: {}\n")


def test_unknown_spec_field_is_refused() -> None:
    """`extra="forbid"`. A typo in a field that was supposed to constrain something
    must fail compilation rather than be carried along and ignored."""
    with pytest.raises(SpecDocumentError, match="tolls"):
        parse(ACTOR.replace("  tools: [web.search@1]", "  tolls: [web.search@1]"))


def test_a_name_that_is_not_an_identity_key_is_refused() -> None:
    with pytest.raises(SpecDocumentError, match="metadata"):
        parse(ACTOR.replace("name: research", "name: Research Team"))


def test_duplicate_identity_across_files_is_refused() -> None:
    """Identity is `(kind, metadata.name)` within an organization (§4)."""
    docs = parse(ACTOR, source="a.yaml") + parse(ACTOR, source="b.yaml")
    with pytest.raises(SpecDocumentError, match="duplicate document"):
        DocumentSet.build(docs)


# --- edge case 86: unsafe tags ------------------------------------------------------------


def test_a_yaml_tag_cannot_construct_a_python_object() -> None:
    """Edge case 86. `yaml.load` would run this; `safe_load` refuses it.

    The payload is inert on purpose — what is asserted is that the *constructor* is
    unavailable, not that this particular string is harmless.
    """
    payload = ACTOR.replace("  graph: research@1", "  graph: !!python/object/apply:os.getcwd []")
    with pytest.raises(SpecDocumentError):
        parse(payload)


def test_an_unknown_tag_is_refused_rather_than_ignored() -> None:
    with pytest.raises(SpecDocumentError):
        parse(ACTOR.replace("  graph: research@1", "  graph: !Custom research@1"))


# --- edge case 83: unpinned references ------------------------------------------------------


def test_an_unpinned_tool_is_refused() -> None:
    with pytest.raises(UnpinnedReferenceError, match="not version-pinned"):
        parse(ACTOR.replace("web.search@1", "web.search"))


def test_an_unpinned_graph_is_refused() -> None:
    with pytest.raises(UnpinnedReferenceError, match="not version-pinned"):
        parse(ACTOR.replace("graph: research@1", "graph: research"))


def test_a_pinned_reference_is_accepted() -> None:
    (doc,) = parse(ACTOR.replace("research@1", "research@12"))
    assert doc.spec.graph == "research@12"


# --- edge case 82: inline secrets --------------------------------------------------------


CONNECTION = """\
apiVersion: agent-platform/v1
kind: Connection
metadata:
  name: search-primary
spec:
  provider: serper
  credentials:
    secretRef: search_api_key
"""


def test_a_secret_ref_is_the_accepted_form() -> None:
    (doc,) = parse(CONNECTION)
    assert doc.spec.credentials is not None
    assert doc.spec.credentials.secret_ref == "search_api_key"


def test_a_literal_under_a_credential_key_is_an_error_not_a_warning() -> None:
    with pytest.raises(InlineSecretError, match="never inline"):
        parse(
            CONNECTION.replace(
                "  credentials:\n    secretRef: search_api_key", "  credentials: hunter2"
            )
        )


def test_a_boring_value_under_a_credential_key_is_still_refused() -> None:
    """The key check catches what no entropy heuristic would."""
    with pytest.raises(InlineSecretError):
        scan_for_secrets({"apiKey": "abc"}, where="t")


@pytest.mark.parametrize(
    "value",
    [
        "sk-abcdefghijklmnopqrstuvwx",
        "ghp_abcdefghijklmnopqrstuvwxyz123456",
        "AKIAIOSFODNN7EXAMPLE",
        "xoxb-1234567890-abcdefghij",
        "-----BEGIN RSA PRIVATE KEY-----\nMII...",
    ],
)
def test_a_credential_shape_is_refused_under_any_key(value: str) -> None:
    """The second check: a secret whose key was renamed to something innocent."""
    with pytest.raises(InlineSecretError, match=r"looks like a live credential"):
        scan_for_secrets({"note": value}, where="t")


def test_an_ordinary_string_is_not_flagged() -> None:
    scan_for_secrets(
        {"description": "publish to staging", "cron": "0 8 * * 1", "model": "deepseek-v4-pro"},
        where="t",
    )


# --- edge case 85: ordering ---------------------------------------------------------------


def test_document_order_does_not_depend_on_file_order() -> None:
    """A diff whose order depends on which file somebody edited is not reviewable."""
    a = parse(ACTOR, source="a.yaml")
    b = parse(CONNECTION, source="b.yaml")
    forwards = DocumentSet.build([*a, *b])
    backwards = DocumentSet.build([*b, *a])
    assert [d.key for d in forwards] == [d.key for d in backwards]


def test_dump_is_deterministic() -> None:
    docs = parse(ACTOR + "---\n" + CONNECTION)
    assert dump_documents(docs) == dump_documents(docs)


def test_dump_then_parse_is_a_fixed_point() -> None:
    """Re-emitting and re-reading changes nothing. What makes `spec show` trustworthy."""
    original = parse(ACTOR + "---\n" + CONNECTION)
    again = parse_text(dump_documents(original), source="round.yaml")
    assert [d.envelope() for d in original] == [d.envelope() for d in again]


# --- directories --------------------------------------------------------------------------


def test_a_directory_loads_every_yaml_file_sorted(tmp_path) -> None:
    (tmp_path / "20-connection.yaml").write_text(CONNECTION)
    (tmp_path / "10-actor.yml").write_text(ACTOR)
    (tmp_path / "notes.txt").write_text("ignored")
    documents = load_path(tmp_path)
    assert {d.key for d in documents} == {
        (Kind.ACTOR, "research"),
        (Kind.CONNECTION, "search-primary"),
    }


def test_a_missing_path_is_an_error(tmp_path) -> None:
    with pytest.raises(SpecDocumentError, match="does not exist"):
        load_path(tmp_path / "nope")


def test_an_empty_directory_is_an_error(tmp_path) -> None:
    with pytest.raises(SpecDocumentError, match=r"no .* files"):
        load_path(tmp_path)


def test_the_stored_source_is_the_parsed_document_not_the_whole_file() -> None:
    """`spec_documents.source_yaml` is per document, and a `---` inside a block scalar
    must not split one. Slicing on `---` is what everybody tries first and it is wrong."""
    text = (
        ACTOR.replace(
            "kind: llm_agent",
            "kind: llm_agent\n  handler: null",
        )
        + "---\n"
        + CONNECTION.replace(
            "  provider: serper",
            "  provider: |\n    serper\n    ---\n    not a separator",
        )
    )
    docs = parse(text)
    assert len(docs) == 2
    assert "not a separator" in docs[1].source_yaml
    assert "not a separator" not in docs[0].source_yaml
