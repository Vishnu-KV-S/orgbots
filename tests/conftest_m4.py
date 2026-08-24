"""M4 fixtures — a document corpus you can bend one document at a time.

The §6 exit criterion is *"every validation has a positive and a negative test"*, and
that is only writable if a negative test can be one edit away from a valid corpus.
`Corpus` is that: the exported M1 department as mutable envelopes, with `edit`, `add`
and `drop`, re-serialised through the real loader so a test never constructs a
`Document` by hand and therefore never skips the parsing rules it is not testing.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any

import yaml

from runtime.spec.compile import CompiledOrg, compile_org
from runtime.spec.documents import API_VERSION, DocumentSet
from runtime.spec.exporter import export_department
from runtime.spec.loader import parse_text
from runtime.spec.validation import Registries, validate

M4_ORGANIZATION = "acme"

TEST_REGISTRIES = Registries(
    tools=frozenset({"web.search@1", "web.fetch@1", "publish.external@1", "fixture.sideeffect@1"}),
    graphs=frozenset({"marketing_head@1", "research@1", "content@1", "echo_agent@1"}),
    handlers=frozenset({"analytics@1", "hasher@1"}),
    tool_actions={"publish.external@1": "publish_external"},
)
"""The M1 registry, stated rather than imported.

A validation test that imported the live registry would start failing when somebody
registered a tool, which is the wrong reason for a validation test to fail.
"""


@dataclass
class Corpus:
    """The exported department as raw envelopes, one edit away from anything."""

    envelopes: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def department(cls) -> Corpus:
        return cls([copy.deepcopy(d.envelope()) for d in export_department().documents])

    # --- mutation -------------------------------------------------------------------

    # Positional-only, so a spec field called `kind` or `name` — an Actor's runtime
    # kind, for instance — does not collide with the addressing arguments.
    def find(self, kind: str, name: str, /) -> dict[str, Any]:
        for envelope in self.envelopes:
            if envelope["kind"] == kind and envelope["metadata"]["name"] == name:
                return envelope
        raise KeyError(f"{kind}/{name} is not in the corpus")

    def edit(self, kind: str, name: str, /, **fields: Any) -> Corpus:
        self.find(kind, name)["spec"].update(fields)
        return self

    def rename(self, kind: str, name: str, new: str, /) -> Corpus:
        self.find(kind, name)["metadata"]["name"] = new
        return self

    def add(self, kind: str, name: str, spec: dict[str, Any] | None = None, /) -> Corpus:
        self.envelopes.append(
            {
                "apiVersion": API_VERSION,
                "kind": kind,
                "metadata": {"name": name},
                "spec": spec or {},
            }
        )
        return self

    def drop(self, kind: str, name: str, /) -> Corpus:
        self.envelopes = [
            e for e in self.envelopes if not (e["kind"] == kind and e["metadata"]["name"] == name)
        ]
        return self

    def drop_kind(self, kind: str) -> Corpus:
        self.envelopes = [e for e in self.envelopes if e["kind"] != kind]
        return self

    # --- realisation ----------------------------------------------------------------

    def to_yaml(self) -> str:
        return "---\n".join(
            yaml.safe_dump(e, sort_keys=True, default_flow_style=False) for e in self.envelopes
        )

    def documents(self) -> DocumentSet:
        return DocumentSet.build(parse_text(self.to_yaml(), source="<corpus>"))

    def compile(self) -> CompiledOrg:
        return compile_org(self.documents())

    def validate(self, *, registries: Registries | None = None) -> CompiledOrg:
        org = self.compile()
        validate(org, registries=registries or TEST_REGISTRIES)
        return org
