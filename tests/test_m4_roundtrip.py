"""§8 — the exit test that matters.

    python_specs  ──compile──►  hash_A
         │
       export
         ▼
       YAML  ──load──►──compile──►  hash_B

    assert hash_A == hash_B, for every actor

If those match, the config plane provably changed nothing about how the system
behaves. That is the entire claim M4 needs to make, and it is checkable in CI rather
than argued about.

**Keep this test permanently.** It is also the regression guard for every future change
to the compiler: §13 risk 2 is that the YAML compiler materialises a default
differently from Python, every apply then churns a version for every actor, and nobody
notices until month four.

No database. The whole point of compiling to `ActorSpec` rather than to a parallel type
is that the comparison is a value comparison.
"""

from __future__ import annotations

import pytest

from runtime.domain.hashing import canonical_hash
from runtime.org.department import DEPARTMENT_SPECS
from runtime.spec.compile import compile_org
from runtime.spec.documents import DocumentSet, Kind
from runtime.spec.exporter import export_department
from runtime.spec.loader import load_path, parse_text
from runtime.spec.validation import default_registries, validate

COMMITTED_CORPUS = "config/org"


@pytest.fixture(scope="module")
def exported():
    return export_department()


@pytest.fixture(scope="module")
def compiled_from_export(exported):
    documents = DocumentSet.build(parse_text(exported.to_yaml(), source="<roundtrip>"))
    return compile_org(documents)


def test_every_actor_hash_is_byte_identical(compiled_from_export) -> None:
    """§8. The claim, stated as an assertion."""
    by_name = {actor.name: actor for actor in compiled_from_export.actors}
    assert set(by_name) == {spec.name for spec in DEPARTMENT_SPECS}

    for spec in DEPARTMENT_SPECS:
        compiled = by_name[spec.name]
        assert compiled.spec_hash == canonical_hash(spec), (
            f"{spec.name}: the YAML compiles to a different spec_hash than the Python "
            "definition. Every actor would get a new version on the first apply, and "
            "the ability to prove nothing changed is gone (§8, risk 2)."
        )


def test_the_compiled_spec_is_the_same_object_not_merely_the_same_hash(
    compiled_from_export,
) -> None:
    """Equality as well as hash equality.

    A hash collision is not the worry — 128 bits of SHA-256 rules that out. What this
    catches is a field that `canonical_hash` happens not to distinguish, which would be
    a hole in the hash rather than in the compiler and is worth finding here.
    """
    by_name = {actor.name: actor.spec for actor in compiled_from_export.actors}
    for spec in DEPARTMENT_SPECS:
        assert by_name[spec.name] == spec


def test_the_committed_corpus_matches_the_python_definitions() -> None:
    """`config/org` is the export, committed. Drift between them is a stale checkout.

    Separate from the in-memory round trip on purpose: that one proves the compiler is
    correct, this one proves the files somebody actually applies are current. A
    refactor that changes a ceiling in `runtime.org.department` and forgets to re-export
    fails here and nowhere else.
    """
    org = compile_org(load_path(COMMITTED_CORPUS))
    by_name = {actor.name: actor for actor in org.actors}
    for spec in DEPARTMENT_SPECS:
        assert by_name[spec.name].spec_hash == canonical_hash(spec), (
            f"{spec.name}: config/org is out of date with runtime.org.department. "
            "Re-export it: python -m runtime.cli spec export"
        )


def test_the_committed_corpus_validates() -> None:
    org = compile_org(load_path(COMMITTED_CORPUS))
    validate(org, registries=default_registries())


def test_export_is_stable(exported) -> None:
    """Re-exporting produces byte-identical YAML.

    Which is what makes `spec export` usable as a drift tool: `git diff` after an
    export says something only when the Python actually changed. Non-determinism here —
    a set iteration, an unsorted dict — would make every export a spurious diff.
    """
    assert export_department().to_yaml() == exported.to_yaml()


def test_export_covers_every_governance_object(exported) -> None:
    """The export is the whole department, not the parts that were easy.

    A missing `ToolGrant` document would still pass the hash test — grants are not in
    `ActorSpec` — and would silently un-grant three tools on the first apply.
    """
    from runtime.org import governance_seed as gov
    from runtime.org.department import TRIGGERS

    assert len(exported.documents.of(Kind.ACTOR)) == len(DEPARTMENT_SPECS)
    assert len(exported.documents.of(Kind.ROLE)) == len(gov.ROLES)
    assert len(exported.documents.of(Kind.CONNECTION)) == len(gov.CONNECTIONS)
    assert len(exported.documents.of(Kind.TOOL_GRANT)) == len(gov.GRANTS)
    assert len(exported.documents.of(Kind.AUTHORITY_POLICY)) == len(gov.POLICIES)
    assert len(exported.documents.of(Kind.TRIGGER)) == len(TRIGGERS)


def test_no_credential_value_reaches_the_documents(exported) -> None:
    """§3. Connections carry a `secretRef`, never a secret.

    `governance_seed` names `search_api_key`; the encrypted store from migration 022
    holds the value. If the exporter ever started copying values, this is the test that
    would say so before a `git add`.
    """
    text = exported.to_yaml()
    assert "secretRef" in text
    for connection in exported.documents.of(Kind.CONNECTION):
        body = connection.body()
        credentials = body.get("credentials")
        assert credentials is None or set(credentials) == {"secretRef"}
